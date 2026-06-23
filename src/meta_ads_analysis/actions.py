"""Action planning and guarded Meta Graph API execution.

Writes (pause_ad, increase_adset_budget) and live-state reads go through
``MetaMarketingApiClient``. Executing actions requires an ``ads_management``-scoped
``META_ACCESS_TOKEN``; live-state reads and dry runs only need ``ads_read``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env
from .utils import ensure_dir, write_json

APPROVED_STATUS = "approved"
EXECUTED_STATUS = "executed"
PROPOSED_STATUS = "proposed"
SUPPORTED_EXECUTABLE_ACTIONS = {"increase_adset_budget", "pause_ad"}
META_AI_DISABLED_POLICY = {
    "keep_disabled": [
        "Advantage+ creative enhancements",
        "automatic text variations",
        "image expansion",
        "visual touch-ups",
        "music generation",
        "flexible media or AI-generated creative variants",
    ],
    "execution_note": (
        "The executor only changes explicit status/budget fields in approved actions. "
        "It does not enable Meta AI or Advantage+ creative features."
    ),
}


@dataclass(slots=True)
class ApplyResult:
    action_id: str
    status: str
    request: dict[str, Any] | None
    response: dict[str, Any] | None = None
    reason: str | None = None


def find_latest_report_run(account_slug: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> str:
    account_dir = reports_root / account_slug
    if not account_dir.exists():
        raise FileNotFoundError(f"No reports directory found for account: {account_slug}")
    candidates = sorted(
        path.name
        for path in account_dir.iterdir()
        if path.is_dir() and _looks_like_date(path.name)
    )
    if not candidates:
        raise FileNotFoundError(f"No date-named report runs found for account: {account_slug}")
    return candidates[-1]


def default_action_plan_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "action_plan.json"


def default_action_results_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"action_results_{timestamp}.json"


def load_report_payload(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> dict[str, Any]:
    report_path = reports_root / account_slug / run_date / "meta_ads_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Report JSON not found: {report_path}")
    import json

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Report JSON did not contain an object: {report_path}")
    return payload


def build_action_plan(payload: dict[str, Any]) -> dict[str, Any]:
    account_slug = payload.get("account_slug")
    run_date = payload.get("run_date")
    if not account_slug or not run_date:
        raise ValueError("Report payload must include account_slug and run_date.")
    policy = _action_policy_for_account(str(account_slug))

    actions: list[dict[str, Any]] = []
    seen_action_ids: set[str] = set()
    for ad in payload.get("budget_waste") or []:
        should_pause, pause_reason = _should_pause_ad(ad, policy)
        if not should_pause:
            continue
        ad_id = str(ad.get("ad_id") or "").strip()
        if not ad_id:
            actions.append(_manual_action("review_waste_without_ad_id", ad, "High waste finding has no ad_id."))
            continue
        action = {
            "action_id": f"pause_ad_{ad_id}",
            "action_type": "pause_ad",
            "status": PROPOSED_STATUS,
            "executable": True,
            "approval_required": True,
            "target": {
                "type": "ad",
                "id": ad_id,
                "name": ad.get("ad_name"),
                "campaign_name": ad.get("campaign_name"),
                "adset_name": ad.get("adset_name"),
            },
            "params": {"status": "paused"},
            "rationale": (
                f"{pause_reason}: spent ${ad.get('total_spend', 0):.2f}, "
                f"results {ad.get('total_results')}, app installs {ad.get('total_app_installs')}, "
                f"waste score {ad.get('waste_score')}."
            ),
            "evidence": {
                "waste_score": ad.get("waste_score"),
                "waste_status": ad.get("waste_status"),
                "waste_reasons": ad.get("waste_reasons") or [],
                "tracking_confidence": ad.get("tracking_confidence"),
            },
        }
        _append_once(actions, seen_action_ids, action)

    for ad in payload.get("fatigue_findings") or []:
        if ad.get("fatigue_status") not in {"high", "medium"}:
            continue
        action = _manual_action(
            "refresh_creative",
            ad,
            "Fatigue finding should be handled by refreshing, rotating, or rebuilding creative before more spend is added.",
        )
        _append_once(actions, seen_action_ids, action)

    for ad in payload.get("scaling_candidates") or []:
        action = _manual_action(
            "consider_scale_budget",
            ad,
            "Scaling candidate needs an operator-selected budget amount before it can become executable.",
        )
        _append_once(actions, seen_action_ids, action)
        budget_action = _build_budget_increase_action(ad, policy)
        if budget_action is not None:
            _append_once(actions, seen_action_ids, budget_action)

    for index, concern in enumerate(payload.get("tracking_concerns") or [], start=1):
        action = {
            "action_id": f"measurement_review_{index}",
            "action_type": "measurement_review",
            "status": PROPOSED_STATUS,
            "executable": False,
            "approval_required": False,
            "target": {"type": "account", "id": account_slug, "name": account_slug},
            "params": {},
            "rationale": concern,
            "evidence": {},
        }
        _append_once(actions, seen_action_ids, action)

    return {
        "schema_version": 1,
        "account_slug": account_slug,
        "run_date": run_date,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_report": f"reports/{account_slug}/{run_date}/meta_ads_report.json",
        "approval_instructions": (
            "Review each action. To allow execution, set status to 'approved'. "
            "Only executable actions with approved status are sent to the Meta Graph API."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "supported_executable_actions": sorted(SUPPORTED_EXECUTABLE_ACTIONS),
            "meta_ai_features": META_AI_DISABLED_POLICY,
        },
        "account_action_policy": policy,
        "actions": actions,
    }


AD_STATE_FIELDS = ["id", "name", "status", "effective_status", "adset_id", "campaign_id", "updated_time"]
ADSET_STATE_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "campaign_id",
    "daily_budget",
    "lifetime_budget",
    "updated_time",
    "targeting",
]


def enrich_action_plan_with_live_state(
    plan: dict[str, Any],
    *,
    client: MetaMarketingApiClient | None = None,
) -> dict[str, Any]:
    effective_client = client or client_from_env()
    enriched = {**plan, "actions": [dict(action) for action in plan.get("actions") or []]}
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for action in enriched["actions"]:
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        if not target.get("id"):
            continue
        live_state: dict[str, Any] = {}
        if target.get("type") == "ad":
            try:
                live_state = fetch_live_ad_state(str(target["id"]), client=effective_client)
            except MetaApiError as exc:
                action["live_state"] = {
                    "checked_at": checked_at,
                    "lookup_status": "failed",
                    "error": _redact_sensitive_text(str(exc)),
                }
                continue
            action["live_state"] = {"checked_at": checked_at, "lookup_status": "ok", **live_state}
            if action.get("action_type") == "pause_ad" and _live_state_is_paused(live_state):
                action["status"] = "already_resolved"
                action["executable"] = False
                action["approval_required"] = False
                action["rationale"] = f"{action.get('rationale', '').rstrip()} Live Meta state already shows this ad is paused."
        if target.get("type") == "adset" or live_state.get("adset_id"):
            _maybe_add_live_adset_state(action, live_state, checked_at, effective_client)
            _populate_budget_params_from_live_state(action)
    _append_meta_ai_remediation_actions(enriched, checked_at)
    enriched["live_state_enriched_at"] = checked_at
    return enriched


def fetch_live_ad_state(
    ad_id: str,
    *,
    client: MetaMarketingApiClient,
) -> dict[str, Any]:
    item = client.get_ad(ad_id, fields=AD_STATE_FIELDS)
    return {
        "ad_id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "effective_status": item.get("effective_status"),
        "adset_id": item.get("adset_id"),
        "campaign_id": item.get("campaign_id"),
        "updated_time": item.get("updated_time"),
    }


def fetch_live_adset_state(
    adset_id: str,
    *,
    client: MetaMarketingApiClient,
) -> dict[str, Any]:
    item = client.get_adset(adset_id, fields=ADSET_STATE_FIELDS)
    targeting = item.get("targeting")
    return {
        "adset_id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "effective_status": item.get("effective_status"),
        "campaign_id": item.get("campaign_id"),
        "daily_budget": item.get("daily_budget"),
        "lifetime_budget": item.get("lifetime_budget"),
        "updated_time": item.get("updated_time"),
        "targeting_automation_detected": _targeting_automation_detected(targeting),
        "targeting_automation_excerpt": _automation_excerpt(targeting),
    }


def write_action_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def apply_action_plan(
    plan: dict[str, Any],
    *,
    execute: bool,
    client: MetaMarketingApiClient | None = None,
) -> list[ApplyResult]:
    effective_client = client
    results: list[ApplyResult] = []
    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("action_id") or "unknown_action")
        if not action.get("executable"):
            results.append(ApplyResult(action_id, "skipped", None, reason="Action is not executable."))
            continue
        if action.get("status") != APPROVED_STATUS:
            results.append(ApplyResult(action_id, "skipped", None, reason="Action is not approved."))
            continue
        try:
            operation = build_api_operation(action)
        except ValueError as exc:
            results.append(ApplyResult(action_id, "blocked", None, reason=str(exc)))
            continue
        if not execute:
            results.append(ApplyResult(action_id, "dry_run", operation))
            continue
        if effective_client is None:
            effective_client = client_from_env()
        try:
            response = _execute_api_operation(operation, effective_client)
        except MetaApiError as exc:
            results.append(
                ApplyResult(action_id, "failed", operation, reason=_redact_sensitive_text(str(exc)))
            )
            continue
        results.append(ApplyResult(action_id, EXECUTED_STATUS, operation, response=response))
    return results


def _execute_api_operation(
    operation: dict[str, Any],
    client: MetaMarketingApiClient,
) -> dict[str, Any]:
    resource = operation.get("resource")
    node_id = str(operation.get("id") or "")
    params = operation.get("params") if isinstance(operation.get("params"), dict) else {}
    if resource == "ad":
        return client.update_ad(node_id, params=params)
    if resource == "adset":
        return client.update_adset(node_id, params=params)
    raise MetaApiError(f"Unknown API operation resource: {resource}")


def write_apply_results(
    *,
    plan: dict[str, Any],
    results: list[ApplyResult],
    output_path: Path,
    execute: bool,
) -> Path:
    payload = {
        "schema_version": 1,
        "account_slug": plan.get("account_slug"),
        "run_date": plan.get("run_date"),
        "executed": execute,
        "transport": "graph_api",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "results": [
            {
                "action_id": item.action_id,
                "status": item.status,
                "request": item.request,
                "response": item.response,
                "reason": item.reason,
            }
            for item in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path


def build_api_operation(action: dict[str, Any]) -> dict[str, Any]:
    """Validate an approved action and return the Graph API operation to perform.

    The operation is a transport-agnostic description: ``{"resource", "id", "params"}``,
    where ``params`` are the exact node fields to POST. This is also what a dry run records.
    """
    action_type = str(action.get("action_type") or "")
    if action_type not in SUPPORTED_EXECUTABLE_ACTIONS:
        raise ValueError(f"Unsupported executable action_type: {action_type}")
    _enforce_no_meta_ai_params(action)
    if action_type == "pause_ad":
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        ad_id = str(target.get("id") or "").strip()
        status = str((action.get("params") or {}).get("status") or "").strip().lower()
        if not ad_id:
            raise ValueError("pause_ad action is missing target.id.")
        if status != "paused":
            raise ValueError("pause_ad action must set params.status to 'paused'.")
        return {"resource": "ad", "id": ad_id, "params": {"status": "PAUSED"}}
    if action_type == "increase_adset_budget":
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        adset_id = str(target.get("id") or "").strip()
        current_budget = _number(params.get("current_daily_budget_cents"))
        new_budget = _number(params.get("new_daily_budget_cents"))
        max_increase_percent = _number(params.get("max_increase_percent")) or 20
        if not adset_id:
            raise ValueError("increase_adset_budget action is missing target.id.")
        if current_budget is None or current_budget <= 0:
            raise ValueError("increase_adset_budget requires params.current_daily_budget_cents.")
        if new_budget is None or new_budget <= current_budget:
            raise ValueError("increase_adset_budget requires a new daily budget above the current budget.")
        max_allowed = current_budget * (1 + (max_increase_percent / 100))
        if new_budget > max_allowed:
            raise ValueError(
                f"increase_adset_budget exceeds max increase of {max_increase_percent:.0f}%."
            )
        return {"resource": "adset", "id": adset_id, "params": {"daily_budget": str(int(round(new_budget)))}}
    raise ValueError(f"Unhandled action_type: {action_type}")


def _append_once(actions: list[dict[str, Any]], seen_action_ids: set[str], action: dict[str, Any]) -> None:
    action_id = str(action.get("action_id") or "")
    if action_id in seen_action_ids:
        return
    seen_action_ids.add(action_id)
    actions.append(action)


def _manual_action(action_type: str, ad: dict[str, Any], rationale: str) -> dict[str, Any]:
    ad_id = str(ad.get("ad_id") or ad.get("ad_name") or "unknown").strip().replace(" ", "_")
    return {
        "action_id": f"{action_type}_{ad_id}",
        "action_type": action_type,
        "status": PROPOSED_STATUS,
        "executable": False,
        "approval_required": False,
        "target": {
            "type": "ad",
            "id": ad.get("ad_id"),
            "name": ad.get("ad_name"),
            "campaign_id": ad.get("campaign_id"),
            "campaign_name": ad.get("campaign_name"),
            "adset_id": ad.get("adset_id"),
            "adset_name": ad.get("adset_name"),
        },
        "params": {},
        "rationale": rationale,
        "evidence": {
            "waste_score": ad.get("waste_score"),
            "fatigue_score": ad.get("fatigue_score"),
            "scaling_score": ad.get("scaling_score"),
            "tracking_confidence": ad.get("tracking_confidence"),
        },
    }


def _enforce_no_meta_ai_params(action: dict[str, Any]) -> None:
    params = action.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("Action params must be an object.")
    forbidden_fragments = ("advantage", "ai_", "creative_enhancement", "image_expansion", "text_variation", "flexible")
    for key, value in params.items():
        probe = f"{key} {value}".lower()
        if any(fragment in probe for fragment in forbidden_fragments):
            raise ValueError("Action attempts to set a Meta AI or Advantage+ creative parameter.")


def _live_state_is_paused(live_state: dict[str, Any]) -> bool:
    return str(live_state.get("status") or "").upper() == "PAUSED"


def _action_policy_for_account(account_slug: str) -> dict[str, Any]:
    try:
        account = account_registry.resolve_account(
            account_slug,
            account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH,
        )
    except (FileNotFoundError, KeyError, ValueError):
        return {}
    return dict(account.action_policy or {})


def _should_pause_ad(ad: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, str]:
    if ad.get("waste_status") == "high":
        return True, "High waste risk"
    if ad.get("waste_status") != "medium":
        return False, ""
    goal = policy.get("primary_goal")
    if goal == "maximize_in_app_subscriptions":
        installs_target = _number(policy.get("pause_if_no_primary_and_secondary_cost_above"))
        if (
            (ad.get("total_results") in (None, 0, 0.0))
            and installs_target is not None
            and _number(ad.get("cost_per_app_install")) is not None
            and _number(ad.get("cost_per_app_install")) > installs_target
        ):
            return True, f"Medium waste risk and app-install fallback is above ${installs_target:.2f} target"
    if goal == "roas":
        roas_floor = _number(policy.get("pause_roas_floor"))
        roas = _number(ad.get("blended_roas"))
        if roas_floor is not None and roas is not None and roas < roas_floor:
            return True, f"Medium waste risk and ROAS is below {roas_floor:.2f} floor"
    return False, ""


def _build_budget_increase_action(ad: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any] | None:
    qualifies, reason = _qualifies_for_budget_increase(ad, policy)
    if not qualifies:
        return None
    adset_id = str(ad.get("adset_id") or "").strip()
    ad_id = str(ad.get("ad_id") or "").strip()
    if not adset_id:
        return None
    max_increase = int(_number(policy.get("max_budget_increase_percent")) or 20)
    return {
        "action_id": f"increase_adset_budget_{adset_id}",
        "action_type": "increase_adset_budget",
        "status": PROPOSED_STATUS,
        "executable": False,
        "approval_required": False,
        "target": {
            "type": "adset",
            "id": adset_id,
            "name": ad.get("adset_name"),
            "campaign_id": ad.get("campaign_id"),
            "campaign_name": ad.get("campaign_name"),
            "source_ad_id": ad_id,
            "source_ad_name": ad.get("ad_name"),
        },
        "params": {
            "max_increase_percent": max_increase,
            "new_daily_budget_cents": None,
        },
        "rationale": (
            f"{reason}. Operator must set new_daily_budget_cents before execution; "
            f"do not increase more than {max_increase}%."
        ),
        "evidence": {
            "scaling_score": ad.get("scaling_score"),
            "total_spend": ad.get("total_spend"),
            "total_results": ad.get("total_results"),
            "cost_per_result": ad.get("cost_per_result"),
            "cost_per_app_install": ad.get("cost_per_app_install"),
            "blended_roas": ad.get("blended_roas"),
            "fatigue_score": ad.get("fatigue_score"),
        },
    }


def _qualifies_for_budget_increase(ad: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, str]:
    if not ad.get("scaling_candidate"):
        return False, ""
    goal = policy.get("primary_goal")
    if goal == "roas":
        roas_floor = _number(policy.get("scale_roas_floor")) or _number(policy.get("target_roas")) or 3.0
        roas = _number(ad.get("blended_roas"))
        if roas is not None and roas >= roas_floor:
            return True, f"ROAS {roas:.2f} meets scale floor of {roas_floor:.2f}"
    if goal == "maximize_in_app_subscriptions":
        min_results = _number(policy.get("scale_if_primary_results_at_least")) or 1
        if (_number(ad.get("total_results")) or 0) >= min_results:
            return True, f"Recorded subscriptions meet scale threshold of {min_results:.0f}+"
    return False, ""


def _maybe_add_live_adset_state(
    action: dict[str, Any],
    live_state: dict[str, Any],
    checked_at: str,
    client: MetaMarketingApiClient,
) -> None:
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    adset_id = str((target.get("id") if target.get("type") == "adset" else live_state.get("adset_id")) or "").strip()
    if not adset_id:
        return
    try:
        adset_state = fetch_live_adset_state(adset_id, client=client)
    except MetaApiError as exc:
        action["live_adset_state"] = {
            "checked_at": checked_at,
            "lookup_status": "failed",
            "error": _redact_sensitive_text(str(exc)),
        }
        return
    action["live_adset_state"] = {"checked_at": checked_at, "lookup_status": "ok", **adset_state}


def _populate_budget_params_from_live_state(action: dict[str, Any]) -> None:
    if action.get("action_type") != "increase_adset_budget":
        return
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    adset_state = action.get("live_adset_state") if isinstance(action.get("live_adset_state"), dict) else {}
    daily_budget = _number(adset_state.get("daily_budget"))
    if daily_budget is not None and params.get("current_daily_budget_cents") is None:
        params["current_daily_budget_cents"] = int(daily_budget)
    action["params"] = params


def _append_meta_ai_remediation_actions(plan: dict[str, Any], checked_at: str) -> None:
    policy = plan.get("account_action_policy") if isinstance(plan.get("account_action_policy"), dict) else {}
    if not policy.get("disable_meta_ai_features"):
        return
    seen_adsets: dict[str, dict[str, Any]] = {}
    for action in plan.get("actions") or []:
        adset_state = action.get("live_adset_state") if isinstance(action.get("live_adset_state"), dict) else {}
        adset_id = str(adset_state.get("adset_id") or "").strip()
        if not adset_id or not adset_state.get("targeting_automation_detected"):
            continue
        seen_adsets.setdefault(adset_id, adset_state)
    existing = {action.get("action_id") for action in plan.get("actions") or []}
    for adset_id, adset_state in seen_adsets.items():
        action_id = f"disable_meta_ai_controls_{adset_id}"
        if action_id in existing:
            continue
        plan["actions"].append(
            {
                "action_id": action_id,
                "action_type": "disable_meta_ai_controls",
                "status": PROPOSED_STATUS,
                "executable": False,
                "approval_required": False,
                "target": {
                    "type": "adset",
                    "id": adset_id,
                    "name": adset_state.get("name"),
                    "campaign_id": adset_state.get("campaign_id"),
                },
                "params": {},
                "rationale": (
                    "Live ad set targeting appears to include Advantage/automation controls. "
                    "Disabling these is intentionally left as an operator follow-up rather than an "
                    "automatic write, so the executor never silently changes targeting automation."
                ),
                "evidence": {
                    "checked_at": checked_at,
                    "targeting_automation_excerpt": adset_state.get("targeting_automation_excerpt"),
                },
            }
        )


def _targeting_to_text(targeting: Any) -> str:
    """Normalize targeting (Graph API dict, or legacy JSON string) to searchable text."""
    if isinstance(targeting, (dict, list)):
        return json.dumps(targeting)
    return str(targeting or "")


def _targeting_automation_detected(targeting: Any) -> bool:
    lowered = _targeting_to_text(targeting).lower()
    return "targeting_automation" in lowered or "advantage_audience" in lowered


def _automation_excerpt(targeting: Any) -> str | None:
    targeting_text = _targeting_to_text(targeting)
    if not targeting_text:
        return None
    lowered = targeting_text.lower()
    index = lowered.find("targeting_automation")
    if index == -1:
        index = lowered.find("advantage_audience")
    if index == -1:
        return None
    start = max(0, index - 80)
    end = min(len(targeting_text), index + 240)
    return targeting_text[start:end]


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _redact_sensitive_text(value: str) -> str:
    if not value:
        return value
    redacted = re.sub(r"(access_token=)[^&\\s)'\"]+", r"\1[REDACTED]", value)
    redacted = re.sub(r"\bEAA[A-Za-z0-9_-]{20,}\b", "[REDACTED_META_TOKEN]", redacted)
    return redacted


def _looks_like_date(value: str) -> bool:
    pieces = value.split("-")
    return len(pieces) == 3 and all(piece.isdigit() for piece in pieces)
