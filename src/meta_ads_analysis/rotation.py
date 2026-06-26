"""Audience rotation across active ad sets.

This is an experiment harness: it reads the custom-audience targeting of the
active ad sets in an account, rotates each ad set's included audience forward to
the next ad set, and recomputes every ad set's exclusions so the invariant
"target one audience, exclude the others" still holds afterward.

Writes go through the Graph API (``MetaMarketingApiClient.update_adset``) and
reuse the same ``proposed -> approved -> apply`` + dry-run guardrails as the
report-driven action plan. Rotation never adds Advantage/automation controls; an
ad set that already has Advantage Audience enabled is flagged as a warning
because custom-audience swaps may be treated only as suggestions there.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import account_registry
from .config import DEFAULT_REPORTS_ROOT
from .meta_api import MetaApiError, MetaMarketingApiClient
from .reader_provider import MetaReaderProvider, as_reader, reader_from_env
from .utils import ensure_dir, write_json

PROPOSED_STATUS = "proposed"
APPROVED_STATUS = "approved"
EXECUTED_STATUS = "executed"

ADSET_FIELDS = ["id", "name", "status", "effective_status", "campaign_id", "targeting"]


@dataclass(slots=True)
class RotationResult:
    adset_id: str
    status: str
    targeting: dict[str, Any] | None = None
    reason: str | None = None
    response: dict[str, Any] | None = None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _audience_refs(raw: Any) -> list[dict[str, str]]:
    """Normalize a custom_audiences / excluded_custom_audiences value to [{id, name}]."""
    refs: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return refs
    for item in raw:
        if isinstance(item, dict) and item.get("id") is not None:
            ref = {"id": str(item["id"])}
            if item.get("name"):
                ref["name"] = str(item["name"])
            refs.append(ref)
        elif isinstance(item, (str, int)):
            refs.append({"id": str(item)})
    return refs


def _ids(refs: list[dict[str, str]]) -> list[str]:
    return [ref["id"] for ref in refs]


def advantage_audience_enabled(targeting: dict[str, Any]) -> bool:
    automation = targeting.get("targeting_automation")
    if isinstance(automation, dict):
        return str(automation.get("advantage_audience")) in {"1", "True", "true"}
    return False


def summarize_adsets(adsets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce raw ad set payloads to the audience structure we rotate over."""
    summaries: list[dict[str, Any]] = []
    for adset in adsets:
        targeting = adset.get("targeting") if isinstance(adset.get("targeting"), dict) else {}
        summaries.append(
            {
                "adset_id": str(adset.get("id") or ""),
                "adset_name": adset.get("name"),
                "campaign_id": adset.get("campaign_id"),
                "effective_status": adset.get("effective_status"),
                "included": _audience_refs(targeting.get("custom_audiences")),
                "excluded": _audience_refs(targeting.get("excluded_custom_audiences")),
                "advantage_audience": advantage_audience_enabled(targeting),
            }
        )
    return summaries


def _rotate_forward(values: list[Any], offset: int) -> list[Any]:
    if not values:
        return values
    shift = offset % len(values)
    return values[-shift:] + values[:-shift] if shift else list(values)


def _name_map(summaries: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for summary in summaries:
        for ref in summary["included"] + summary["excluded"]:
            if ref.get("name"):
                names[ref["id"]] = ref["name"]
    return names


def _label(ids: list[str], names: dict[str, str]) -> str:
    if not ids:
        return "(none)"
    return ", ".join(names.get(i, i) for i in ids)


def build_rotation_plan(
    adsets: list[dict[str, Any]],
    *,
    account_slug: str,
    ad_account_id: str,
    offset: int = 1,
    disable_advantage_audience: bool = False,
) -> dict[str, Any]:
    """Build a rotation plan from active ad sets.

    new_included[i] = old_included[(i - offset) mod n] (each ad set's audience
    moves forward to the next ad set). Each ad set's exclusions become the pool
    of all rotating audiences minus its own new include, preserving any excluded
    audiences that are not part of the rotation pool.
    """
    summaries = summarize_adsets(adsets)
    eligible = [s for s in summaries if s["included"]]
    warnings: list[str] = []
    if len(eligible) < 2:
        warnings.append(
            f"Only {len(eligible)} ad set(s) have an included custom audience; "
            "need at least 2 to rotate."
        )
    names = _name_map(summaries)

    pool: list[str] = []
    for summary in eligible:
        for aid in _ids(summary["included"]):
            if aid not in pool:
                pool.append(aid)
    pool_set = set(pool)

    included_units = [_ids(s["included"]) for s in eligible]
    rotated_units = _rotate_forward(included_units, offset)

    rotations: list[dict[str, Any]] = []
    for summary, new_included_ids in zip(eligible, rotated_units):
        old_included_ids = _ids(summary["included"])
        preserved_excluded = [i for i in _ids(summary["excluded"]) if i not in pool_set]
        new_excluded_ids = [i for i in pool if i not in set(new_included_ids)] + preserved_excluded
        if summary["advantage_audience"]:
            if disable_advantage_audience:
                warnings.append(
                    f"Ad set {summary['adset_id']} ({summary['adset_name']}) has Advantage "
                    "Audience enabled; this rotation will turn it off so the audience change is accepted."
                )
            else:
                warnings.append(
                    f"Ad set {summary['adset_id']} ({summary['adset_name']}) has Advantage Audience "
                    "enabled; Meta will REJECT the audience change unless you re-run with "
                    "--disable-advantage-audience."
                )
        will_disable_aa = disable_advantage_audience and summary["advantage_audience"]
        diff = (
            f"include [{_label(old_included_ids, names)}] -> [{_label(new_included_ids, names)}]; "
            f"exclude [{_label(_ids(summary['excluded']), names)}] -> [{_label(new_excluded_ids, names)}]"
        )
        if will_disable_aa:
            diff += "; advantage_audience: on -> off"
        rotations.append(
            {
                "adset_id": summary["adset_id"],
                "adset_name": summary["adset_name"],
                "campaign_id": summary["campaign_id"],
                "status": PROPOSED_STATUS,
                "advantage_audience": summary["advantage_audience"],
                "disable_advantage_audience": will_disable_aa,
                "old_included": old_included_ids,
                "old_excluded": _ids(summary["excluded"]),
                "new_included": new_included_ids,
                "new_excluded": new_excluded_ids,
                "diff": diff,
            }
        )

    skipped = [s["adset_id"] for s in summaries if not s["included"]]
    if skipped:
        warnings.append(
            f"Skipped {len(skipped)} active ad set(s) with no included custom audience: "
            + ", ".join(skipped)
        )

    return {
        "schema_version": 1,
        "plan_type": "audience_rotation",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "offset": offset,
        "disable_advantage_audience": disable_advantage_audience,
        "generated_at": _now_iso(),
        "audience_names": names,
        "approval_instructions": (
            "Review each rotation. To allow execution, set its status to 'approved'. "
            "Only approved rotations are sent to Meta, and only with the --execute flag."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "writes_only_custom_audiences": not disable_advantage_audience,
            "never_enables_advantage_audience": True,
            "advantage_audience_disable_only_when_requested": disable_advantage_audience,
            "rescans_live_targeting_before_write": True,
        },
        "warnings": warnings,
        "rotations": rotations,
    }


def compute_new_targeting(
    live_targeting: dict[str, Any],
    *,
    new_included_ids: list[str],
    new_excluded_ids: list[str],
    disable_advantage_audience: bool = False,
) -> dict[str, Any]:
    """Return the full targeting object with only the custom-audience fields swapped.

    When ``disable_advantage_audience`` is set, ``targeting_automation.advantage_audience``
    is forced to 0 (other automation keys are preserved). This is the only case in which
    rotation touches targeting automation, and it can only ever turn it off, never on.
    """
    targeting = copy.deepcopy(live_targeting) if isinstance(live_targeting, dict) else {}
    if new_included_ids:
        targeting["custom_audiences"] = [{"id": i} for i in new_included_ids]
    else:
        targeting.pop("custom_audiences", None)
    if new_excluded_ids:
        targeting["excluded_custom_audiences"] = [{"id": i} for i in new_excluded_ids]
    else:
        targeting.pop("excluded_custom_audiences", None)
    if disable_advantage_audience:
        automation = targeting.get("targeting_automation")
        automation = dict(automation) if isinstance(automation, dict) else {}
        automation["advantage_audience"] = 0
        targeting["targeting_automation"] = automation
        # age_range is an automation-managed field; Meta rejects it once targeting
        # automation is disabled ("targeting_automation must be enabled to use age_range").
        # age_min/age_max remain as the real age controls.
        targeting.pop("age_range", None)
    return targeting


def apply_rotation_plan(
    plan: dict[str, Any],
    client: MetaMarketingApiClient,
    *,
    execute: bool,
    validate_only: bool = False,
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
) -> list[RotationResult]:
    """Dry-run, validate against Meta, or execute approved rotations.

    Mixed read+write: the live re-read of each ad set's targeting (drift detection — fresh, not
    cached) goes through ``reader``; the targeting write stays on the concrete ``client``. When
    ``reader`` is omitted it defaults to reading through the same ``client``.

    - ``validate_only=True``: send each approved rotation to Meta with
      ``execution_options=['validate_only']`` — a real round-trip that returns Meta's
      validation result but changes nothing. Takes precedence over ``execute``.
    - ``execute=True``: perform the real write.
    - otherwise: a local dry run that records the targeting that would be sent.
    """
    effective_reader = as_reader(reader) or as_reader(client)
    results: list[RotationResult] = []
    for rotation in plan.get("rotations") or []:
        if not isinstance(rotation, dict):
            continue
        adset_id = str(rotation.get("adset_id") or "unknown")
        if rotation.get("status") != APPROVED_STATUS:
            results.append(RotationResult(adset_id, "skipped", reason="Rotation is not approved."))
            continue

        live = effective_reader.get_adset(adset_id, fields=ADSET_FIELDS)
        live_targeting = live.get("targeting") if isinstance(live.get("targeting"), dict) else {}
        live_included = _ids(_audience_refs(live_targeting.get("custom_audiences")))
        if live_included != list(rotation.get("old_included") or []):
            results.append(
                RotationResult(
                    adset_id,
                    "blocked",
                    reason=(
                        "Live included audiences changed since the plan was built "
                        f"(live={live_included}, plan={rotation.get('old_included')}). Re-propose."
                    ),
                )
            )
            continue
        if advantage_audience_enabled(live_targeting) and not rotation.get("advantage_audience"):
            results.append(
                RotationResult(
                    adset_id,
                    "blocked",
                    reason="Advantage Audience is now enabled live but was not at plan time. Re-propose.",
                )
            )
            continue

        new_targeting = compute_new_targeting(
            live_targeting,
            new_included_ids=list(rotation.get("new_included") or []),
            new_excluded_ids=list(rotation.get("new_excluded") or []),
            disable_advantage_audience=bool(rotation.get("disable_advantage_audience")),
        )
        if validate_only:
            try:
                response = client.update_adset(
                    adset_id, params={"targeting": new_targeting}, validate_only=True
                )
            except MetaApiError as exc:
                results.append(
                    RotationResult(adset_id, "validation_failed", targeting=new_targeting, reason=str(exc))
                )
                continue
            results.append(RotationResult(adset_id, "validated", targeting=new_targeting, response=response))
            continue
        if not execute:
            results.append(RotationResult(adset_id, "dry_run", targeting=new_targeting))
            continue

        try:
            response = client.update_adset(adset_id, params={"targeting": new_targeting})
        except MetaApiError as exc:
            results.append(RotationResult(adset_id, "failed", targeting=new_targeting, reason=str(exc)))
            continue
        results.append(RotationResult(adset_id, EXECUTED_STATUS, targeting=new_targeting, response=response))
    return results


def default_rotation_plan_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "rotation_plan.json"


def default_rotation_results_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"rotation_results_{timestamp}.json"


def write_rotation_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def write_rotation_results(
    *,
    plan: dict[str, Any],
    results: list[RotationResult],
    output_path: Path,
    execute: bool,
) -> Path:
    payload = {
        "schema_version": 1,
        "plan_type": "audience_rotation",
        "account_slug": plan.get("account_slug"),
        "executed": execute,
        "generated_at": _now_iso(),
        "results": [
            {
                "adset_id": item.adset_id,
                "status": item.status,
                "targeting": item.targeting,
                "reason": item.reason,
                "response": item.response,
            }
            for item in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path


# --- Disable Advantage Audience (in place, audiences unchanged) --------------


def build_advantage_disable_plan(
    adsets: list[dict[str, Any]],
    *,
    account_slug: str,
    ad_account_id: str,
) -> dict[str, Any]:
    """Plan to turn Advantage Audience off on each ad set, keeping audiences as-is.

    Only ad sets that currently have it enabled get an actionable item; others are
    recorded as already-off. Inclusions and exclusions are preserved verbatim.
    """
    summaries = summarize_adsets(adsets)
    names = _name_map(summaries)
    items: list[dict[str, Any]] = []
    for summary in summaries:
        items.append(
            {
                "adset_id": summary["adset_id"],
                "adset_name": summary["adset_name"],
                "status": PROPOSED_STATUS,
                "advantage_audience": summary["advantage_audience"],
                "included": _ids(summary["included"]),
                "excluded": _ids(summary["excluded"]),
                "included_labels": [names.get(i, i) for i in _ids(summary["included"])],
                "excluded_labels": [names.get(i, i) for i in _ids(summary["excluded"])],
            }
        )
    return {
        "schema_version": 1,
        "plan_type": "advantage_disable",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "audience_names": names,
        "approval_instructions": (
            "Review each ad set. To allow the change, set its status to 'approved'. Only "
            "approved items are sent to Meta, and only with --execute (or tested with --validate-only)."
        ),
        "guardrails": {
            "requires_explicit_approval": True,
            "preserves_audiences": True,
            "writes_only_advantage_audience_off_and_age_range": True,
        },
        "items": items,
    }


def apply_advantage_disable_plan(
    plan: dict[str, Any],
    client: MetaMarketingApiClient,
    *,
    execute: bool,
    validate_only: bool = False,
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
) -> list[RotationResult]:
    """Dry-run, validate, or execute approved Advantage Audience disables.

    Mixed read+write: audiences are preserved exactly; only advantage_audience=0 is written (with
    the automation-managed age_range dropped). The live per-ad-set re-read goes through ``reader``
    (defaulting to the ``client``); the write stays on the concrete ``client``.
    """
    effective_reader = as_reader(reader) or as_reader(client)
    results: list[RotationResult] = []
    for item in plan.get("items") or []:
        if not isinstance(item, dict):
            continue
        adset_id = str(item.get("adset_id") or "unknown")
        if item.get("status") != APPROVED_STATUS:
            results.append(RotationResult(adset_id, "skipped", reason="Item is not approved."))
            continue

        live = effective_reader.get_adset(adset_id, fields=ADSET_FIELDS)
        live_targeting = live.get("targeting") if isinstance(live.get("targeting"), dict) else {}
        if not advantage_audience_enabled(live_targeting):
            results.append(RotationResult(adset_id, "skipped", reason="Advantage Audience is already off."))
            continue

        live_included = _ids(_audience_refs(live_targeting.get("custom_audiences")))
        live_excluded = _ids(_audience_refs(live_targeting.get("excluded_custom_audiences")))
        new_targeting = compute_new_targeting(
            live_targeting,
            new_included_ids=live_included,
            new_excluded_ids=live_excluded,
            disable_advantage_audience=True,
        )
        if validate_only:
            try:
                response = client.update_adset(adset_id, params={"targeting": new_targeting}, validate_only=True)
            except MetaApiError as exc:
                results.append(RotationResult(adset_id, "validation_failed", targeting=new_targeting, reason=str(exc)))
                continue
            results.append(RotationResult(adset_id, "validated", targeting=new_targeting, response=response))
            continue
        if not execute:
            results.append(RotationResult(adset_id, "dry_run", targeting=new_targeting))
            continue

        try:
            response = client.update_adset(adset_id, params={"targeting": new_targeting})
        except MetaApiError as exc:
            results.append(RotationResult(adset_id, "failed", targeting=new_targeting, reason=str(exc)))
            continue
        results.append(RotationResult(adset_id, EXECUTED_STATUS, targeting=new_targeting, response=response))
    return results


def default_advantage_disable_plan_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "advantage_disable_plan.json"


def default_advantage_disable_results_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"advantage_disable_results_{timestamp}.json"


def write_advantage_disable_results(
    *,
    plan: dict[str, Any],
    results: list[RotationResult],
    output_path: Path,
    execute: bool,
) -> Path:
    payload = {
        "schema_version": 1,
        "plan_type": "advantage_disable",
        "account_slug": plan.get("account_slug"),
        "executed": execute,
        "generated_at": _now_iso(),
        "results": [
            {
                "adset_id": item.adset_id,
                "status": item.status,
                "targeting": item.targeting,
                "reason": item.reason,
                "response": item.response,
            }
            for item in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path


# --- Ad set rename ----------------------------------------------------------

ADSET_NAME_FIELDS = ["id", "name"]


@dataclass(slots=True)
class RenameResult:
    adset_id: str
    status: str
    old_name: str | None = None
    new_name: str | None = None
    reason: str | None = None
    response: dict[str, Any] | None = None


def friendly_audience_name(included_refs: list[dict[str, str]], names: dict[str, str]) -> str | None:
    """Derive a human ad set name from the included audiences, preferring the seed list.

    "high-value-customers.csv" -> "High Value Customers". Lookalike entries are only
    used if no seed (non-lookalike) audience is present.
    """
    labels = [ref.get("name") or names.get(ref["id"], "") for ref in included_refs]
    labels = [label for label in labels if label]
    if not labels:
        return None
    seed = next((label for label in labels if not label.lower().startswith("lookalike")), labels[0])
    base = re.sub(r"\.csv$", "", seed, flags=re.IGNORECASE)
    base = base.replace("-facebook-fixed", "").replace("_", " ").replace("-", " ")
    base = re.sub(r"\s+", " ", base).strip()
    return base.title() if base else None


def build_rename_plan(
    adsets: list[dict[str, Any]],
    *,
    account_slug: str,
    ad_account_id: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Propose a name per ad set derived from its current included audience.

    Run this AFTER a rotation so names reflect what each ad set now targets.
    ``overrides`` maps adset_id -> explicit name and wins over the derived name.
    """
    overrides = overrides or {}
    summaries = summarize_adsets(adsets)
    names = _name_map(summaries)
    warnings: list[str] = []
    renames: list[dict[str, Any]] = []
    for summary in summaries:
        adset_id = summary["adset_id"]
        old_name = summary["adset_name"]
        proposed = overrides.get(adset_id) or friendly_audience_name(summary["included"], names)
        if not proposed:
            warnings.append(f"Ad set {adset_id} ({old_name}) has no included audience to derive a name from; skipped.")
            continue
        renames.append(
            {
                "adset_id": adset_id,
                "status": PROPOSED_STATUS,
                "old_name": old_name,
                "new_name": proposed,
                "included": [names.get(i, i) for i in _ids(summary["included"])],
                "unchanged": proposed == old_name,
            }
        )
    return {
        "schema_version": 1,
        "plan_type": "adset_rename",
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "generated_at": _now_iso(),
        "approval_instructions": (
            "Review each rename. To allow it, set its status to 'approved'. Only approved "
            "renames are sent to Meta, and only with --execute (or tested with --validate-only)."
        ),
        "guardrails": {"requires_explicit_approval": True, "writes_only_name": True},
        "warnings": warnings,
        "renames": renames,
    }


def apply_rename_plan(
    plan: dict[str, Any],
    client: MetaMarketingApiClient,
    *,
    execute: bool,
    validate_only: bool = False,
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
) -> list[RenameResult]:
    """Dry-run, validate, or execute approved ad set renames (writes only the name field).

    Mixed read+write: the live name re-read (drift detection) goes through ``reader`` (defaulting
    to the ``client``); the rename write stays on the concrete ``client``.
    """
    effective_reader = as_reader(reader) or as_reader(client)
    results: list[RenameResult] = []
    for rename in plan.get("renames") or []:
        if not isinstance(rename, dict):
            continue
        adset_id = str(rename.get("adset_id") or "unknown")
        old_name = rename.get("old_name")
        new_name = str(rename.get("new_name") or "")
        if rename.get("status") != APPROVED_STATUS:
            results.append(RenameResult(adset_id, "skipped", old_name, new_name, reason="Rename is not approved."))
            continue
        if not new_name or new_name == old_name:
            results.append(RenameResult(adset_id, "skipped", old_name, new_name, reason="Name is unchanged."))
            continue

        live = effective_reader.get_adset(adset_id, fields=ADSET_NAME_FIELDS)
        if live.get("name") != old_name:
            results.append(
                RenameResult(
                    adset_id,
                    "blocked",
                    old_name,
                    new_name,
                    reason=f"Live name changed since the plan was built (live={live.get('name')!r}). Re-propose.",
                )
            )
            continue

        if validate_only:
            try:
                response = client.update_adset(adset_id, params={"name": new_name}, validate_only=True)
            except MetaApiError as exc:
                results.append(RenameResult(adset_id, "validation_failed", old_name, new_name, reason=str(exc)))
                continue
            results.append(RenameResult(adset_id, "validated", old_name, new_name, response=response))
            continue
        if not execute:
            results.append(RenameResult(adset_id, "dry_run", old_name, new_name))
            continue

        try:
            response = client.update_adset(adset_id, params={"name": new_name})
        except MetaApiError as exc:
            results.append(RenameResult(adset_id, "failed", old_name, new_name, reason=str(exc)))
            continue
        results.append(RenameResult(adset_id, EXECUTED_STATUS, old_name, new_name, response=response))
    return results


def default_rename_plan_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    return reports_root / account_slug / run_date / "rename_plan.json"


def default_rename_results_path(
    account_slug: str,
    run_date: str,
    reports_root: Path = DEFAULT_REPORTS_ROOT,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return reports_root / account_slug / run_date / f"rename_results_{timestamp}.json"


def write_rename_plan(plan: dict[str, Any], output_path: Path) -> Path:
    write_json(output_path, plan)
    return output_path


def write_rename_results(
    *,
    plan: dict[str, Any],
    results: list[RenameResult],
    output_path: Path,
    execute: bool,
) -> Path:
    payload = {
        "schema_version": 1,
        "plan_type": "adset_rename",
        "account_slug": plan.get("account_slug"),
        "executed": execute,
        "generated_at": _now_iso(),
        "results": [
            {
                "adset_id": item.adset_id,
                "status": item.status,
                "old_name": item.old_name,
                "new_name": item.new_name,
                "reason": item.reason,
                "response": item.response,
            }
            for item in results
        ],
    }
    ensure_dir(output_path.parent)
    write_json(output_path, payload)
    return output_path


def fetch_active_adsets(
    account_slug: str,
    *,
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
    accounts_config_path: Path | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve the account and return (ad_account_id, active ad set payloads).

    Read-only: ``reader`` accepts a :class:`MetaReaderProvider` or a raw
    ``MetaMarketingApiClient`` (wrapped); when omitted the env-selected reader is built
    (``direct`` by default — see :func:`reader_from_env`).
    """
    account = account_registry.resolve_account(
        account_slug,
        accounts_config_path or account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH,
    )
    effective_reader = as_reader(reader) or reader_from_env()
    adsets = effective_reader.list_adsets(
        account.ad_account_id,
        fields=ADSET_FIELDS,
        effective_status=["ACTIVE"],
    )
    return account.ad_account_id, adsets
