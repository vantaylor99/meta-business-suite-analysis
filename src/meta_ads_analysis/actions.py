"""Action planning and guarded Meta CLI execution."""

from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .utils import ensure_dir, write_json

APPROVED_STATUS = "approved"
EXECUTED_STATUS = "executed"
PROPOSED_STATUS = "proposed"
SUPPORTED_EXECUTABLE_ACTIONS = {"pause_ad"}
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
    command: list[str] | None
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None
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

    actions: list[dict[str, Any]] = []
    seen_action_ids: set[str] = set()
    for ad in payload.get("budget_waste") or []:
        if ad.get("waste_status") != "high":
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
                f"High waste risk: spent ${ad.get('total_spend', 0):.2f}, "
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
            "Only executable actions with approved status will be sent to Meta CLI."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "supported_executable_actions": sorted(SUPPORTED_EXECUTABLE_ACTIONS),
            "meta_ai_features": META_AI_DISABLED_POLICY,
        },
        "actions": actions,
    }


def enrich_action_plan_with_live_state(
    plan: dict[str, Any],
    *,
    meta_binary: str = "meta",
    runner: Any | None = None,
) -> dict[str, Any]:
    account_slug = str(plan.get("account_slug") or "")
    account = account_registry.resolve_account(
        account_slug,
        account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH,
    )
    enriched = {**plan, "actions": [dict(action) for action in plan.get("actions") or []]}
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for action in enriched["actions"]:
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        if target.get("type") != "ad" or not target.get("id"):
            continue
        try:
            live_state = fetch_live_ad_state(
                str(target["id"]),
                account.ad_account_id,
                meta_binary=meta_binary,
                runner=runner,
            )
        except RuntimeError as exc:
            action["live_state"] = {
                "checked_at": checked_at,
                "lookup_status": "failed",
                "error": str(exc),
            }
            continue
        action["live_state"] = {"checked_at": checked_at, "lookup_status": "ok", **live_state}
        if action.get("action_type") == "pause_ad" and _live_state_is_paused(live_state):
            action["status"] = "already_resolved"
            action["executable"] = False
            action["approval_required"] = False
            action["rationale"] = f"{action.get('rationale', '').rstrip()} Live Meta state already shows this ad is paused."
    enriched["live_state_enriched_at"] = checked_at
    return enriched


def fetch_live_ad_state(
    ad_id: str,
    ad_account_id: str,
    *,
    meta_binary: str = "meta",
    runner: Any | None = None,
) -> dict[str, Any]:
    command = [
        meta_binary,
        "--output",
        "json",
        "ads",
        "--ad-account-id",
        ad_account_id,
        "ad",
        "get",
        ad_id,
    ]
    payload = _run_meta_cli_json(command, runner=runner)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        item = payload[0]
    elif isinstance(payload, dict):
        item = payload
    else:
        raise RuntimeError(f"Meta CLI returned no ad state for ad {ad_id}.")
    return {
        "ad_id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "effective_status": item.get("effective_status"),
        "adset_id": item.get("adset_id"),
        "campaign_id": item.get("campaign_id"),
        "updated_time": item.get("updated_time"),
    }


def write_action_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def apply_action_plan(
    plan: dict[str, Any],
    *,
    execute: bool,
    meta_binary: str = "meta",
) -> list[ApplyResult]:
    account_slug = str(plan.get("account_slug") or "")
    account = account_registry.resolve_account(
        account_slug,
        account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH,
    )
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
            command = build_meta_cli_command(action, account.ad_account_id, meta_binary=meta_binary)
        except ValueError as exc:
            results.append(ApplyResult(action_id, "blocked", None, reason=str(exc)))
            continue
        if not execute:
            results.append(ApplyResult(action_id, "dry_run", command))
            continue
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        results.append(
            ApplyResult(
                action_id,
                EXECUTED_STATUS if completed.returncode == 0 else "failed",
                command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
    return results


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
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "results": [
            {
                "action_id": item.action_id,
                "status": item.status,
                "command": item.command,
                "returncode": item.returncode,
                "stdout": item.stdout,
                "stderr": item.stderr,
                "reason": item.reason,
            }
            for item in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path


def build_meta_cli_command(action: dict[str, Any], ad_account_id: str, *, meta_binary: str = "meta") -> list[str]:
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
        return [
            meta_binary,
            "--no-input",
            "-o",
            "json",
            "ads",
            "--ad-account-id",
            ad_account_id,
            "ad",
            "update",
            ad_id,
            "--status",
            "paused",
        ]
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
            "campaign_name": ad.get("campaign_name"),
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


def _run_meta_cli_json(command: list[str], *, runner: Any | None = None) -> Any:
    effective_runner = runner or subprocess.run
    completed = effective_runner(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit status {completed.returncode}"
        raise RuntimeError(f"Meta CLI command failed: {detail}")
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Meta CLI returned non-JSON output: {exc}") from exc


def _live_state_is_paused(live_state: dict[str, Any]) -> bool:
    return str(live_state.get("status") or "").upper() == "PAUSED"


def _looks_like_date(value: str) -> bool:
    pieces = value.split("-")
    return len(pieces) == 3 and all(piece.isdigit() for piece in pieces)
