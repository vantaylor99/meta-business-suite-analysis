"""Guarded authoring: create campaigns, ad sets, ads (incl. duplicating an ad), and
lookalike audiences.

Safety model (same spirit as the rest of the repo):
- Every created campaign/ad set/ad is forced to **status PAUSED** — authoring never spends.
  An explicit `apply-ops set_status ACTIVE` (separately approved) is required to go live.
- Per-op approval (`status: approved`), `--validate-only` real dry test, `--execute`, audit log.
- Advantage+/Meta-AI params are rejected (consistent with account policy).
- Creation only; no delete/archive here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_REPORTS_ROOT
from .control import FORBIDDEN_FRAGMENTS
from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env
from .reader_provider import MetaReaderProvider, as_reader
from .utils import ensure_dir, write_json
from .write_grounding import op_grounding_gap

APPROVED_STATUS = "approved"
PROPOSED_STATUS = "proposed"
CREATED_STATUS = "created"

CREATE_KINDS = {"create_campaign", "create_adset", "create_ad", "create_video_ad", "create_lookalike"}
PAUSED_KINDS = {"create_campaign", "create_adset", "create_ad", "create_video_ad"}

# Every create changes account structure, so all authoring kinds are grounding-required: an approved
# create must carry a computed confidence band before it is sent (see ``write_grounding``). As in
# ``control.py`` the guard is enforced only when the plan opts in via
# ``guardrails.requires_grounding``; legacy/ungrounded plans are unaffected. Creates remain PAUSED
# regardless — grounding gates whether the create is *sent*, never the forced-PAUSED safety.
GROUNDING_REQUIRED_KINDS = set(CREATE_KINDS)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _guard_params(params: dict[str, Any]) -> None:
    for key, value in params.items():
        if any(frag in f"{key} {value}".lower() for frag in FORBIDDEN_FRAGMENTS):
            raise ValueError("authoring op attempts to set a Meta AI / Advantage+ parameter (blocked).")


@dataclass(slots=True)
class AuthoringResult:
    op_id: str
    kind: str
    status: str
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    created_id: str | None = None
    reason: str | None = None


def validate_authoring_op(op: dict[str, Any]) -> None:
    kind = op.get("kind")
    params = op.get("params") if isinstance(op.get("params"), dict) else {}
    if kind not in CREATE_KINDS:
        raise ValueError(f"Unsupported authoring kind: {kind!r}. Allowed: {sorted(CREATE_KINDS)}")
    _guard_params(params)
    if kind == "create_campaign":
        if not str(params.get("name") or "").strip():
            raise ValueError("create_campaign requires params.name.")
        if not str(params.get("objective") or "").strip():
            raise ValueError("create_campaign requires params.objective (e.g. OUTCOME_SALES).")
    elif kind == "create_adset":
        for req in ("name", "campaign_id"):
            if not str(params.get(req) or "").strip():
                raise ValueError(f"create_adset requires params.{req}.")
    elif kind == "create_ad":
        if not str(params.get("name") or "").strip():
            raise ValueError("create_ad requires params.name.")
        if not str(params.get("adset_id") or "").strip():
            raise ValueError("create_ad requires params.adset_id.")
        if not isinstance(params.get("creative"), dict):
            raise ValueError("create_ad requires params.creative (e.g. {'creative_id': '<id>'}).")
    elif kind == "create_video_ad":
        for req in ("name", "adset_id", "video_id", "page_id", "link"):
            if not str(params.get(req) or "").strip():
                raise ValueError(f"create_video_ad requires params.{req}.")
        texts = params.get("primary_texts")
        has_multi = isinstance(texts, list) and any(str(t).strip() for t in texts)
        if not has_multi and not str(params.get("message") or "").strip():
            raise ValueError("create_video_ad requires params.message or a non-empty params.primary_texts list.")
        if isinstance(texts, list) and len(texts) > 5:
            raise ValueError("create_video_ad supports at most 5 primary_texts.")
        for field in ("headlines", "descriptions"):
            vals = params.get(field)
            if isinstance(vals, list) and len(vals) > 5:
                raise ValueError(f"create_video_ad supports at most 5 {field}.")
    elif kind == "create_lookalike":
        if not str(params.get("name") or "").strip():
            raise ValueError("create_lookalike requires params.name.")
        if not str(params.get("origin_audience_id") or "").strip():
            raise ValueError("create_lookalike requires params.origin_audience_id.")
        ratio = params.get("ratio")
        if not isinstance(ratio, (int, float)) or not (0.01 <= float(ratio) <= 0.20):
            raise ValueError("create_lookalike requires params.ratio between 0.01 and 0.20.")
        if not str(params.get("country") or "").strip():
            raise ValueError("create_lookalike requires params.country (e.g. 'US').")


def _build_create(op: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (api_method_name, request_params) for an authoring op. Forces PAUSED creates."""
    kind = op["kind"]
    params = dict(op.get("params") or {})
    if kind == "create_campaign":
        params["status"] = "PAUSED"
        params.setdefault("special_ad_categories", [])
        return "create_campaign", params
    if kind == "create_adset":
        params["status"] = "PAUSED"
        return "create_adset", params
    if kind == "create_ad":
        params["status"] = "PAUSED"
        return "create_ad", params
    if kind == "create_video_ad":
        cta_type = params.get("call_to_action_type", "SHOP_NOW")
        texts = [str(t).strip() for t in (params.get("primary_texts") or []) if str(t).strip()]
        if texts:
            # Multiple text options: Meta optimizes among the operator-written copy we provide
            # (asset_feed_spec). This text optimization is the intended behavior here; we do NOT
            # add the deprecated standard_enhancements field (Meta rejects it). The account's
            # "no Advantage+ creative" stance concerns image/visual AI manipulation, not picking
            # among our own texts — no image media is generated since we supply the video as-is.
            video_asset: dict[str, Any] = {"video_id": str(params["video_id"])}
            if params.get("image_hash"):
                video_asset["thumbnail_hash"] = params["image_hash"]
            afs: dict[str, Any] = {
                "ad_formats": ["SINGLE_VIDEO"],
                "videos": [video_asset],
                "bodies": [{"text": t} for t in texts[:5]],
                "call_to_action_types": [cta_type],
                "link_urls": [{"website_url": params["link"]}],
            }
            headlines = [str(t).strip() for t in (params.get("headlines") or []) if str(t).strip()]
            descriptions = [str(t).strip() for t in (params.get("descriptions") or []) if str(t).strip()]
            if headlines:
                afs["titles"] = [{"text": t} for t in headlines[:5]]
            if descriptions:
                afs["descriptions"] = [{"text": t} for t in descriptions[:5]]
            request = {
                "name": params["name"],
                "adset_id": str(params["adset_id"]),
                "status": "PAUSED",
                "creative": {
                    "object_story_spec": {"page_id": str(params["page_id"])},
                    "asset_feed_spec": afs,
                },
            }
            return "create_ad", request
        video_data: dict[str, Any] = {
            "video_id": str(params["video_id"]),
            "message": params["message"],
            "call_to_action": {"type": cta_type, "value": {"link": params["link"]}},
        }
        if params.get("title"):
            video_data["title"] = params["title"]
        if params.get("description"):
            video_data["link_description"] = params["description"]
        if params.get("image_hash"):
            video_data["image_hash"] = params["image_hash"]
        elif params.get("image_url"):
            video_data["image_url"] = params["image_url"]
        request = {
            "name": params["name"],
            "adset_id": str(params["adset_id"]),
            "status": "PAUSED",
            "creative": {"object_story_spec": {"page_id": str(params["page_id"]), "video_data": video_data}},
        }
        return "create_ad", request
    if kind == "create_lookalike":
        request = {
            "name": params["name"],
            "subtype": "LOOKALIKE",
            "origin_audience_id": str(params["origin_audience_id"]),
            "lookalike_spec": {"ratio": float(params["ratio"]), "country": params["country"]},
        }
        return "create_custom_audience", request
    raise ValueError(f"Unhandled kind: {kind}")


def apply_authoring_plan(
    plan: dict[str, Any],
    client: MetaMarketingApiClient | None = None,
    *,
    execute: bool,
    validate_only: bool = False,
) -> list[AuthoringResult]:
    effective_client = client
    ad_account_id = str(plan.get("ad_account_id") or "")
    require_grounding = bool((plan.get("guardrails") or {}).get("requires_grounding"))
    results: list[AuthoringResult] = []
    for op in plan.get("ops") or []:
        if not isinstance(op, dict):
            continue
        op_id = str(op.get("op_id") or "op")
        kind = str(op.get("kind") or "")
        if op.get("status") != APPROVED_STATUS:
            results.append(AuthoringResult(op_id, kind, "skipped", reason="Op is not approved."))
            continue
        if require_grounding and kind in GROUNDING_REQUIRED_KINDS:
            gap = op_grounding_gap(op.get("confidence"), op.get("evidence"))
            if gap is not None:
                results.append(AuthoringResult(op_id, kind, "blocked", reason=gap))
                continue
        try:
            validate_authoring_op(op)
            method_name, request = _build_create(op)
        except ValueError as exc:
            results.append(AuthoringResult(op_id, kind, "blocked", reason=str(exc)))
            continue
        if not execute and not validate_only:
            results.append(AuthoringResult(op_id, kind, "dry_run", request=request))
            continue
        if effective_client is None:
            effective_client = client_from_env()
        method = getattr(effective_client, method_name)
        try:
            response = method(ad_account_id, params=request, validate_only=validate_only)
        except MetaApiError as exc:
            results.append(
                AuthoringResult(op_id, kind, "validation_failed" if validate_only else "failed", request=request, reason=str(exc))
            )
            continue
        created_id = response.get("id") if isinstance(response, dict) else None
        results.append(
            AuthoringResult(
                op_id, kind, "validated" if validate_only else CREATED_STATUS,
                request=request, response=response, created_id=created_id,
            )
        )
    return results


# --- Convenience builders ---------------------------------------------------


def build_duplicate_ad_plan(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    source_ad_id: str,
    target_adset_id: str,
    name: str | None = None,
    account_slug: str | None = None,
) -> dict[str, Any]:
    """Plan to recreate an existing ad's creative in a target ad set (created PAUSED).

    Read-only (reads the source ad's creative through ``reader``); the build only proposes an op.
    """
    reader = as_reader(reader)
    src = reader.get_ad(source_ad_id, fields=["id", "name", "creative"])
    creative = src.get("creative") if isinstance(src.get("creative"), dict) else {}
    creative_id = creative.get("id")
    if not creative_id:
        raise ValueError(f"Could not read a creative id from source ad {source_ad_id}.")
    op = {
        "op_id": f"dup_{source_ad_id}_to_{target_adset_id}",
        "kind": "create_ad",
        "params": {
            "name": name or f"{src.get('name') or 'Ad'} (copy)",
            "adset_id": target_adset_id,
            "creative": {"creative_id": creative_id},
        },
        "status": PROPOSED_STATUS,
        "note": f"duplicate of ad {source_ad_id} ({src.get('name')}) into ad set {target_adset_id}; created PAUSED",
    }
    return _wrap_plan([op], ad_account_id, account_slug, intent="duplicate_ad")


def build_video_ad_plan(
    ad_account_id: str,
    *,
    name: str,
    adset_id: str,
    video_id: str,
    page_id: str,
    link: str,
    message: str | None = None,
    title: str | None = None,
    description: str | None = None,
    primary_texts: list[str] | None = None,
    headlines: list[str] | None = None,
    descriptions: list[str] | None = None,
    call_to_action_type: str = "SHOP_NOW",
    image_hash: str | None = None,
    image_url: str | None = None,
    account_slug: str | None = None,
) -> dict[str, Any]:
    """Plan to create a video ad (PAUSED) from an uploaded video_id.

    Pass ``primary_texts`` (and optionally ``headlines``/``descriptions``) for the standard
    multiple-text-options ad (Meta optimizes among them); or ``message``/``title``/``description``
    for a single-text ad.
    """
    params: dict[str, Any] = {
        "name": name, "adset_id": adset_id, "video_id": video_id, "page_id": page_id,
        "link": link, "call_to_action_type": call_to_action_type,
    }
    if primary_texts:
        params["primary_texts"] = primary_texts
        if headlines:
            params["headlines"] = headlines
        if descriptions:
            params["descriptions"] = descriptions
    else:
        params["message"] = message
        if title:
            params["title"] = title
        if description:
            params["description"] = description
    if image_hash:
        params["image_hash"] = image_hash
    elif image_url:
        params["image_url"] = image_url
    op = {
        "op_id": f"video_ad_{video_id}_to_{adset_id}",
        "kind": "create_video_ad",
        "params": params,
        "status": PROPOSED_STATUS,
        "note": f"video ad '{name}' (video {video_id}) into ad set {adset_id}; created PAUSED",
    }
    return _wrap_plan([op], ad_account_id, account_slug, intent="create_video_ad")


def build_lookalike_plan(
    ad_account_id: str,
    *,
    name: str,
    origin_audience_id: str,
    country: str,
    ratio: float,
    account_slug: str | None = None,
) -> dict[str, Any]:
    op = {
        "op_id": f"lookalike_{origin_audience_id}_{int(ratio * 100)}",
        "kind": "create_lookalike",
        "params": {"name": name, "origin_audience_id": origin_audience_id, "country": country, "ratio": ratio},
        "status": PROPOSED_STATUS,
        "note": f"{int(ratio * 100)}% lookalike of {origin_audience_id} in {country}",
    }
    return _wrap_plan([op], ad_account_id, account_slug, intent="create_lookalike")


def _wrap_plan(ops: list[dict[str, Any]], ad_account_id: str, account_slug: str | None, *, intent: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "plan_type": "authoring",
        "intent": intent,
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "approval_instructions": (
            "Review each op. To create it, set its status to 'approved'. Created entities are always "
            "PAUSED. Only approved ops are sent to Meta, and only with --execute (or --validate-only)."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "creates_are_paused": True,
            "no_meta_ai_or_advantage_params": True,
        },
        "ops": ops,
    }


# --- Paths / writers --------------------------------------------------------


def default_authoring_plan_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "authoring_plan.json"


def default_authoring_results_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"authoring_results_{timestamp}.json"


def write_authoring_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def write_authoring_results(*, plan: dict[str, Any], results: list[AuthoringResult], output_path: Path, execute: bool) -> Path:
    payload = {
        "schema_version": 1,
        "plan_type": "authoring",
        "intent": plan.get("intent"),
        "account_slug": plan.get("account_slug"),
        "executed": execute,
        "generated_at": _now_iso(),
        "results": [
            {"op_id": r.op_id, "kind": r.kind, "status": r.status, "created_id": r.created_id,
             "request": r.request, "response": r.response, "reason": r.reason}
            for r in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path
