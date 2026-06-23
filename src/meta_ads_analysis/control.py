"""Agent control layer for a Meta ad account.

Two halves:
- **Read / situational awareness**: ``build_account_snapshot`` returns the full
  campaign -> ad set -> ad tree with status, delivery issues, budgets, and audiences.
- **Guarded write**: a small typed operations vocabulary (``set_status``,
  ``set_daily_budget``, ``rename``) over ad / ad set / campaign, applied through the same
  ``proposed -> approved -> validate-only -> execute`` gate as the rest of the repo.

Deliberately NOT supported here (too destructive / out of scope for now): delete, archive,
creating new campaigns/ad sets/ads, and arbitrary targeting edits (targeting has its own
guarded tools: rotation + advantage-audience disable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env
from .rotation import _audience_refs, _ids, advantage_audience_enabled
from .utils import ensure_dir, write_json

APPROVED_STATUS = "approved"
PROPOSED_STATUS = "proposed"
EXECUTED_STATUS = "executed"

SUPPORTED_OPS = {"set_status", "set_daily_budget", "rename"}
OP_LEVELS = {
    "set_status": {"ad", "adset", "campaign"},
    "set_daily_budget": {"adset", "campaign"},
    "rename": {"ad", "adset", "campaign"},
}
ALLOWED_STATUSES = {"ACTIVE", "PAUSED"}
FORBIDDEN_FRAGMENTS = ("advantage", "ai_", "creative_enhancement", "image_expansion", "text_variation")

CAMPAIGN_FIELDS = ["id", "name", "status", "effective_status", "objective", "daily_budget", "lifetime_budget"]
ADSET_FIELDS = [
    "id", "name", "status", "effective_status", "campaign_id",
    "daily_budget", "lifetime_budget", "optimization_goal", "targeting",
]
AD_FIELDS = ["id", "name", "status", "effective_status", "adset_id", "issues_info"]


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _issue_summaries(ad: dict[str, Any]) -> list[str]:
    out = []
    for i in ad.get("issues_info") or []:
        out.append(i.get("error_summary") or i.get("error_message") or "issue")
    return out


# --- Read / situational awareness -------------------------------------------


def build_account_snapshot(
    client: MetaMarketingApiClient,
    ad_account_id: str,
    *,
    active_only: bool = False,
) -> dict[str, Any]:
    """Return the full account tree plus rollups, for agent decision-making."""
    status_filter = ["ACTIVE"] if active_only else None
    campaigns = client.list_campaigns(ad_account_id, fields=CAMPAIGN_FIELDS, effective_status=status_filter)
    adsets = client.list_adsets(ad_account_id, fields=ADSET_FIELDS, effective_status=status_filter)
    ads = list(
        client.iter_paginated(
            f"/{ad_account_id}/ads", params={"fields": ",".join(AD_FIELDS), "limit": 200}
        )
    )

    ads_by_adset: dict[str, list[dict[str, Any]]] = {}
    for ad in ads:
        ads_by_adset.setdefault(str(ad.get("adset_id") or ""), []).append(ad)

    adsets_by_campaign: dict[str, list[dict[str, Any]]] = {}
    adset_nodes = []
    for a in adsets:
        targeting = a.get("targeting") if isinstance(a.get("targeting"), dict) else {}
        included = _audience_refs(targeting.get("custom_audiences"))
        excluded = _audience_refs(targeting.get("excluded_custom_audiences"))
        ad_nodes = [
            {
                "id": ad.get("id"),
                "name": ad.get("name"),
                "status": ad.get("status"),
                "effective_status": ad.get("effective_status"),
                "issues": _issue_summaries(ad),
            }
            for ad in ads_by_adset.get(str(a.get("id")), [])
        ]
        node = {
            "id": a.get("id"),
            "name": a.get("name"),
            "status": a.get("status"),
            "effective_status": a.get("effective_status"),
            "campaign_id": a.get("campaign_id"),
            "daily_budget": a.get("daily_budget"),
            "lifetime_budget": a.get("lifetime_budget"),
            "optimization_goal": a.get("optimization_goal"),
            "advantage_audience": advantage_audience_enabled(targeting),
            "included_audiences": [r.get("name") or r["id"] for r in included],
            "excluded_audiences": [r.get("name") or r["id"] for r in excluded],
            "ads": ad_nodes,
        }
        adset_nodes.append(node)
        adsets_by_campaign.setdefault(str(a.get("campaign_id") or ""), []).append(node)

    campaign_nodes = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "status": c.get("status"),
            "effective_status": c.get("effective_status"),
            "objective": c.get("objective"),
            "daily_budget": c.get("daily_budget"),
            "lifetime_budget": c.get("lifetime_budget"),
            "adsets": adsets_by_campaign.get(str(c.get("id")), []),
        }
        for c in campaigns
    ]

    all_ads = [ad for node in adset_nodes for ad in node["ads"]]
    issues = [
        {"ad_id": ad["id"], "ad_name": ad["name"], "issues": ad["issues"]}
        for ad in all_ads
        if ad["issues"]
    ]
    return {
        "schema_version": 1,
        "account_slug": None,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "active_only": active_only,
        "rollup": {
            "campaigns": len(campaign_nodes),
            "adsets": len(adset_nodes),
            "ads": len(all_ads),
            "active_ads": sum(1 for ad in all_ads if ad["effective_status"] == "ACTIVE"),
            "ads_with_issues": len(issues),
            "adsets_with_advantage_audience": sum(1 for n in adset_nodes if n["advantage_audience"]),
        },
        "ads_with_issues": issues,
        "campaigns": campaign_nodes,
    }


# --- Guarded write operations -----------------------------------------------


@dataclass(slots=True)
class OpResult:
    op_id: str
    status: str
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    reason: str | None = None


def validate_op(op: dict[str, Any]) -> None:
    """Raise ValueError if an op is malformed or violates a guardrail."""
    op_type = op.get("op")
    level = op.get("level")
    params = op.get("params") if isinstance(op.get("params"), dict) else {}
    if op_type not in SUPPORTED_OPS:
        raise ValueError(f"Unsupported op: {op_type!r}. Allowed: {sorted(SUPPORTED_OPS)}")
    if level not in OP_LEVELS[op_type]:
        raise ValueError(f"op {op_type} not allowed at level {level!r}; allowed: {sorted(OP_LEVELS[op_type])}")
    if not str(op.get("id") or "").strip():
        raise ValueError(f"op {op.get('op_id')} is missing target id.")
    for key, value in params.items():
        if any(frag in f"{key} {value}".lower() for frag in FORBIDDEN_FRAGMENTS):
            raise ValueError("op attempts to set a Meta AI / Advantage+ parameter (blocked).")
    if op_type == "set_status":
        if str(params.get("status") or "").upper() not in ALLOWED_STATUSES:
            raise ValueError("set_status requires params.status in {ACTIVE, PAUSED}.")
    elif op_type == "set_daily_budget":
        if (_num(params.get("daily_budget_cents")) or 0) <= 0:
            raise ValueError("set_daily_budget requires a positive params.daily_budget_cents.")
    elif op_type == "rename":
        if not str(params.get("name") or "").strip():
            raise ValueError("rename requires a non-empty params.name.")


def _get_entity(client: MetaMarketingApiClient, level: str, node_id: str, fields: list[str]) -> dict[str, Any]:
    if level == "ad":
        return client.get_ad(node_id, fields=fields)
    if level == "adset":
        return client.get_adset(node_id, fields=fields)
    return client.get_campaign(node_id, fields=fields)


def _update_entity(
    client: MetaMarketingApiClient, level: str, node_id: str, params: dict[str, Any], validate_only: bool
) -> dict[str, Any]:
    if level == "ad":
        return client.update_ad(node_id, params=params, validate_only=validate_only)
    if level == "adset":
        return client.update_adset(node_id, params=params, validate_only=validate_only)
    return client.update_campaign(node_id, params=params, validate_only=validate_only)


def _build_request(op: dict[str, Any], client: MetaMarketingApiClient) -> dict[str, Any]:
    """Translate an op into the Graph API params to POST. May re-read live state (budget cap)."""
    op_type = op["op"]
    params = op.get("params") or {}
    if op_type == "set_status":
        return {"status": str(params["status"]).upper()}
    if op_type == "rename":
        return {"name": str(params["name"])}
    if op_type == "set_daily_budget":
        new_cents = int(_num(params.get("daily_budget_cents")))
        max_increase = _num(params.get("max_increase_percent"))
        max_increase = 20.0 if max_increase is None else max_increase
        live = _get_entity(client, op["level"], str(op["id"]), ["id", "daily_budget"])
        current = _num(live.get("daily_budget"))
        if current is None or current <= 0:
            raise ValueError(
                "set_daily_budget needs an existing daily budget to cap against "
                "(entity has none — likely lifetime/CBO budget); not changing it."
            )
        if new_cents > current * (1 + max_increase / 100):
            raise ValueError(
                f"set_daily_budget {new_cents} exceeds max increase of {max_increase:.0f}% over current {int(current)}."
            )
        return {"daily_budget": str(new_cents)}
    raise ValueError(f"Unhandled op: {op_type}")


def apply_ops_plan(
    plan: dict[str, Any],
    client: MetaMarketingApiClient | None = None,
    *,
    execute: bool,
    validate_only: bool = False,
) -> list[OpResult]:
    """Dry-run, validate, or execute approved ops. Only approved ops are sent."""
    effective_client = client
    results: list[OpResult] = []
    for op in plan.get("ops") or []:
        if not isinstance(op, dict):
            continue
        op_id = str(op.get("op_id") or "op")
        if op.get("status") != APPROVED_STATUS:
            results.append(OpResult(op_id, "skipped", reason="Op is not approved."))
            continue
        try:
            validate_op(op)
        except ValueError as exc:
            results.append(OpResult(op_id, "blocked", reason=str(exc)))
            continue
        if effective_client is None:
            effective_client = client_from_env()
        try:
            request = _build_request(op, effective_client)
        except ValueError as exc:
            results.append(OpResult(op_id, "blocked", reason=str(exc)))
            continue

        if not execute and not validate_only:
            results.append(OpResult(op_id, "dry_run", request=request))
            continue
        try:
            response = _update_entity(
                effective_client, op["level"], str(op["id"]), request, validate_only
            )
        except MetaApiError as exc:
            results.append(
                OpResult(op_id, "validation_failed" if validate_only else "failed", request=request, reason=str(exc))
            )
            continue
        results.append(
            OpResult(op_id, "validated" if validate_only else EXECUTED_STATUS, request=request, response=response)
        )
    return results


# --- Convenience builder: enable paused ads ---------------------------------


def build_enable_ads_plan(
    client: MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str | None = None,
    adset_ids: list[str] | None = None,
    name_contains: str | None = None,
) -> dict[str, Any]:
    """Propose set_status=ACTIVE ops for currently-not-active ads, optionally filtered.

    Each op starts ``proposed`` with the ad's current effective_status + delivery issues in its
    note, so the operator/agent approves only the ads worth turning on.
    """
    ads = list(
        client.iter_paginated(
            f"/{ad_account_id}/ads",
            params={"fields": ",".join(AD_FIELDS), "limit": 200},
        )
    )
    scope = set(adset_ids or [])
    ops: list[dict[str, Any]] = []
    for ad in ads:
        if ad.get("effective_status") == "ACTIVE":
            continue
        if scope and str(ad.get("adset_id")) not in scope:
            continue
        if name_contains and name_contains.lower() not in str(ad.get("name") or "").lower():
            continue
        issues = _issue_summaries(ad)
        ops.append(
            {
                "op_id": f"enable_ad_{ad.get('id')}",
                "op": "set_status",
                "level": "ad",
                "id": ad.get("id"),
                "name": ad.get("name"),
                "params": {"status": "ACTIVE"},
                "status": PROPOSED_STATUS,
                "note": f"currently {ad.get('effective_status')}; issues: {'; '.join(issues) or 'none'}",
            }
        )
    return {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "enable_ads",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "approval_instructions": (
            "Review each ad. To enable it, set its op status to 'approved'. Only approved ops are "
            "sent to Meta, and only with --execute (or tested with --validate-only)."
        ),
        "guardrails": {"requires_explicit_approval": True, "statuses": sorted(ALLOWED_STATUSES)},
        "ops": ops,
    }


# --- Paths / writers --------------------------------------------------------


def resolve_ad_account_id(account_slug: str) -> str:
    return account_registry.resolve_account(
        account_slug, account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH
    ).ad_account_id


def default_snapshot_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "account_snapshot.json"


def default_ops_plan_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "ops_plan.json"


def default_ops_results_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"ops_results_{timestamp}.json"


def write_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def write_ops_results(*, plan: dict[str, Any], results: list[OpResult], output_path: Path, execute: bool) -> Path:
    payload = {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": plan.get("intent"),
        "account_slug": plan.get("account_slug"),
        "executed": execute,
        "generated_at": _now_iso(),
        "results": [
            {"op_id": r.op_id, "status": r.status, "request": r.request, "response": r.response, "reason": r.reason}
            for r in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path
