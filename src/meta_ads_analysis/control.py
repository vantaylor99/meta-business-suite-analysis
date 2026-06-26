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

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env
from .reader_provider import MetaReaderProvider, as_reader
from .rotation import _audience_refs, _ids, advantage_audience_enabled
from .utils import ensure_dir, write_json
from .write_grounding import op_grounding_gap

APPROVED_STATUS = "approved"
PROPOSED_STATUS = "proposed"
EXECUTED_STATUS = "executed"

TARGETING_OPS = {"set_age_range", "set_genders", "set_geo_locations", "set_placements"}
SUPPORTED_OPS = {"set_status", "set_daily_budget", "rename", "set_creative", "set_creative_features"} | TARGETING_OPS
OP_LEVELS = {
    "set_status": {"ad", "adset", "campaign"},
    "set_daily_budget": {"adset", "campaign"},
    "rename": {"ad", "adset", "campaign"},
    "set_creative": {"ad"},
    "set_creative_features": {"ad"},
    "set_age_range": {"adset"},
    "set_genders": {"adset"},
    "set_geo_locations": {"adset"},
    "set_placements": {"adset"},
}

# Account default for creative enhancements (data + research, 2026-06-24): additive/visual ON,
# copy-rewriting OFF. NB: the umbrella `standard_enhancements` field is deprecated — set individual
# features only. Tune per validate-only feedback (not every feature is valid for every creative).
DEFAULT_OPT_IN_FEATURES = [
    "enhance_cta", "inline_comment", "show_summary", "show_destination_blurbs",
    "reveal_details_over_time", "site_extensions", "product_extensions", "image_brightness_and_contrast",
]
DEFAULT_OPT_OUT_FEATURES = ["text_optimizations", "replace_media_text"]
ALLOWED_STATUSES = {"ACTIVE", "PAUSED"}
FORBIDDEN_FRAGMENTS = ("advantage", "ai_", "creative_enhancement", "image_expansion", "text_variation")

# Ops that change spend / delivery / structure must carry a computed confidence band before an
# approved write is sent (see ``write_grounding.attach_op_grounding`` / ``op_grounding_gap``). A pure
# ``rename`` is cosmetic — no spend, delivery, or structural change — so it is exempt. The guard is
# enforced only when the plan opts in via ``guardrails.requires_grounding`` (set by the grounded
# per-capability builders); legacy/ungrounded plans are unaffected.
GROUNDING_REQUIRED_OPS = SUPPORTED_OPS - {"rename"}

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
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    active_only: bool = False,
) -> dict[str, Any]:
    """Return the full account tree plus rollups, for agent decision-making.

    Read-only: ``reader`` is a :class:`MetaReaderProvider` (a raw ``MetaMarketingApiClient`` is
    accepted and wrapped), so reads route through the provider seam.
    """
    reader = as_reader(reader)
    status_filter = ["ACTIVE"] if active_only else None
    campaigns = reader.list_campaigns(ad_account_id, fields=CAMPAIGN_FIELDS, effective_status=status_filter)
    adsets = reader.list_adsets(ad_account_id, fields=ADSET_FIELDS, effective_status=status_filter)
    ads = list(
        reader.iter_paginated(
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
    elif op_type == "set_creative":
        if not str(params.get("creative_id") or "").strip():
            raise ValueError("set_creative requires params.creative_id (an existing valid creative).")
    elif op_type == "set_creative_features":
        opt_in = params.get("opt_in") or []
        opt_out = params.get("opt_out") or []
        if not isinstance(opt_in, list) or not isinstance(opt_out, list) or not (opt_in or opt_out):
            raise ValueError("set_creative_features requires non-empty params.opt_in and/or params.opt_out lists.")
    elif op_type == "set_age_range":
        lo, hi = _num(params.get("age_min")), _num(params.get("age_max"))
        if lo is None or hi is None or not (13 <= lo <= hi <= 65):
            raise ValueError("set_age_range requires 13 <= age_min <= age_max <= 65.")
    elif op_type == "set_genders":
        genders = params.get("genders")
        if not isinstance(genders, list) or any(g not in (1, 2) for g in genders):
            raise ValueError("set_genders requires params.genders as a list subset of [1, 2] (1=male, 2=female; [] = all).")
    elif op_type == "set_geo_locations":
        if not isinstance(params.get("geo_locations"), dict) or not params["geo_locations"]:
            raise ValueError("set_geo_locations requires a non-empty params.geo_locations object.")
    elif op_type == "set_placements":
        if not params.get("automatic") and not (isinstance(params.get("publisher_platforms"), list) and params["publisher_platforms"]):
            raise ValueError("set_placements requires params.automatic=true or a non-empty publisher_platforms list.")


def _get_entity(reader: MetaReaderProvider, level: str, node_id: str, fields: list[str]) -> dict[str, Any]:
    """Re-read one entity's live state (the read half of a guarded write); goes through the reader."""
    if level == "ad":
        return reader.get_ad(node_id, fields=fields)
    if level == "adset":
        return reader.get_adset(node_id, fields=fields)
    return reader.get_campaign(node_id, fields=fields)


def _update_entity(
    client: MetaMarketingApiClient, level: str, node_id: str, params: dict[str, Any], validate_only: bool
) -> dict[str, Any]:
    if level == "ad":
        return client.update_ad(node_id, params=params, validate_only=validate_only)
    if level == "adset":
        return client.update_adset(node_id, params=params, validate_only=validate_only)
    return client.update_campaign(node_id, params=params, validate_only=validate_only)


def _apply_targeting_change(op_type: str, params: dict[str, Any], targeting: Any) -> dict[str, Any]:
    """Read-modify-write one targeting dimension; preserves all other fields incl. automation."""
    t = copy.deepcopy(targeting) if isinstance(targeting, dict) else {}
    if op_type == "set_age_range":
        t["age_min"] = int(params["age_min"])
        t["age_max"] = int(params["age_max"])
        t.pop("age_range", None)
    elif op_type == "set_genders":
        genders = params.get("genders") or []
        if genders:
            t["genders"] = genders
        else:
            t.pop("genders", None)
    elif op_type == "set_geo_locations":
        t["geo_locations"] = params["geo_locations"]
    elif op_type == "set_placements":
        if params.get("automatic"):
            for k in ("publisher_platforms", "facebook_positions", "instagram_positions",
                      "audience_network_positions", "messenger_positions", "device_platforms"):
                t.pop(k, None)
        else:
            t["publisher_platforms"] = params["publisher_platforms"]
            for k in ("facebook_positions", "instagram_positions", "device_platforms"):
                if params.get(k) is not None:
                    t[k] = params[k]
    return t


def _build_request(op: dict[str, Any], reader: MetaReaderProvider) -> dict[str, Any]:
    """Translate an op into the Graph API params to POST. May re-read live state (budget cap,
    targeting, current creative) — that re-read is read-only and goes through the reader; the
    POST itself is done by the caller against the concrete write client."""
    op_type = op["op"]
    params = op.get("params") or {}
    if op_type == "set_status":
        return {"status": str(params["status"]).upper()}
    if op_type == "rename":
        return {"name": str(params["name"])}
    if op_type == "set_creative":
        return {"creative": {"creative_id": str(params["creative_id"])}}
    if op_type == "set_creative_features":
        # Creatives are immutable; to change enhancement enrollment we re-attach the SAME creative
        # content with a degrees_of_freedom_spec. Read the current creative and rebuild it.
        ad = reader.get_ad(str(op["id"]), fields=["creative{object_story_spec,asset_feed_spec}"])
        cr = ad.get("creative") if isinstance(ad.get("creative"), dict) else {}
        new_creative: dict[str, Any] = {}
        if isinstance(cr.get("object_story_spec"), dict):
            oss = copy.deepcopy(cr["object_story_spec"])
            # Read-back video_data can carry BOTH image_hash and image_url; Meta rejects re-posting
            # both ("ObjectStorySpecRedundant"). Keep the hash, drop the redundant url.
            vd = oss.get("video_data")
            if isinstance(vd, dict) and vd.get("image_hash") and vd.get("image_url"):
                vd.pop("image_url", None)
            new_creative["object_story_spec"] = oss
        if isinstance(cr.get("asset_feed_spec"), dict):
            new_creative["asset_feed_spec"] = copy.deepcopy(cr["asset_feed_spec"])
        feats: dict[str, Any] = {}
        for f in params.get("opt_in") or []:
            feats[str(f)] = {"enroll_status": "OPT_IN"}
        for f in params.get("opt_out") or []:
            feats[str(f)] = {"enroll_status": "OPT_OUT"}
        new_creative["degrees_of_freedom_spec"] = {"creative_features_spec": feats}
        return {"creative": new_creative}
    if op_type in TARGETING_OPS:
        live = _get_entity(reader, "adset", str(op["id"]), ["id", "targeting"])
        return {"targeting": _apply_targeting_change(op_type, params, live.get("targeting"))}
    if op_type == "set_daily_budget":
        new_cents = int(_num(params.get("daily_budget_cents")))
        max_increase = _num(params.get("max_increase_percent"))
        max_increase = 20.0 if max_increase is None else max_increase
        live = _get_entity(reader, op["level"], str(op["id"]), ["id", "daily_budget"])
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
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
) -> list[OpResult]:
    """Dry-run, validate, or execute approved ops. Only approved ops are sent.

    Mixed read+write: ``client`` performs the writes (``update_*``); the live re-reads inside
    ``_build_request`` (budget cap / targeting / current creative) go through ``reader``. When
    ``reader`` is not supplied it defaults to reading through the same ``client``, so a future
    hybrid caller can pass an MCP ``reader`` for the read while the write stays on the client.
    """
    effective_client = client
    effective_reader = as_reader(reader)
    require_grounding = bool((plan.get("guardrails") or {}).get("requires_grounding"))
    results: list[OpResult] = []
    for op in plan.get("ops") or []:
        if not isinstance(op, dict):
            continue
        op_id = str(op.get("op_id") or "op")
        if op.get("status") != APPROVED_STATUS:
            results.append(OpResult(op_id, "skipped", reason="Op is not approved."))
            continue
        if require_grounding and op.get("op") in GROUNDING_REQUIRED_OPS:
            gap = op_grounding_gap(op.get("confidence"), op.get("evidence"))
            if gap is not None:
                results.append(OpResult(op_id, "blocked", reason=gap))
                continue
        try:
            validate_op(op)
        except ValueError as exc:
            results.append(OpResult(op_id, "blocked", reason=str(exc)))
            continue
        if effective_client is None:
            effective_client = client_from_env()
        if effective_reader is None:
            effective_reader = as_reader(effective_client)
        try:
            request = _build_request(op, effective_reader)
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
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str | None = None,
    adset_ids: list[str] | None = None,
    name_contains: str | None = None,
) -> dict[str, Any]:
    """Propose set_status=ACTIVE ops for currently-not-active ads, optionally filtered.

    Read-only (reads ads through ``reader``): each op starts ``proposed`` with the ad's current
    effective_status + delivery issues in its note, so the operator/agent approves only the ads
    worth turning on.
    """
    reader = as_reader(reader)
    ads = list(
        reader.iter_paginated(
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


# --- Winning copy library (+ shared metric helpers) -------------------------

from .config import PROJECT_ROOT  # noqa: E402
from .sync_api import (  # noqa: E402
    PURCHASE_KEYS,
    _extract_headline,
    _extract_primary_text,
    _find_metric,
    _infer_creative_type,
    _metric_blob_list,
    _number,
)

KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


def _extract_description(object_story_spec: dict[str, Any], asset_feed_spec: dict[str, Any]) -> str:
    for key in ("link_data", "video_data"):
        section = object_story_spec.get(key)
        if isinstance(section, dict):
            for candidate in ("description", "link_description"):
                value = section.get(candidate)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    descriptions = asset_feed_spec.get("descriptions")
    if isinstance(descriptions, list):
        for d in descriptions:
            if isinstance(d, dict) and isinstance(d.get("text"), str) and d["text"].strip():
                return d["text"].strip()
    return ""


def extract_creative_copy(creative: dict[str, Any]) -> dict[str, Any]:
    """Pull primary text / headline / description / media type from an ad's creative."""
    oss = creative.get("object_story_spec") if isinstance(creative.get("object_story_spec"), dict) else {}
    afs = creative.get("asset_feed_spec") if isinstance(creative.get("asset_feed_spec"), dict) else {}
    return {
        "primary_text": _extract_primary_text(oss, afs),
        "headline": _extract_headline(oss, afs, creative),
        "description": _extract_description(oss, afs),
        "media_type": _infer_creative_type(oss, afs),
    }


def build_copy_library(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    date_from: str,
    date_to: str,
    min_spend: float = 50.0,
    top_n: int = 20,
) -> list[dict[str, Any]]:
    """Rank ads by ROAS over a window and attach their copy — the proven-winner swipe file.

    Read-only (insights + ads via ``reader``). Includes any ad with spend >= min_spend in the
    window (active or paused), so historical winners are captured. Ads with no extractable copy
    are skipped.
    """
    reader = as_reader(reader)
    metrics = {
        str(m["id"]): m
        for m in fetch_entity_metrics(reader, ad_account_id, level="ad", date_from=date_from, date_to=date_to)
    }
    ads = reader.fetch_ads(
        ad_account_id, fields=["id", "name", "creative{object_story_spec,asset_feed_spec,body,title}"]
    )
    rows: list[dict[str, Any]] = []
    for ad in ads:
        m = metrics.get(str(ad.get("id")))
        if not m:
            continue
        roas, spend = m.get("roas"), m.get("spend") or 0.0
        if roas is None or spend < min_spend:
            continue
        copy = extract_creative_copy(ad.get("creative") or {})
        if not (copy["primary_text"] or copy["headline"]):
            continue
        rows.append({
            "ad_id": ad.get("id"), "ad_name": ad.get("name"),
            "roas": roas, "spend": round(spend, 2), "purchases": m.get("purchases"),
            **copy,
        })
    rows.sort(key=lambda r: r["roas"], reverse=True)
    return rows[:top_n]


def render_copy_library_md(account_slug: str, rows: list[dict[str, Any]], *, date_from: str, date_to: str) -> str:
    lines = [
        f"# Winning ad copy — {account_slug}",
        "",
        f"Proven performers ranked by ROAS over **{date_from} → {date_to}** (min-spend filtered). "
        "Regenerate with `copy-library`; git history keeps the record over time.",
        "",
        "**Agent: use these as the base/reference when writing new ad copy** (see "
        "`knowledge/ad_copy_best_practices.md`). Mirror what works here; adapt to the new creative.",
        "",
    ]
    if not rows:
        lines.append("_No qualifying ads yet (need spend + extractable copy in the window)._")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        lines += [
            f"## {i}. {r['ad_name']} — ROAS {r['roas']} (${r['spend']:.0f} spend, {r['purchases'] or 0} purchases, {r['media_type']})",
            f"- **Primary text:** {r['primary_text'] or '(none)'}",
            f"- **Headline:** {r['headline'] or '(none)'}",
            f"- **Description:** {r['description'] or '(none)'}",
            "",
        ]
    return "\n".join(lines)


def default_winning_copy_path(account_slug: str) -> Path:
    return KNOWLEDGE_ROOT / "accounts" / account_slug / "winning_copy.md"


# --- Live performance metrics -----------------------------------------------

_LEVEL_KEYS = {
    "account": ("account_id", "account_name"),
    "campaign": ("campaign_id", "campaign_name"),
    "adset": ("adset_id", "adset_name"),
    "ad": ("ad_id", "ad_name"),
}


def fetch_entity_metrics(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    level: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """Live per-entity performance over a window (one aggregated row per entity).

    Read-only (insights via ``reader``). Returns dicts with id, name, spend, purchase_value,
    roas, purchases, impressions, cost_per_purchase — sorted by spend desc. ``level`` is
    account/campaign/adset/ad.
    """
    if level not in _LEVEL_KEYS:
        raise ValueError(f"level must be one of {sorted(_LEVEL_KEYS)}")
    reader = as_reader(reader)
    idk, namek = _LEVEL_KEYS[level]
    fields = [idk, namek, "spend", "impressions", "actions", "action_values", "purchase_roas"]
    rows = reader.fetch_insights(
        ad_account_id, fields=fields, date_from=date_from, date_to=date_to,
        level=level, time_increment="all_days",
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        spend = _number(r.get("spend")) or 0.0
        value = _find_metric(_metric_blob_list(r.get("action_values")), PURCHASE_KEYS)
        purchases = _find_metric(_metric_blob_list(r.get("actions")), PURCHASE_KEYS)
        roas = (value / spend) if (value is not None and spend) else None
        out.append({
            "id": r.get(idk),
            "name": r.get(namek),
            "spend": round(spend, 2),
            "purchase_value": round(value, 2) if value is not None else None,
            "roas": round(roas, 2) if roas is not None else None,
            "purchases": purchases,
            "impressions": _number(r.get("impressions")),
            "cost_per_purchase": round(spend / purchases, 2) if purchases else None,
        })
    out.sort(key=lambda x: x["spend"], reverse=True)
    return out


def fetch_breakdown_metrics(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    breakdown: str,
    date_from: str,
    date_to: str,
    level: str = "account",
) -> list[dict[str, Any]]:
    """Performance split by a breakdown dimension (age, gender, country, publisher_platform,
    platform_position, impression_device, device_platform, region, ...). Read-only (insights via
    ``reader``). Returns rows with the segment value(s) + spend/value/roas/purchases, sorted by
    spend desc."""
    reader = as_reader(reader)
    breakdowns = [b.strip() for b in breakdown.split(",") if b.strip()]
    rows = reader.fetch_insights(
        ad_account_id, fields=["spend", "impressions", "actions", "action_values"],
        date_from=date_from, date_to=date_to, level=level, time_increment="all_days", breakdowns=breakdowns,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        spend = _number(r.get("spend")) or 0.0
        value = _find_metric(_metric_blob_list(r.get("action_values")), PURCHASE_KEYS)
        purchases = _find_metric(_metric_blob_list(r.get("actions")), PURCHASE_KEYS)
        out.append({
            "segment": {b: r.get(b) for b in breakdowns},
            "spend": round(spend, 2),
            "purchase_value": round(value, 2) if value is not None else None,
            "roas": round(value / spend, 2) if (value is not None and spend) else None,
            "purchases": purchases,
        })
    out.sort(key=lambda x: x["spend"], reverse=True)
    return out


# --- Account-level info ------------------------------------------------------

ACCOUNT_FIELDS = [
    "name", "account_status", "currency", "timezone_name", "amount_spent",
    "spend_cap", "balance", "business_name", "funding_source_details", "disable_reason",
]
_ACCOUNT_STATUS = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW", 9: "IN_GRACE_PERIOD", 101: "CLOSED"}


def account_info(reader: MetaReaderProvider | MetaMarketingApiClient, ad_account_id: str) -> dict[str, Any]:
    """Account-level status, currency, spend, spend cap, balance, funding source (read-only)."""
    reader = as_reader(reader)
    a = reader.get_account(ad_account_id, fields=ACCOUNT_FIELDS)
    status_code = a.get("account_status")
    funding = a.get("funding_source_details") or {}
    return {
        "ad_account_id": ad_account_id,
        "name": a.get("name"),
        "business_name": a.get("business_name"),
        "status": _ACCOUNT_STATUS.get(status_code, status_code),
        "currency": a.get("currency"),
        "timezone": a.get("timezone_name"),
        "amount_spent": a.get("amount_spent"),
        "spend_cap": a.get("spend_cap"),
        "balance": a.get("balance"),
        "funding_source": funding.get("display_string") if isinstance(funding, dict) else funding,
        "disable_reason": a.get("disable_reason"),
    }


# --- Audience sizing / discovery / measurement ------------------------------


def estimate_adset_audience(reader: MetaReaderProvider | MetaMarketingApiClient, adset_id: str) -> dict[str, Any]:
    """Estimated audience size / reach for an ad set's current targeting (read-only)."""
    reader = as_reader(reader)
    payload = reader.get_delivery_estimate(
        adset_id, fields=["estimate_dau", "estimate_mau_lower_bound", "estimate_mau_upper_bound", "estimate_ready"]
    )
    data = payload.get("data") or []
    est = data[0] if data and isinstance(data[0], dict) else {}
    return {
        "adset_id": adset_id,
        "estimate_ready": est.get("estimate_ready"),
        "estimate_dau": est.get("estimate_dau"),
        "mau_lower": est.get("estimate_mau_lower_bound"),
        "mau_upper": est.get("estimate_mau_upper_bound"),
    }


def search_interests(reader: MetaReaderProvider | MetaMarketingApiClient, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Search detailed-targeting interests (id, name, audience size, topic) for use in targeting (read-only)."""
    reader = as_reader(reader)
    rows = reader.search_targeting(query=query, search_type="adinterest", limit=limit)
    return [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "audience_lower": r.get("audience_size_lower_bound"),
            "audience_upper": r.get("audience_size_upper_bound"),
            "topic": r.get("topic"),
            "path": r.get("path"),
        }
        for r in rows
    ]


def list_account_pixels(reader: MetaReaderProvider | MetaMarketingApiClient, ad_account_id: str) -> list[dict[str, Any]]:
    """List the Meta pixels on the account (id, name, last fired, availability) (read-only)."""
    reader = as_reader(reader)
    return reader.list_pixels(ad_account_id, fields=["id", "name", "last_fired_time", "is_unavailable"])


def list_account_conversions(reader: MetaReaderProvider | MetaMarketingApiClient, ad_account_id: str) -> list[dict[str, Any]]:
    """List custom conversions defined on the account (read-only)."""
    reader = as_reader(reader)
    return reader.list_custom_conversions(
        ad_account_id, fields=["id", "name", "custom_event_type", "is_archived", "default_conversion_value"]
    )


# --- Delivery-issue scan ----------------------------------------------------


def scan_issues(reader: MetaReaderProvider | MetaMarketingApiClient, ad_account_id: str) -> dict[str, Any]:
    """Account-wide scan of ad delivery issues, grouped by issue summary (read-only)."""
    reader = as_reader(reader)
    ads = list(
        reader.iter_paginated(f"/{ad_account_id}/ads", params={"fields": ",".join(AD_FIELDS), "limit": 200})
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    for ad in ads:
        for summary in _issue_summaries(ad):
            groups.setdefault(summary, []).append(
                {"id": ad.get("id"), "name": ad.get("name"), "effective_status": ad.get("effective_status")}
            )
    return {
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "ads_scanned": len(ads),
        "ads_with_issues": sum(1 for ad in ads if _issue_summaries(ad)),
        "by_issue": {k: {"count": len(v), "ads": v} for k, v in sorted(groups.items(), key=lambda x: -len(x[1]))},
    }


# --- Custom-audience inventory ----------------------------------------------

AUDIENCE_FIELDS = [
    "id", "name", "subtype", "description", "approximate_count_lower_bound",
    "approximate_count_upper_bound", "operation_status", "time_updated",
]


def list_account_audiences(reader: MetaReaderProvider | MetaMarketingApiClient, ad_account_id: str) -> list[dict[str, Any]]:
    """Inventory of custom audiences in the account (id, name, subtype, size, status) (read-only)."""
    reader = as_reader(reader)
    auds = reader.list_custom_audiences(ad_account_id, fields=AUDIENCE_FIELDS)
    out = []
    for a in auds:
        op = a.get("operation_status") or {}
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "subtype": a.get("subtype"),
            "size_lower": a.get("approximate_count_lower_bound"),
            "size_upper": a.get("approximate_count_upper_bound"),
            "status": op.get("description") if isinstance(op, dict) else op,
        })
    return out


# --- Convenience builder: pause ads -----------------------------------------


def build_pause_plan(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str | None = None,
    adset_ids: list[str] | None = None,
    name_contains: str | None = None,
    roas_below: float | None = None,
    min_spend: float = 0.0,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Propose pausing ACTIVE ads, by name/ad-set filter and/or a performance rule.

    Read-only (ads + optional metrics via ``reader``; only proposes ops). If ``roas_below`` is
    set, pulls live ad-level metrics over [date_from, date_to] and selects ads whose ROAS is
    below the threshold with spend >= ``min_spend``.
    """
    reader = as_reader(reader)
    ads = list(
        reader.iter_paginated(f"/{ad_account_id}/ads", params={"fields": ",".join(AD_FIELDS), "limit": 200})
    )
    scope = set(adset_ids or [])
    candidates = []
    for ad in ads:
        if ad.get("effective_status") != "ACTIVE":
            continue
        if scope and str(ad.get("adset_id")) not in scope:
            continue
        if name_contains and name_contains.lower() not in str(ad.get("name") or "").lower():
            continue
        candidates.append(ad)

    perf: dict[str, dict[str, Any]] = {}
    if roas_below is not None:
        if not (date_from and date_to):
            raise ValueError("roas_below requires date_from and date_to.")
        perf = {str(m["id"]): m for m in fetch_entity_metrics(reader, ad_account_id, level="ad", date_from=date_from, date_to=date_to)}

    ops = []
    for ad in candidates:
        note = "active"
        if roas_below is not None:
            m = perf.get(str(ad.get("id")))
            roas = (m or {}).get("roas")
            spend = (m or {}).get("spend") or 0.0
            if roas is None or roas >= roas_below or spend < min_spend:
                continue
            note = f"ROAS {roas} on ${spend:.0f} spend (< {roas_below} floor)"
        ops.append({
            "op_id": f"pause_ad_{ad.get('id')}",
            "op": "set_status",
            "level": "ad",
            "id": ad.get("id"),
            "name": ad.get("name"),
            "params": {"status": "PAUSED"},
            "status": PROPOSED_STATUS,
            "note": note,
        })
    return {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "pause_ads",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "selection": {"roas_below": roas_below, "min_spend": min_spend, "date_from": date_from, "date_to": date_to},
        "approval_instructions": (
            "Review each ad. To pause it, set its op status to 'approved'. Only approved ops are "
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


def default_metrics_path(account_slug: str, run_date: str, level: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / f"metrics_{level}.json"


def default_diagnose_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "issue_scan.json"


def default_audiences_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "custom_audiences.json"


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
