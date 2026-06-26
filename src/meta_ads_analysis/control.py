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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import account_registry, review
from .confidence import Evidence, EvidenceTier, build_regenerating_query
from .config import (
    CONFIDENCE_CONVERSIONS_FLOOR,
    DEFAULT_REPORTS_ROOT,
    MAX_BUDGET_DECREASE_PERCENT,
    MIN_DAILY_BUDGET_CENTS,
    MIN_SCALING_SPEND,
    MIN_WASTE_SPEND,
)
from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env
from .reader_provider import MetaReaderProvider, as_reader
from .rotation import _audience_refs, _ids, advantage_audience_enabled
from .utils import ensure_dir, write_json
from .write_grounding import attach_op_grounding, op_grounding_gap

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
        return _build_budget_request(op, reader)
    raise ValueError(f"Unhandled op: {op_type}")


# --- Budget ops: CBO detection + symmetric +/- caps -------------------------

# Classification of a ``set_daily_budget`` target after a live re-read (recorded on the op/action as
# ``live_campaign_state``; the caller derives ``cbo_detected`` from it).
BUDGET_ADSET_LEVEL = "adset_level"  # the ad set carries its own daily budget — adjust it directly
BUDGET_CBO_ACTIVE = "cbo_active"  # budget lives on the parent campaign (CBO) — redirect there
BUDGET_BROKEN = "broken"  # neither the ad set nor its campaign has a budget — nothing to cap against

CAMPAIGN_BUDGET_FIELDS = ["id", "daily_budget", "lifetime_budget"]
ADSET_BUDGET_FIELDS = ["id", "daily_budget", "campaign_id"]


def classify_adset_budget(reader: MetaReaderProvider | MetaMarketingApiClient, adset_id: str) -> dict[str, Any]:
    """Re-read an ad set's live budget and classify WHERE its budget actually lives.

    Shared by the ops path (:func:`_build_budget_request`, :func:`build_budget_plan`) and the action
    path (``actions._populate_budget_params_from_live_state``) so both classify the same fixture
    identically (the CBO parity contract). Read-only via the reader. Returns a JSON-serializable dict
    suitable to store on an op/action as ``live_campaign_state``.

    - **adset_level** — the ad set has a positive ``daily_budget``; adjust it directly.
    - **cbo_active** — the ad set has no daily budget but the parent campaign has a daily OR lifetime
      budget (campaign-budget-optimization); the budget must be changed at the campaign. EITHER
      campaign budget type counts as CBO (a daily-budget op can't touch a lifetime budget, but the
      *classification* is still "budget is at the campaign").
    - **broken** — neither the ad set nor its campaign has a budget; nothing to cap against.
    """
    reader = as_reader(reader)
    live = reader.get_adset(adset_id, fields=ADSET_BUDGET_FIELDS)
    adset_daily = _num(live.get("daily_budget"))
    campaign_id = _optional_str(live.get("campaign_id"))
    if adset_daily is not None and adset_daily > 0:
        return {
            "classification": BUDGET_ADSET_LEVEL,
            "adset_id": str(adset_id),
            "adset_daily_budget": adset_daily,
            "campaign_id": campaign_id,
            "campaign_daily_budget": None,
            "campaign_lifetime_budget": None,
        }
    campaign: dict[str, Any] = {}
    if campaign_id:
        campaign = reader.get_campaign(campaign_id, fields=CAMPAIGN_BUDGET_FIELDS)
    campaign_daily = _num(campaign.get("daily_budget"))
    campaign_lifetime = _num(campaign.get("lifetime_budget"))
    cbo = (campaign_daily is not None and campaign_daily > 0) or (
        campaign_lifetime is not None and campaign_lifetime > 0
    )
    return {
        "classification": BUDGET_CBO_ACTIVE if cbo else BUDGET_BROKEN,
        "adset_id": str(adset_id),
        "adset_daily_budget": None,
        "campaign_id": campaign_id,
        "campaign_daily_budget": campaign_daily,
        "campaign_lifetime_budget": campaign_lifetime,
    }


def _resolve_increase_cap(params: dict[str, Any]) -> float:
    """The increase cap, op-param-driven (default 20%). Source intentionally UNCHANGED from the
    original behavior — the decrease path gets its own separate cap, never this one."""
    cap = _num(params.get("max_increase_percent"))
    return 20.0 if cap is None else cap


def _resolve_decrease_cap(params: dict[str, Any]) -> float:
    """The decrease cap: op-param ``max_decrease_percent`` override, else the config default. The
    per-account ``max_budget_decrease_percent`` (registry) is folded into the op-param by
    :func:`build_budget_plan`, so by apply time it is already on the op."""
    cap = _num(params.get("max_decrease_percent"))
    return MAX_BUDGET_DECREASE_PERCENT if cap is None else cap


def _capped_budget_request(new_cents: int, current: float, params: dict[str, Any]) -> dict[str, Any]:
    """Validate a daily-budget change against the live current budget, choosing the cap by the SIGN of
    ``(new - current)``: an increase uses the op-param increase cap; a decrease uses the symmetric
    decrease cap AND the absolute ``MIN_DAILY_BUDGET_CENTS`` floor. Keeping the two caps separate and
    sign-selected means applying the wrong cap can never wrongly block a valid move."""
    if new_cents > current:
        max_increase = _resolve_increase_cap(params)
        if new_cents > current * (1 + max_increase / 100):
            raise ValueError(
                f"set_daily_budget {new_cents} exceeds max increase of {max_increase:.0f}% over "
                f"current {int(current)}."
            )
    elif new_cents < current:
        max_decrease = _resolve_decrease_cap(params)
        if new_cents < current * (1 - max_decrease / 100):
            raise ValueError(
                f"set_daily_budget {new_cents} exceeds max decrease of {max_decrease:.0f}% under "
                f"current {int(current)}."
            )
        if new_cents < MIN_DAILY_BUDGET_CENTS:
            raise ValueError(
                f"set_daily_budget {new_cents} is below the absolute floor of "
                f"{MIN_DAILY_BUDGET_CENTS} cents — refusing to risk pausing delivery."
            )
    return {"daily_budget": str(new_cents)}


def _build_budget_request(op: dict[str, Any], reader: MetaReaderProvider) -> dict[str, Any]:
    """Translate a ``set_daily_budget`` op into the Graph params to POST, re-reading live budget at
    execute time (read-only via the reader). Handles BOTH levels, re-detects CBO on an ad-set op (so a
    campaign that flipped CBO state since propose is caught, not mis-applied), supports increase AND
    decrease (cap selected by sign), and refuses a lifetime-budget campaign."""
    params = op.get("params") or {}
    new_cents = int(_num(params.get("daily_budget_cents")))
    level = op["level"]
    node_id = str(op["id"])

    if level == "campaign":
        live = _get_entity(reader, "campaign", node_id, CAMPAIGN_BUDGET_FIELDS)
        current = _num(live.get("daily_budget"))
        if current is not None and current > 0:
            return _capped_budget_request(new_cents, current, params)
        if (_num(live.get("lifetime_budget")) or 0) > 0:
            raise ValueError(
                "campaign carries a lifetime budget, not a daily budget — not adjustable via a "
                "daily-budget op; edit the lifetime budget directly in Ads Manager."
            )
        raise ValueError(
            "set_daily_budget needs an existing daily budget to cap against (campaign has none); "
            "not changing it."
        )

    # ad-set level: re-detect CBO at execute time, not just at propose time.
    state = classify_adset_budget(reader, node_id)
    classification = state["classification"]
    if classification == BUDGET_ADSET_LEVEL:
        return _capped_budget_request(new_cents, float(state["adset_daily_budget"]), params)
    if classification == BUDGET_CBO_ACTIVE:
        raise ValueError(
            "CBO active: this ad set's budget lives on its parent campaign "
            f"({state.get('campaign_id')}) — change the campaign budget instead "
            "(set_daily_budget at level=campaign), not the ad set."
        )
    raise ValueError(
        "set_daily_budget needs an existing daily budget to cap against (neither the ad set nor its "
        "campaign has one — likely a broken or lifetime-only setup); not changing it."
    )


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


# --- set_status grounding (shared by enable / pause builders) ---------------


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _fmt_roas(value: float | None) -> str:
    return f"ROAS {value:.2f}" if value is not None else "ROAS n/a"


def _fmt_cost_per_install(value: float | None) -> str:
    return f"cost/install ${value:.2f}" if value is not None else "cost/install n/a"


def resolve_action_policy(account_slug: str | None) -> dict[str, Any]:
    """The account's action policy (``primary_goal`` etc.), or ``{}`` when unknown — mirrors
    ``actions._action_policy_for_account`` so control plans pick the SAME goal-based metric as the
    action plan without coupling ``control`` to ``actions``."""
    if not account_slug:
        return {}
    try:
        account = account_registry.resolve_account(
            account_slug, account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH
        )
    except (FileNotFoundError, KeyError, ValueError):
        return {}
    return dict(account.action_policy or {})


def _status_metric(
    metrics_row: dict[str, Any] | None, goal: str | None
) -> tuple[str, float | None, str]:
    """Pick the metric a ``set_status`` decision rests on, mirroring ``actions._select_action_metric``:
    ROAS for ROAS-goal accounts, cost-per-install for install-goal accounts, else whichever is present
    in the window's metrics."""
    row = metrics_row or {}
    roas = _num(row.get("roas"))
    cost_per_install = _num(row.get("cost_per_app_install"))
    if goal == "maximize_in_app_subscriptions":
        return "cost_per_app_install", cost_per_install, _fmt_cost_per_install(cost_per_install)
    if goal == "roas":
        return "blended_roas", roas, _fmt_roas(roas)
    if roas is not None:
        return "blended_roas", roas, _fmt_roas(roas)
    if cost_per_install is not None:
        return "cost_per_app_install", cost_per_install, _fmt_cost_per_install(cost_per_install)
    return "blended_roas", None, _fmt_roas(None)


def _resolve_grounding_window(
    date_from: str | None, date_to: str | None, run_date: str | None
) -> tuple[str, str, int | None, str]:
    """Resolve the evidence window + recency for a set_status plan. Returns
    ``(date_from, date_to, recency_days, run_date_iso)``. ``run_date`` (default today) and the window
    end derive recency the SAME way the producer feeds :func:`confidence.assess` and the way
    ``review`` re-derives it from ``plan["run_date"]`` — so the gate's recompute is faithful."""
    from .sync_api import resolve_date_window

    run_dt = _parse_iso_date(run_date) or date.today()
    resolved_from, resolved_to = resolve_date_window(run_dt, date_from=date_from, date_to=date_to)
    end = _parse_iso_date(resolved_to)
    recency_days = (run_dt - end).days if end is not None else None
    return resolved_from, resolved_to, recency_days, run_dt.isoformat()


def _attach_status_grounding(
    op: dict[str, Any],
    ad: dict[str, Any],
    metrics_row: dict[str, Any] | None,
    *,
    metric_name: str,
    metric_value: float | None,
    metric_display: str,
    account_slug: str | None,
    date_from: str,
    date_to: str,
    recency_days: int | None,
    cold_cites_zero: bool,
) -> None:
    """Attach ``evidence`` + a COMPUTED ``confidence`` band to a ``set_status`` op via the shared
    :func:`write_grounding.attach_op_grounding`. The sample is the entity's own delivery over the
    window; the band is computed (or abstained) — never free-typed.

    ``cold_cites_zero`` decides what "no recent delivery" means, and this is the whole asymmetry
    between turning an ad ON vs OFF:

    - **enable (True):** an ad with no recent insights cites a *zero* purchases/spend sample — an
      honest "this ad spent $0 in the window." Below the floor, ``assess`` abstains, and because the
      sample IS cited the apply-time gate BLOCKS the write: you cannot confidently turn ON an ad with
      no evidence it still works (the cold-ad boundary).
    - **pause (False):** a structural/safety pause with no metric cites NO sample, so the abstain is a
      *structural* abstain the gate ALLOWS — pausing is the conservative, safe direction, and blocking
      it would break PAUSED-by-default safety writes.
    """
    if metrics_row is None and not cold_cites_zero:
        # Structural / safety pause: name WHICH entity over WHAT window, but cite NO sample
        # (sample_*=None). attach_op_grounding then abstains, and because nothing is cited the gate
        # treats it as a structural abstain and ALLOWS it — never a fabricated band.
        evidence: Evidence | None = Evidence(
            metric_name=metric_name,
            metric_value=None,
            metric_display=metric_display,
            window=f"{date_from}..{date_to}" if (date_from or date_to) else "",
            sample_purchases=None,
            sample_spend=None,
            entity_level="ad",
            entity_id=_optional_str(ad.get("id")),
            entity_name=ad.get("name"),
            regenerating_query=build_regenerating_query(account_slug, "ad", date_from, date_to),
        )
    elif metrics_row is None:
        evidence = Evidence(
            metric_name=metric_name,
            metric_value=None,
            metric_display=metric_display,
            window=f"{date_from}..{date_to}",
            sample_purchases=0.0,
            sample_spend=0.0,
            entity_level="ad",
            entity_id=_optional_str(ad.get("id")),
            entity_name=ad.get("name"),
            regenerating_query=build_regenerating_query(account_slug, "ad", date_from, date_to),
        )
    else:
        evidence = Evidence(
            metric_name=metric_name,
            metric_value=metric_value,
            metric_display=metric_display,
            window=f"{date_from}..{date_to}",
            sample_purchases=_num(metrics_row.get("purchases")),
            sample_spend=_num(metrics_row.get("spend")) or 0.0,
            entity_level="ad",
            entity_id=_optional_str(ad.get("id")),
            entity_name=ad.get("name"),
            regenerating_query=build_regenerating_query(account_slug, "ad", date_from, date_to),
        )
    attach_op_grounding(
        op,
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=MIN_WASTE_SPEND,
        conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR,
        recency_days=recency_days,
    )


# --- Convenience builder: enable paused ads ---------------------------------


def build_enable_ads_plan(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str | None = None,
    adset_ids: list[str] | None = None,
    name_contains: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    run_date: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose set_status=ACTIVE ops for currently-not-active ads, optionally filtered.

    Read-only (reads ads + per-ad metrics through ``reader``). Each op starts ``proposed`` and carries
    grounding: an ``evidence`` block (the ad's own performance over [date_from, date_to], metric chosen
    by the account goal) and a **computed** ``confidence`` band. An ad with no recent delivery (a cold
    ad) cites a zero sample → abstains → the apply-time grounding gate refuses to turn it on (keep
    observing). The plan is run through :func:`review.review_ops_plan` before it is returned, so an
    over-claimed or below-floor enable is demoted/marked insufficient before it reaches the operator.
    """
    reader = as_reader(reader)
    policy = policy if policy is not None else resolve_action_policy(account_slug)
    goal = policy.get("primary_goal")
    date_from, date_to, recency_days, run_date_iso = _resolve_grounding_window(
        date_from, date_to, run_date
    )
    ads = list(
        reader.iter_paginated(
            f"/{ad_account_id}/ads",
            params={"fields": ",".join(AD_FIELDS), "limit": 200},
        )
    )
    metrics_by_id = {
        str(m["id"]): m
        for m in fetch_entity_metrics(
            reader, ad_account_id, level="ad", date_from=date_from, date_to=date_to
        )
    }
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
        metrics_row = metrics_by_id.get(str(ad.get("id")))
        metric_name, metric_value, metric_display = _status_metric(metrics_row, goal)
        op = {
            "op_id": f"enable_ad_{ad.get('id')}",
            "op": "set_status",
            "level": "ad",
            "id": ad.get("id"),
            "name": ad.get("name"),
            "params": {"status": "ACTIVE"},
            "status": PROPOSED_STATUS,
            "note": f"currently {ad.get('effective_status')}; issues: {'; '.join(issues) or 'none'}",
        }
        _attach_status_grounding(
            op,
            ad,
            metrics_row,
            metric_name=metric_name,
            metric_value=metric_value,
            metric_display=metric_display,
            account_slug=account_slug,
            date_from=date_from,
            date_to=date_to,
            recency_days=recency_days,
            cold_cites_zero=True,
        )
        ops.append(op)
    plan = {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "enable_ads",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "run_date": run_date_iso,
        "account_action_policy": policy,
        "selection": {"date_from": date_from, "date_to": date_to},
        "approval_instructions": (
            "Review each ad. To enable it, set its op status to 'approved'. Only approved ops are "
            "sent to Meta, and only with --execute (or tested with --validate-only). An enable with a "
            "below-floor / no-delivery sample abstains and is blocked until the ad shows it works."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "requires_grounding": True,
            "statuses": sorted(ALLOWED_STATUSES),
        },
        "ops": ops,
    }
    return review.review_ops_plan(plan)


# --- Winning copy library (+ shared metric helpers) -------------------------

from .config import PROJECT_ROOT  # noqa: E402
from .sync_api import (  # noqa: E402
    APP_INSTALL_KEYS,
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
        app_installs = _find_metric(_metric_blob_list(r.get("actions")), APP_INSTALL_KEYS)
        roas = (value / spend) if (value is not None and spend) else None
        out.append({
            "id": r.get(idk),
            "name": r.get(namek),
            "spend": round(spend, 2),
            "purchase_value": round(value, 2) if value is not None else None,
            "roas": round(roas, 2) if roas is not None else None,
            "purchases": purchases,
            "app_installs": app_installs,
            "cost_per_app_install": round(spend / app_installs, 2) if app_installs else None,
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
    run_date: str | None = None,
) -> dict[str, Any]:
    """Propose pausing ACTIVE ads, by name/ad-set filter and/or a performance rule.

    Read-only (ads + optional metrics via ``reader``; only proposes ops). If ``roas_below`` is
    set, pulls live ad-level metrics over [date_from, date_to] and selects ads whose ROAS is
    below the threshold with spend >= ``min_spend``.

    Each op carries grounding. A ``roas_below`` pause cites the ad's ROAS over the window (a computed
    band). A purely structural pause (name/ad-set filter, no metric) cites NO sample → an honest
    *structural* abstain the apply-time gate allows, because pausing is the conservative direction (the
    no-metric policy: never fabricate a band for a safety pause). The plan is run through
    :func:`review.review_ops_plan` before it is returned.
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

    run_dt = _parse_iso_date(run_date) or date.today()
    end = _parse_iso_date(date_to)
    recency_days = (run_dt - end).days if end is not None else None
    window_from, window_to = date_from or "", date_to or ""

    ops = []
    for ad in candidates:
        note = "active"
        metrics_row: dict[str, Any] | None = None
        if roas_below is not None:
            m = perf.get(str(ad.get("id")))
            roas = (m or {}).get("roas")
            spend = (m or {}).get("spend") or 0.0
            if roas is None or roas >= roas_below or spend < min_spend:
                continue
            note = f"ROAS {roas} on ${spend:.0f} spend (< {roas_below} floor)"
            metrics_row = m
        op = {
            "op_id": f"pause_ad_{ad.get('id')}",
            "op": "set_status",
            "level": "ad",
            "id": ad.get("id"),
            "name": ad.get("name"),
            "params": {"status": "PAUSED"},
            "status": PROPOSED_STATUS,
            "note": note,
        }
        # A roas_below pause rests on ROAS by construction (it is how the ad was selected); a
        # structural pause has no metric → no sample cited (structural abstain, gate-allowed).
        _attach_status_grounding(
            op,
            ad,
            metrics_row,
            metric_name="blended_roas",
            metric_value=_num((metrics_row or {}).get("roas")),
            metric_display=_fmt_roas(_num((metrics_row or {}).get("roas"))),
            account_slug=account_slug,
            date_from=window_from,
            date_to=window_to,
            recency_days=recency_days,
            cold_cites_zero=False,
        )
        ops.append(op)
    plan = {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "pause_ads",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "run_date": run_dt.isoformat(),
        "selection": {"roas_below": roas_below, "min_spend": min_spend, "date_from": date_from, "date_to": date_to},
        "approval_instructions": (
            "Review each ad. To pause it, set its op status to 'approved'. Only approved ops are "
            "sent to Meta, and only with --execute (or tested with --validate-only)."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "requires_grounding": True,
            "statuses": sorted(ALLOWED_STATUSES),
        },
        "ops": ops,
    }
    return review.review_ops_plan(plan)


# --- Convenience builder: CBO-aware budget +/- ------------------------------

# Action-type tags for the four budget moves. Ops carry no ``action_type`` by default, so the review
# gate's ``direction`` check (scale-up below target / scale-down of a clear winner) cannot fire on a
# bare op. Budget ops set one of these so the gate's direction refutation works (see
# ``review._SCALE_ACTIONS`` / ``review._SCALE_DOWN_BUDGET_ACTIONS``).
def _budget_action_type(level: str, new_cents: int, current: float | None) -> str:
    increasing = current is None or new_cents >= current
    if level == "campaign":
        return "increase_campaign_budget" if increasing else "decrease_campaign_budget"
    return "increase_adset_budget" if increasing else "decrease_adset_budget"


def _budget_op(
    *,
    level: str,
    node_id: str,
    new_cents: int,
    current: float | None,
    max_increase_percent: float | None,
    max_decrease_percent: float | None,
    note: str | None = None,
) -> dict[str, Any]:
    """A bare (un-grounded) ``set_daily_budget`` op. Caps flow through op-params so the apply-time
    re-read in :func:`_build_budget_request` enforces them; ``current`` only decides the direction tag
    (the real cap is checked against the live re-read, never this propose-time snapshot)."""
    params: dict[str, Any] = {"daily_budget_cents": int(new_cents)}
    if max_increase_percent is not None:
        params["max_increase_percent"] = max_increase_percent
    if max_decrease_percent is not None:
        params["max_decrease_percent"] = max_decrease_percent
    action_type = _budget_action_type(level, new_cents, current)
    direction = "increase" if action_type.startswith("increase") else "decrease"
    return {
        "op_id": f"{action_type}_{node_id}",
        "op": "set_daily_budget",
        "level": level,
        "id": node_id,
        "action_type": action_type,
        "params": params,
        "status": PROPOSED_STATUS,
        "note": note or f"{direction} daily budget to {new_cents} cents",
    }


def _attach_budget_grounding(
    op: dict[str, Any],
    reader: MetaReaderProvider,
    ad_account_id: str,
    *,
    level: str,
    entity_id: str,
    goal: str | None,
    account_slug: str | None,
    date_from: str,
    date_to: str,
    recency_days: int | None,
) -> None:
    """Attach the budget move's grounding: the entity's OWN metric (ROAS / cost-per-install by goal)
    over the window, as a cited sample, plus a computed band. An entity with no delivery in the window
    cites a ZERO sample → abstain → the apply-time gate blocks the swing (no confident budget move on
    thin/absent data — the '9 purchases over 5 days' guard). Each op grounds on its own level's metric,
    so a CBO redirect's campaign op carries CAMPAIGN evidence, never a copy of the ad set's."""
    rows = fetch_entity_metrics(reader, ad_account_id, level=level, date_from=date_from, date_to=date_to)
    row = next((m for m in rows if str(m.get("id")) == str(entity_id)), None)
    metric_name, metric_value, metric_display = _status_metric(row, goal)
    window = f"{date_from}..{date_to}"
    if row is None:
        evidence = Evidence(
            metric_name=metric_name, metric_value=None, metric_display=metric_display, window=window,
            sample_purchases=0.0, sample_spend=0.0, entity_level=level,
            entity_id=_optional_str(entity_id), entity_name=None,
            regenerating_query=build_regenerating_query(account_slug, level, date_from, date_to),
        )
    else:
        evidence = Evidence(
            metric_name=metric_name, metric_value=metric_value, metric_display=metric_display,
            window=window, sample_purchases=_num(row.get("purchases")),
            sample_spend=_num(row.get("spend")) or 0.0, entity_level=level,
            entity_id=_optional_str(entity_id), entity_name=row.get("name"),
            regenerating_query=build_regenerating_query(account_slug, level, date_from, date_to),
        )
    attach_op_grounding(
        op, evidence=evidence, tier=EvidenceTier.direct_observation,
        spend_floor=MIN_SCALING_SPEND, conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR,
        recency_days=recency_days,
    )


def _build_campaign_budget_ops(
    reader: MetaReaderProvider,
    ad_account_id: str,
    campaign_id: str,
    new_cents: int,
    *,
    goal: str | None,
    account_slug: str | None,
    date_from: str,
    date_to: str,
    recency_days: int | None,
    max_increase_percent: float | None,
    max_decrease_percent: float | None,
    cbo_origin_adset_id: str | None = None,
) -> list[dict[str, Any]]:
    live = reader.get_campaign(campaign_id, fields=CAMPAIGN_BUDGET_FIELDS)
    campaign_daily = _num(live.get("daily_budget"))
    campaign_lifetime = _num(live.get("lifetime_budget"))
    note = None
    if cbo_origin_adset_id:
        note = f"Campaign budget op redirected from ad set {cbo_origin_adset_id} (CBO active)."
    op = _budget_op(
        level="campaign", node_id=str(campaign_id), new_cents=new_cents, current=campaign_daily,
        max_increase_percent=max_increase_percent, max_decrease_percent=max_decrease_percent, note=note,
    )
    if cbo_origin_adset_id:
        op["cbo_redirect_from_adset_id"] = str(cbo_origin_adset_id)
    if (campaign_daily is None or campaign_daily <= 0) and (campaign_lifetime or 0) > 0:
        # Lifetime-budget campaign: a daily-budget op can't touch it. Surface it; it is blocked at
        # apply by _build_budget_request's lifetime guard.
        op["budget_type"] = "lifetime"
        op["note"] = (
            (op.get("note") or "")
            + " Campaign uses a LIFETIME budget — not adjustable via a daily-budget op; "
            "non-executable (blocked at apply)."
        ).strip()
    _attach_budget_grounding(
        op, reader, ad_account_id, level="campaign", entity_id=str(campaign_id), goal=goal,
        account_slug=account_slug, date_from=date_from, date_to=date_to, recency_days=recency_days,
    )
    return [op]


def _build_adset_budget_ops(
    reader: MetaReaderProvider,
    ad_account_id: str,
    adset_id: str,
    new_cents: int,
    *,
    goal: str | None,
    account_slug: str | None,
    date_from: str,
    date_to: str,
    recency_days: int | None,
    max_increase_percent: float | None,
    max_decrease_percent: float | None,
) -> list[dict[str, Any]]:
    state = classify_adset_budget(reader, adset_id)
    classification = state["classification"]

    if classification == BUDGET_ADSET_LEVEL:
        op = _budget_op(
            level="adset", node_id=str(adset_id), new_cents=new_cents,
            current=state["adset_daily_budget"], max_increase_percent=max_increase_percent,
            max_decrease_percent=max_decrease_percent,
        )
        _attach_budget_grounding(
            op, reader, ad_account_id, level="adset", entity_id=str(adset_id), goal=goal,
            account_slug=account_slug, date_from=date_from, date_to=date_to, recency_days=recency_days,
        )
        return [op]

    if classification == BUDGET_CBO_ACTIVE:
        # Non-executable ad-set pointer op + actionable campaign op. The pointer carries the CBO
        # classification for the operator/audit log and is blocked at apply (_build_budget_request);
        # the campaign op carries its OWN campaign-level evidence (never a copy of the ad set's).
        pointer = _budget_op(
            level="adset", node_id=str(adset_id), new_cents=new_cents, current=None,
            max_increase_percent=max_increase_percent, max_decrease_percent=max_decrease_percent,
            note=(
                "CBO active: budget is at the campaign — increase/decrease the campaign budget "
                "instead. This ad-set op is non-executable (blocked at apply); approve the campaign "
                "op below."
            ),
        )
        pointer["cbo_detected"] = True
        pointer["live_campaign_state"] = state
        _attach_budget_grounding(
            pointer, reader, ad_account_id, level="adset", entity_id=str(adset_id), goal=goal,
            account_slug=account_slug, date_from=date_from, date_to=date_to, recency_days=recency_days,
        )
        ops = [pointer]
        campaign_id = state.get("campaign_id")
        if campaign_id:
            ops.extend(_build_campaign_budget_ops(
                reader, ad_account_id, str(campaign_id), new_cents, goal=goal, account_slug=account_slug,
                date_from=date_from, date_to=date_to, recency_days=recency_days,
                max_increase_percent=max_increase_percent, max_decrease_percent=max_decrease_percent,
                cbo_origin_adset_id=str(adset_id),
            ))
        return ops

    # broken: neither the ad set nor its campaign has a budget. Surface a non-executable op (blocked
    # at apply) so the operator sees the situation rather than a silent no-op.
    op = _budget_op(
        level="adset", node_id=str(adset_id), new_cents=new_cents, current=None,
        max_increase_percent=max_increase_percent, max_decrease_percent=max_decrease_percent,
        note=(
            "No daily budget on the ad set or its campaign (broken or lifetime-only setup) — "
            "non-executable; blocked at apply."
        ),
    )
    op["live_campaign_state"] = state
    _attach_budget_grounding(
        op, reader, ad_account_id, level="adset", entity_id=str(adset_id), goal=goal,
        account_slug=account_slug, date_from=date_from, date_to=date_to, recency_days=recency_days,
    )
    return [op]


def build_budget_plan(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    new_daily_budget_cents: int,
    adset_id: str | None = None,
    campaign_id: str | None = None,
    account_slug: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    run_date: str | None = None,
    policy: dict[str, Any] | None = None,
    max_increase_percent: float | None = None,
    max_decrease_percent: float | None = None,
) -> dict[str, Any]:
    """Propose a grounded, CBO-aware ``set_daily_budget`` op for ONE entity (an ad set OR a campaign).

    Read-only: reads live budget + the entity's metric through ``reader``; only proposes ops. The move
    may be an increase OR a decrease — the direction is inferred from the live current budget, and the
    apply-time gate picks the cap by sign (increase cap vs the symmetric decrease cap + absolute floor).

    Targeting an **ad set** whose budget lives on its campaign (CBO) yields TWO ops: a non-executable
    ad-set pointer (marked ``cbo_detected``) and an actionable campaign-level op carrying its own
    campaign metric as evidence. Every op carries ``evidence`` + a computed ``confidence`` band and the
    plan is run through :func:`review.review_ops_plan` before return, so a below-floor sample abstains
    into a non-executable "keep running" recommendation and a scale-up below the ROAS target is refuted.
    """
    reader = as_reader(reader)
    if bool(adset_id) == bool(campaign_id):
        raise ValueError("build_budget_plan requires exactly one of adset_id or campaign_id.")
    policy = policy if policy is not None else resolve_action_policy(account_slug)
    goal = policy.get("primary_goal")
    date_from, date_to, recency_days, run_date_iso = _resolve_grounding_window(date_from, date_to, run_date)
    new_cents = int(new_daily_budget_cents)
    # Fold the per-account decrease override into the op-param so the apply-time cap honors it without
    # control._build_budget_request needing the registry.
    if max_decrease_percent is None:
        max_decrease_percent = _num(policy.get("max_budget_decrease_percent"))

    common = {
        "goal": goal, "account_slug": account_slug, "date_from": date_from, "date_to": date_to,
        "recency_days": recency_days, "max_increase_percent": max_increase_percent,
        "max_decrease_percent": max_decrease_percent,
    }
    if campaign_id:
        ops = _build_campaign_budget_ops(reader, ad_account_id, str(campaign_id), new_cents, **common)
    else:
        ops = _build_adset_budget_ops(reader, ad_account_id, str(adset_id), new_cents, **common)

    plan = {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "set_budget",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "run_date": run_date_iso,
        "account_action_policy": policy,
        "selection": {
            "date_from": date_from, "date_to": date_to, "new_daily_budget_cents": new_cents,
            "adset_id": adset_id, "campaign_id": campaign_id,
        },
        "approval_instructions": (
            "Review each op. To apply it, set its status to 'approved'. Only approved ops are sent to "
            "Meta, and only with --execute (or tested with --validate-only). Under CBO, approve the "
            "campaign op (the ad-set op is a non-executable pointer). A below-floor sample abstains "
            "and is blocked until the data clears the significance floor."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "requires_grounding": True,
        },
        "ops": ops,
    }
    return review.review_ops_plan(plan)


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
