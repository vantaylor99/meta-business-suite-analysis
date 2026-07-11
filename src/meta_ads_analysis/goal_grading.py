"""Pure goal-grading engine: where does one account stand against its own configured goal?

``cross_account_performance`` already returns each account's real efficiency (native
``cost_per_result`` / ``roas``); ``config/meta_ads_accounts.json`` already carries each account's
goal bars (``target_cost_per_result`` / ``pause_cost_per_result_above`` / ``target_roas`` /
``pause_roas_floor`` + the ``roas_role`` / ``primary_goal`` that pick the metric). Nothing joined the
two and said *where the account stands*. This module is that join's arithmetic.

It is deliberately **clock-free, reader-free, and config-free** — a pure function of
``(metric, value, spend, policy, as_of)`` — so it is fully unit-testable with no ``FakeMetaReader``,
no FX table, and no registry. The orchestration layer
(:func:`meta_ads_analysis.account_discovery.grade_accounts_against_goals`) owns the fan-out, the
native-metric resolution, and the single clock touch (``as_of`` default = today); it hands this engine
an already-resolved metric value and a flat policy dict, and gets back a :class:`GoalGrade`.

The vocabulary is kept consistent with the rest of the tool on purpose: ``GOAL_PAUSE_CANDIDATE`` is the
same ``"pause_candidate"`` string as :data:`meta_ads_analysis.early_triage.VERDICT_PAUSE_CANDIDATE`
(a sync test enforces the equality; we do not import ``early_triage`` here, which would drag in
``confidence`` / ``config`` and defeat the import-light intent — same discipline as
``account_discovery._AD_DELIVERING`` mirroring ``monitor.DELIVERING``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# Verdicts. GOAL_PAUSE_CANDIDATE MUST equal early_triage.VERDICT_PAUSE_CANDIDATE (== "pause_candidate")
# so the whole tool speaks one pause vocabulary — enforced by a sync test, not an import.
GOAL_ON = "on_goal"
GOAL_WATCH = "watch"
GOAL_PAUSE_CANDIDATE = "pause_candidate"  # == early_triage.VERDICT_PAUSE_CANDIDATE
GOAL_INSUFFICIENT = "insufficient_data"
GOAL_NO_THRESHOLDS = "no_goal_thresholds"  # configured, but no CPR/ROAS bar for its chosen metric
GOAL_NO_CONFIG = "no_goal_configured"  # not in config/meta_ads_accounts.json at all (orchestration-set)

# The two metrics the engine can grade on.
METRIC_COST_PER_RESULT = "cost_per_result"
METRIC_ROAS = "roas"


@dataclass(slots=True)
class GoalGrade:
    """One account's standing against its own goal — the engine's return value.

    - ``verdict``: one of the ``GOAL_*`` constants.
    - ``metric``: the metric it was graded on (``"cost_per_result"`` / ``"roas"``), or ``None`` when no
      grading happened (``GOAL_NO_CONFIG`` — the engine itself always sets a metric).
    - ``value``: the account's native value for ``metric`` (``None`` when insufficient / absent).
    - ``target`` / ``pause_threshold``: the goal bars for ``metric`` from policy (either may be ``None``
      when only a partial goal is configured).
    - ``in_grace``: the account is inside its post-launch evaluation window (softens a
      ``pause_candidate`` to ``watch``; mirrors ``monitor.classify_ad``'s "protected from kill" rule).
    - ``reasons``: human-readable trail of why this verdict fired.
    """

    verdict: str
    metric: str | None
    value: float | None
    target: float | None
    pause_threshold: float | None
    in_grace: bool
    reasons: list[str]


def _num(value: Any) -> float | None:
    """Coerce a policy/metric field to ``float``; ``None`` for absent/blank/non-numeric/``bool``.

    ``bool`` is an ``int`` subclass, so ``True``/``False`` are rejected rather than silently graded as
    ``1.0`` / ``0.0`` — a threshold is never a boolean.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_lower(value: Any) -> str | None:
    """Strip + lowercase a policy string field; ``None`` for absent/blank (case-insensitive compares)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def select_goal_metric(policy: dict[str, Any]) -> str:
    """Pick the metric an account is graded on from its policy — the ``roas_role`` rule.

    - ``roas_role == "not_applicable"`` -> **always** ``cost_per_result`` (never ROAS). A lead-gen
      account (Seattle) that happens to carry a ROAS number is still a cost-per-lead account.
    - else, ROAS-based goal (``primary_goal == "roas"`` OR a ``target_roas`` / ``pause_roas_floor`` bar
      is present) -> ``roas``.
    - else -> ``cost_per_result`` (the default; install/subscription accounts land here too and then
      fall to ``no_goal_thresholds`` because they carry no CPR bar).
    """
    if _clean_lower(policy.get("roas_role")) == "not_applicable":
        return METRIC_COST_PER_RESULT
    has_roas_bar = (
        policy.get("target_roas") is not None or policy.get("pause_roas_floor") is not None
    )
    if _clean_lower(policy.get("primary_goal")) == "roas" or has_roas_bar:
        return METRIC_ROAS
    return METRIC_COST_PER_RESULT


def _in_grace(policy: dict[str, Any], as_of: date) -> bool:
    """Is the account inside its post-launch evaluation grace window as of ``as_of``?

    True only when the policy carries BOTH an ``evaluation_start_date`` (ISO ``YYYY-MM-DD``) and a
    numeric ``evaluation_grace_days``, and ``(as_of - evaluation_start_date).days < grace_days``. When
    ``evaluation_start_date`` is absent (the common case) the account is graded as mature — we never
    fabricate a launch date. A malformed date is treated as absent (not in grace), never a crash.
    """
    start_raw = policy.get("evaluation_start_date")
    if not start_raw:
        return False
    grace_days = _num(policy.get("evaluation_grace_days"))
    if grace_days is None:
        return False
    try:
        start_date = date.fromisoformat(str(start_raw).strip())
    except (TypeError, ValueError):
        return False
    return (as_of - start_date).days < grace_days


def _classify_cost_per_result(
    value: float, target: float | None, pause: float | None, reasons: list[str]
) -> str:
    """Grade a cost-per-result value (LOWER is better) against whatever bars are present."""
    if target is not None and pause is not None:
        if value <= target:
            reasons.append(f"cost_per_result {value:.2f} at/under target {target:.2f}")
            return GOAL_ON
        if value <= pause:
            reasons.append(
                f"cost_per_result {value:.2f} between target {target:.2f} and pause {pause:.2f}"
            )
            return GOAL_WATCH
        reasons.append(f"cost_per_result {value:.2f} above pause threshold {pause:.2f}")
        return GOAL_PAUSE_CANDIDATE
    if pause is not None:  # only a pause bar: can escalate, cannot confirm on_goal.
        reasons.append("no target configured; cannot confirm on_goal")
        if value > pause:
            reasons.append(f"cost_per_result {value:.2f} above pause threshold {pause:.2f}")
            return GOAL_PAUSE_CANDIDATE
        reasons.append(f"cost_per_result {value:.2f} at/under pause threshold {pause:.2f}")
        return GOAL_WATCH
    # only a target bar: can confirm on_goal, cannot escalate to pause_candidate.
    reasons.append("no pause threshold configured; cannot escalate to pause_candidate")
    if value <= target:  # type: ignore[operator]  # target is not None on this branch
        reasons.append(f"cost_per_result {value:.2f} at/under target {target:.2f}")
        return GOAL_ON
    reasons.append(f"cost_per_result {value:.2f} above target {target:.2f}")
    return GOAL_WATCH


def _classify_roas(
    value: float, target: float | None, pause: float | None, reasons: list[str]
) -> str:
    """Grade a ROAS value (HIGHER is better) against whatever bars are present."""
    if target is not None and pause is not None:
        if value >= target:
            reasons.append(f"roas {value:.2f} at/above target {target:.2f}")
            return GOAL_ON
        if value >= pause:
            reasons.append(
                f"roas {value:.2f} between pause floor {pause:.2f} and target {target:.2f}"
            )
            return GOAL_WATCH
        reasons.append(f"roas {value:.2f} below pause floor {pause:.2f}")
        return GOAL_PAUSE_CANDIDATE
    if pause is not None:  # only a pause floor: can escalate, cannot confirm on_goal.
        reasons.append("no target configured; cannot confirm on_goal")
        if value < pause:
            reasons.append(f"roas {value:.2f} below pause floor {pause:.2f}")
            return GOAL_PAUSE_CANDIDATE
        reasons.append(f"roas {value:.2f} at/above pause floor {pause:.2f}")
        return GOAL_WATCH
    # only a target bar: can confirm on_goal, cannot escalate to pause_candidate.
    reasons.append("no pause floor configured; cannot escalate to pause_candidate")
    if value >= target:  # type: ignore[operator]  # target is not None on this branch
        reasons.append(f"roas {value:.2f} at/above target {target:.2f}")
        return GOAL_ON
    reasons.append(f"roas {value:.2f} below target {target:.2f}")
    return GOAL_WATCH


def grade_against_goal(
    *,
    metric: str,
    value: float | None,
    spend: float | None,
    policy: dict[str, Any],
    as_of: date,
) -> GoalGrade:
    """Grade one account's resolved metric ``value`` against its own policy bars for ``metric``.

    ``metric`` is the already-selected metric (:func:`select_goal_metric`); ``value`` its native value
    (``None`` when Meta returned nothing — e.g. no results -> no cost_per_result, no spend -> no roas);
    ``spend`` the account's native spend; ``policy`` a flat dict carrying the account's ``action_policy``
    fields plus ``roas_role``; ``as_of`` the run date (a :class:`datetime.date`, for the grace window).

    Verdict order (each guards the next):

    1. **No applicable threshold** — the chosen metric has neither a target nor a pause bar ->
       :data:`GOAL_NO_THRESHOLDS` (install/subscription accounts land here; distinct from
       ``no_goal_configured`` = no config entry, and from ``insufficient_data`` = transient).
    2. **Insufficient data** — ``value is None``, OR ``spend < min_spend_before_pause`` (too early to
       judge; an absent ``min_spend_before_pause`` -> 0 -> no floor) -> :data:`GOAL_INSUFFICIENT`. Guards
       the cheap-but-zero-results trap: a zero-result account must NOT read ``pause_candidate``.
    3. **Classify** against whatever bars are present (both, or a single partial bar — never crashes).
    4. **Grace softening** — an in-grace account's ``pause_candidate`` softens to ``watch`` (mirrors
       ``monitor.classify_ad``: a protected entity never gets a kill verdict). ``on_goal`` / ``watch``
       are unchanged.
    """
    metric = METRIC_ROAS if metric == METRIC_ROAS else METRIC_COST_PER_RESULT
    in_grace = _in_grace(policy, as_of)
    reasons: list[str] = []

    if metric == METRIC_ROAS:
        target = _num(policy.get("target_roas"))
        pause = _num(policy.get("pause_roas_floor"))
    else:
        target = _num(policy.get("target_cost_per_result"))
        pause = _num(policy.get("pause_cost_per_result_above"))

    # (1) No bar at all for the chosen metric -> configured, but ungradable (not a crash, not a misgrade).
    if target is None and pause is None:
        reasons.append(f"no {metric} target or pause threshold configured for this account's goal")
        return GoalGrade(
            verdict=GOAL_NO_THRESHOLDS,
            metric=metric,
            value=value,
            target=None,
            pause_threshold=None,
            in_grace=in_grace,
            reasons=reasons,
        )

    # (2) Insufficient data: no value yet, or spend below the configured pause floor.
    if value is None:
        reasons.append(f"no {metric} yet (no results/spend in window)")
        return GoalGrade(GOAL_INSUFFICIENT, metric, None, target, pause, in_grace, reasons)
    min_spend = _num(policy.get("min_spend_before_pause")) or 0.0
    effective_spend = spend if spend is not None else 0.0
    if min_spend > 0 and effective_spend < min_spend:
        reasons.append(
            f"spend {effective_spend:.2f} below min_spend_before_pause {min_spend:.2f} "
            "(too early to judge)"
        )
        return GoalGrade(GOAL_INSUFFICIENT, metric, value, target, pause, in_grace, reasons)

    # (3) Classify against the present bars.
    if metric == METRIC_ROAS:
        verdict = _classify_roas(value, target, pause, reasons)
    else:
        verdict = _classify_cost_per_result(value, target, pause, reasons)

    # (4) Grace softening: an in-grace account never reads pause_candidate.
    if in_grace and verdict == GOAL_PAUSE_CANDIDATE:
        reasons.append("within evaluation grace window — softened from pause_candidate to watch")
        verdict = GOAL_WATCH

    return GoalGrade(verdict, metric, value, target, pause, in_grace, reasons)
