"""Operator brief generation for Meta action plans."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_REPORTS_ROOT
from .confidence import (
    BAND_PRESENTATION,
    Band,
    Confidence,
    confidence_from_dict,
    evidence_from_dict,
    render_confidence_line,
    render_evidence_line,
)
from .utils import ensure_dir, write_json


def default_operator_brief_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "operator_brief.md"


def default_operator_brief_json_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "operator_brief.json"


def load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Action plan did not contain an object: {path}")
    return payload


def load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Report did not contain an object: {path}")
    return payload


def find_previous_report_run(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> str | None:
    account_dir = reports_root / account_slug
    if not account_dir.exists():
        return None
    candidates = sorted(
        path.name
        for path in account_dir.iterdir()
        if path.is_dir() and path.name < run_date and _looks_like_date(path.name)
    )
    return candidates[-1] if candidates else None


def build_operator_brief(
    *,
    plan: dict[str, Any],
    report: dict[str, Any] | None = None,
    previous_plan: dict[str, Any] | None = None,
    previous_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actions = [action for action in plan.get("actions") or [] if isinstance(action, dict)]
    previous_actions = [
        action for action in (previous_plan or {}).get("actions", []) if isinstance(action, dict)
    ]
    action_counts = Counter(str(action.get("action_type") or "unknown") for action in actions)
    status_counts = Counter(str(action.get("status") or "unknown") for action in actions)
    executable_actions = [action for action in actions if action.get("executable")]
    blocked_actions = [
        action
        for action in actions
        if action.get("status") in {"blocked", "failed"} or _live_lookup_blocks_action(action)
    ]
    blocked_action_ids = {action.get("action_id") for action in blocked_actions}
    approved_actions = [
        action
        for action in executable_actions
        if action.get("status") == "approved" and action.get("action_id") not in blocked_action_ids
    ]
    review_actions = [
        action
        for action in executable_actions
        if action.get("status") == "proposed" and action.get("action_id") not in blocked_action_ids
    ]
    manual_actions = [
        action
        for action in actions
        if not action.get("executable") and action.get("action_id") not in blocked_action_ids
    ]
    meta_ai_actions = [action for action in actions if action.get("action_type") == "disable_meta_ai_controls"]
    new_action_ids = sorted(
        {str(action.get("action_id")) for action in actions if action.get("action_id")}
        - {str(action.get("action_id")) for action in previous_actions if action.get("action_id")}
    )
    removed_action_ids = sorted(
        {str(action.get("action_id")) for action in previous_actions if action.get("action_id")}
        - {str(action.get("action_id")) for action in actions if action.get("action_id")}
    )
    account_delta = _account_delta(report, previous_report)

    return {
        "schema_version": 1,
        "account_slug": plan.get("account_slug"),
        "run_date": plan.get("run_date"),
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "account_goal": _account_goal(plan.get("account_action_policy") or {}),
        "summary": {
            "action_count": len(actions),
            "executable_count": len(executable_actions),
            "approved_executable_count": len(approved_actions),
            "manual_action_count": len(manual_actions),
            "blocked_or_failed_count": len(blocked_actions),
            "meta_ai_followup_count": len(meta_ai_actions),
            "action_counts": dict(sorted(action_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "what_changed": {
            "previous_run_date": (previous_plan or previous_report or {}).get("run_date"),
            "new_action_ids": new_action_ids,
            "removed_action_ids": removed_action_ids,
            "account_delta": account_delta,
        },
        "approved_to_execute": [_brief_action(action) for action in approved_actions],
        "ready_for_review": [_brief_action(action) for action in review_actions],
        "needs_human_judgment": [_brief_action(action) for action in manual_actions],
        "do_not_touch_yet": [_brief_action(action) for action in blocked_actions],
        "meta_ai_followups": [_brief_action(action) for action in meta_ai_actions],
    }


def render_operator_brief(brief: dict[str, Any]) -> str:
    summary = brief.get("summary") or {}
    changed = brief.get("what_changed") or {}
    lines = [
        f"# Operator Brief - {brief.get('account_slug')} - {brief.get('run_date')}",
        "",
        "## Account Goal",
        str(brief.get("account_goal") or "No account action policy configured."),
        "",
        "## Snapshot",
        f"- Actions: {summary.get('action_count', 0)}",
        f"- Executable after approval: {summary.get('executable_count', 0)}",
        f"- Approved executable now: {summary.get('approved_executable_count', 0)}",
        f"- Manual/operator tasks: {summary.get('manual_action_count', 0)}",
        f"- Meta AI / Advantage follow-ups: {summary.get('meta_ai_followup_count', 0)}",
        "",
        "## What Changed",
    ]
    previous_run = changed.get("previous_run_date")
    if previous_run:
        lines.append(f"- Compared with previous run: {previous_run}")
    else:
        lines.append("- No previous run was available for comparison.")
    account_delta = changed.get("account_delta") or {}
    if account_delta:
        for label, value in account_delta.items():
            lines.append(f"- {label}: {value}")
    lines.extend(_render_id_list("New actions", changed.get("new_action_ids") or []))
    lines.extend(_render_id_list("Removed actions", changed.get("removed_action_ids") or []))

    sections = [
        ("Approved To Execute", brief.get("approved_to_execute") or []),
        ("Ready For Review", brief.get("ready_for_review") or []),
        ("Needs Human Judgment", brief.get("needs_human_judgment") or []),
        ("Do Not Touch Yet", brief.get("do_not_touch_yet") or []),
        ("Meta AI Follow-Ups", brief.get("meta_ai_followups") or []),
    ]
    account_slug = brief.get("account_slug")
    for title, actions in sections:
        lines.extend(["", f"## {title}"])
        if not actions:
            lines.append("- None.")
            continue
        for action in actions:
            lines.append(
                "- "
                f"{action.get('action_id')} ({action.get('action_type')}) "
                f"targeting {action.get('target_name') or action.get('target_id') or 'unknown target'}: "
                f"{action.get('rationale') or 'No rationale supplied.'}"
            )
            lines.extend(_render_action_evidence(action, account_slug=account_slug))
    return "\n".join(lines).rstrip() + "\n"


# Markdown indent for the evidence/confidence sub-lines beneath each action bullet.
_BLOCK_INDENT = "    "


def _render_action_evidence(action: dict[str, Any], *, account_slug: Any) -> list[str]:
    """Render the compact evidence + confidence block beneath one action bullet.

    Additive and skimmable: a labeled line each for the facts, the confidence band, (when the claim
    is causal) the correlational caveat + offer to file an A/B, the exact re-check command, and what
    would move the band. Renders nothing when the action carries no evidence/confidence (e.g.
    measurement_review actions) — never prints ``None``. The band/emoji vocabulary comes straight
    from ``confidence.py`` so the brief speaks the one confidence language."""
    evidence_block = action.get("evidence") if isinstance(action.get("evidence"), dict) else {}
    confidence_block = action.get("confidence") if isinstance(action.get("confidence"), dict) else {}
    if not evidence_block and not confidence_block:
        return []

    lines: list[str] = []
    if evidence_block:
        evidence = evidence_from_dict(evidence_block)
        # Drop the inline regen query — it gets its own labeled "Re-check:" line below.
        lines.append(f"{_BLOCK_INDENT}Evidence: {render_evidence_line(evidence, include_regen=False)}")

    confidence: Confidence | None = None
    if confidence_block:
        confidence = confidence_from_dict(confidence_block)
        lines.append(f"{_BLOCK_INDENT}Confidence: {_render_brief_confidence(confidence)}")
        if confidence.causal_flag:
            lines.append(f"{_BLOCK_INDENT}{_render_causal_offer(account_slug)}")

    regen = evidence_block.get("regenerating_query")
    if regen:
        lines.append(f"{_BLOCK_INDENT}Re-check: {regen}")

    if confidence is not None:
        raise_lower = _render_raise_lower(confidence)
        if raise_lower:
            lines.append(f"{_BLOCK_INDENT}{raise_lower}")

    return lines


def _render_brief_confidence(conf: Confidence) -> str:
    """One-line confidence for the brief. Defers to ``confidence.render_confidence_line`` for scored
    bands; an ``abstain`` reads as a promising test ("Insufficient data — keep running"), distinct
    from a 🔴 Low verdict and never a percentage."""
    if conf.band is Band.abstain:
        head = f"{BAND_PRESENTATION[Band.abstain]['emoji']} Insufficient data — keep running"
        if conf.factors:
            head += " — " + "; ".join(conf.factors[:3])
        return head
    return render_confidence_line(conf)


def _render_causal_offer(account_slug: Any) -> str:
    """Surface the correlational caveat and the offer to confirm it with an A/B. The brief only
    surfaces the text — it does not auto-file the experiment."""
    command = "experiment define"
    if account_slug:
        command += f" --account {account_slug}"
    return f"⚠️ correlational — confirm via A/B — file one to confirm: {command} …"


def _render_raise_lower(conf: Confidence) -> str | None:
    parts = []
    if conf.would_raise:
        parts.append(f"Would raise: {conf.would_raise}")
    if conf.would_lower:
        parts.append(f"Would lower: {conf.would_lower}")
    return " · ".join(parts) if parts else None


def write_operator_brief(
    *,
    brief: dict[str, Any],
    markdown_path: Path,
    json_path: Path,
) -> tuple[Path, Path]:
    ensure_dir(markdown_path.parent)
    markdown_path.write_text(render_operator_brief(brief), encoding="utf-8")
    write_json(json_path, brief)
    return markdown_path, json_path


def _brief_action(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "status": action.get("status"),
        "executable": bool(action.get("executable")),
        "target_type": target.get("type"),
        "target_id": target.get("id"),
        "target_name": target.get("name") or target.get("source_ad_name"),
        "campaign_name": target.get("campaign_name"),
        "adset_name": target.get("adset_name"),
        "params": action.get("params") if isinstance(action.get("params"), dict) else {},
        "rationale": action.get("rationale"),
        # Carry the structured evidence + computed confidence straight through (computed once, in the
        # action plan — never recomputed here). Empty dict when the action carries none, so the
        # renderer can omit the block gracefully.
        "evidence": action.get("evidence") if isinstance(action.get("evidence"), dict) else {},
        "confidence": action.get("confidence") if isinstance(action.get("confidence"), dict) else {},
    }


def _account_goal(policy: dict[str, Any]) -> str:
    goal = policy.get("primary_goal")
    if goal == "maximize_in_app_subscriptions":
        target = policy.get("secondary_cost_per_app_install_target")
        return (
            "Prioritize in-app subscription results first. "
            f"Use app installs as a secondary signal with a ${target:g} target when subscription volume is sparse."
            if isinstance(target, int | float)
            else "Prioritize in-app subscription results first, with app installs as the secondary signal."
        )
    if goal == "roas":
        target = policy.get("target_roas")
        return (
            f"Optimize toward {target:g} blended ROAS or better."
            if isinstance(target, int | float)
            else "Optimize toward profitable blended ROAS."
        )
    return "No specific account action goal is configured."


def _account_delta(
    report: dict[str, Any] | None,
    previous_report: dict[str, Any] | None,
) -> dict[str, str]:
    if not report or not previous_report:
        return {}
    current = report.get("account_summary") if isinstance(report.get("account_summary"), dict) else {}
    previous = (
        previous_report.get("account_summary")
        if isinstance(previous_report.get("account_summary"), dict)
        else {}
    )
    fields = {
        "Spend change": "total_spend",
        "Results change": "total_results",
        "App installs change": "total_app_installs",
        "ROAS change": "blended_roas",
    }
    deltas: dict[str, str] = {}
    for label, field in fields.items():
        current_value = _number(current.get(field))
        previous_value = _number(previous.get(field))
        if current_value is None or previous_value is None:
            continue
        delta = current_value - previous_value
        sign = "+" if delta >= 0 else ""
        deltas[label] = f"{sign}{delta:.2f} ({previous_value:.2f} -> {current_value:.2f})"
    return deltas


def _has_failed_live_lookup(action: dict[str, Any]) -> bool:
    for key in ("live_state", "live_adset_state"):
        state = action.get(key)
        if isinstance(state, dict) and state.get("lookup_status") == "failed":
            return True
    return False


def _live_lookup_blocks_action(action: dict[str, Any]) -> bool:
    if not _has_failed_live_lookup(action):
        return False
    return bool(action.get("executable")) or action.get("action_type") == "increase_adset_budget"


def _render_id_list(title: str, ids: list[str]) -> list[str]:
    if not ids:
        return [f"- {title}: none"]
    shown = ", ".join(ids[:8])
    suffix = f" and {len(ids) - 8} more" if len(ids) > 8 else ""
    return [f"- {title}: {shown}{suffix}"]


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_like_date(value: str) -> bool:
    pieces = value.split("-")
    return len(pieces) == 3 and all(piece.isdigit() for piece in pieces)
