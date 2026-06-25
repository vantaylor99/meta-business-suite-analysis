"""Adversarial review gate for operator-facing recommendations (pure, no I/O, no network, no clock).

This is the verification layer that sits ON TOP of ``confidence.py``. Where ``confidence.py`` is the
producer — it computes the band a recommendation *claims* — this module is the **adversary**: a
fresh-eyes pass whose only job is to try to *refute* each recommendation from its own cited evidence
and claimed band, and to correct or drop the ones that cannot survive the challenge before they reach
the operator brief.

The principle (the same one TESS's code-review stage demonstrates): the agent that produced a call is
the worst judge of whether it is grounded. The grounded work moved confidence to a deterministic
rubric, which means the most reliable adversary for the *arithmetic/structural* claims is **code, not
another model** — code can't rubber-stamp, and it can re-derive the band from scratch. So this gate
reasons over the structured ``evidence`` + ``confidence`` blocks **in isolation**, deliberately NOT
trusting the producing ``rationale``'s conclusion (it may *read* the rationale only as the
causal-language probe — that's re-checking, not trusting). It re-derives the band from the same
evidence via :func:`confidence.assess` and compares.

It speaks ONE confidence language with the rest of the repo: the bands/emoji and the
``assess``/``combine_bands``/``grounding_strength`` machinery all come from ``confidence.py``. It
introduces no second confidence scale. It is **read-only** with respect to Meta — it never re-pulls
metrics and never writes to an account; re-pulling over a standard window to catch semantic
cherry-picking is the companion ``adversarial-review-protocol`` doc-procedure's job, not this code's.

The gate can only ever **demote** (lower a band, flip executable→non-executable, demote
approved-eligible→not). It can never raise a band, promote a status, or flip a proposed action into an
executable/approved one — so it sits safely UPSTREAM of the guarded-write approval gate and can never
weaken it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import (
    CONFIDENCE_CONVERSIONS_FLOOR,
    CONFIDENCE_RECENCY_STALE_DAYS,
    MIN_SCALING_SPEND,
    MIN_WASTE_SPEND,
    REVIEW_MIN_WINDOW_DAYS,
)
from .confidence import (
    Band,
    EvidenceTier,
    assess,
    combine_bands,
    evidence_from_dict,
    grounding_strength,
)

# Verdicts, most-conservative → least. The integer rank drives the most-conservative-wins tie-break
# when several checks fire at once.
VERDICT_STANDS = "stands"
VERDICT_DOWNGRADE = "downgrade"
VERDICT_REFUTED = "refuted"
VERDICT_INSUFFICIENT = "insufficient"

_VERDICT_RANK: dict[str, int] = {
    VERDICT_STANDS: 0,
    VERDICT_DOWNGRADE: 1,
    VERDICT_REFUTED: 2,
    VERDICT_INSUFFICIENT: 3,
}

# Per-action-type spend floor for the floor re-check / band recompute. Mirrors
# ``actions._ACTION_SPEND_FLOOR`` so the gate and the producer share floors (we cannot import it —
# ``actions`` pulls in the Meta API client and this module is deliberately pure). A type not listed
# falls back to the ``spend_floor`` passed in.
_ACTION_SPEND_FLOOR: dict[str, float] = {
    "pause_ad": MIN_WASTE_SPEND,
    "increase_adset_budget": MIN_SCALING_SPEND,
    "consider_scale_budget": MIN_SCALING_SPEND,
    "refresh_creative": MIN_WASTE_SPEND,
}

# Action types whose *direction* is a scale-up. For a ROAS-goal account, scaling an entity whose cited
# ROAS is below the goal target contradicts its own number.
_SCALE_ACTIONS = {"increase_adset_budget", "consider_scale_budget"}

# How far above target a paused ad's cited ROAS must sit before pausing it reads as "pausing a winner"
# (a clear self-contradiction). A generous margin keeps borderline pauses — which may be justified for
# reasons outside the cited metric — from being falsely refuted.
_PAUSE_WINNER_MARGIN = 1.5


@dataclass(slots=True)
class ReviewResult:
    """The adversary's verdict on a single recommendation.

    ``verdict`` is the most-conservative outcome across all checks that fired
    (``insufficient`` > ``refuted`` > ``downgrade`` > ``stands``). Every non-``stands`` verdict names
    the specific rubric input(s) that failed in ``failed_inputs`` and a human-readable ``reasons``
    entry for each — a vague "looks wrong" is never an acceptable result. ``stands`` carries empty
    ``reasons``/``failed_inputs``.
    """

    verdict: str
    original_band: str
    revised_band: str | None = None
    reasons: list[str] = field(default_factory=list)
    failed_inputs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Finding:
    """One check's outcome. ``revised_band`` is the band a downgrade check argues the call should be
    capped at (``None`` for non-downgrade findings)."""

    verdict: str
    failed_input: str
    reason: str
    revised_band: Band | None = None


def _band(name: Any) -> Band | None:
    """Coerce a stored band name (``"high"``/``"medium"``/``"low"``/``"abstain"``) to a :class:`Band`,
    or ``None`` if it is missing/unrecognized (never guess a band)."""
    if isinstance(name, Band):
        return name
    try:
        return Band[str(name)]
    except KeyError:
        return None


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _window_bounds(window: Any) -> tuple[date | None, date | None]:
    """Parse a ``"YYYY-MM-DD..YYYY-MM-DD"`` window string into (start, end) dates (either may be
    ``None`` when the string is missing/malformed)."""
    if not window or ".." not in str(window):
        return None, None
    start_s, _, end_s = str(window).partition("..")
    return _parse_date(start_s), _parse_date(end_s)


def _window_span_days(window: Any) -> int | None:
    """Span of a window in days (``end - start``), or ``None`` if unparseable. Matches the convention
    the rest of the codebase uses (``2026-06-10..2026-06-24`` is a "14-day window")."""
    start, end = _window_bounds(window)
    if start is None or end is None:
        return None
    return (end - start).days


def _recency_days_from_window(run_date: Any, window: Any) -> int | None:
    """Days between the window's end and the plan ``run_date`` — the same recency the producer fed
    :func:`confidence.assess`. ``None`` when either date is missing (assess then rounds down)."""
    run = _parse_date(run_date)
    _, end = _window_bounds(window)
    if run is None or end is None:
        return None
    return (run - end).days


def _spend_floor_for(action_type: Any, default: float) -> float:
    return _ACTION_SPEND_FLOOR.get(str(action_type or ""), default)


def review_recommendation(
    *,
    evidence: dict[str, Any],
    confidence: dict[str, Any],
    action: dict[str, Any],
    policy: dict[str, Any] | None = None,
    spend_floor: float,
    conversions_floor: float,
    min_window_days: int,
    recency_stale_days: int,
    recency_days: int | None = None,
) -> ReviewResult:
    """Adversarially review one recommendation from its cited evidence + claimed band ONLY.

    Runs every refutation check, then returns the **most-conservative** verdict while accumulating a
    reason (and the failing rubric-input key) from every check that fired — so the operator sees all
    problems, not just the worst. Conservative throughout: when an input is ambiguous, round toward
    refute.

    ``policy`` supplies the account goal/target for the ``direction`` check (the same
    ``account_action_policy`` ``briefs._account_goal`` reads). ``recency_days`` is the producer's
    recency (days since the window end) used to make the band recompute faithful; when ``None`` the
    recompute rounds down exactly as the producer did. ``recency_stale_days`` is accepted so the gate
    shares the producer's staleness knee; the canonical recompute defers to :func:`confidence.assess`,
    which already applies that same constant.
    """
    policy = policy or {}
    original_band = _band(confidence.get("band"))
    if original_band is None:
        # No claimed band to judge — nothing to refute. (review_action_plan only calls us for
        # confidence-bearing actions, so this is a defensive no-op.)
        return ReviewResult(verdict=VERDICT_STANDS, original_band=str(confidence.get("band") or ""))

    findings: list[_Finding] = []
    purchases = _num(evidence.get("sample_purchases"))
    spend = _num(evidence.get("sample_spend"))
    sample_cited = purchases is not None or spend is not None
    tier = confidence.get("grounding_tier")

    # 1. sample_floor — cited sample clears NEITHER the conversions nor the spend floor → abstain.
    if sample_cited:
        cleared_conversions = (purchases or 0.0) >= conversions_floor
        cleared_spend = (spend or 0.0) >= spend_floor
        if not cleared_conversions and not cleared_spend:
            findings.append(
                _Finding(
                    verdict=VERDICT_INSUFFICIENT,
                    failed_input="sample_floor",
                    reason=(
                        f"sample of {_fmt_count(purchases)} purchases / {_fmt_spend(spend)} spend is "
                        f"below the {conversions_floor:g}-purchase floor — should abstain"
                    ),
                )
            )

    # 2. window_length — window span shorter than the representative minimum → downgrade one band.
    span = _window_span_days(evidence.get("window"))
    if span is not None and span < min_window_days:
        findings.append(
            _Finding(
                verdict=VERDICT_DOWNGRADE,
                failed_input="window_length",
                reason=(
                    f"{span}-day window may be unrepresentative; recommend a wider window "
                    f"(at least {min_window_days} days)"
                ),
                revised_band=_one_lower(original_band),
            )
        )

    # 3. causal — a causal claim from non-experimental data whose band exceeds the causal-capped band.
    if bool(confidence.get("causal_flag")) and tier != EvidenceTier.ab_experiment.name:
        capped = _causal_capped_band(confidence, tier)
        if capped is not None and original_band > capped:
            findings.append(
                _Finding(
                    verdict=VERDICT_DOWNGRADE,
                    failed_input="causal",
                    reason="correlational — confirm via A/B before trusting the band",
                    revised_band=capped,
                )
            )

    # 4. band_earned — recompute the band from the cited evidence + tier (NOT the rationale). If the
    #    claimed band exceeds the recompute, the band drifted above its inputs → downgrade.
    if sample_cited:
        recomputed = _recompute_band(
            evidence=evidence,
            tier=tier,
            spend_floor=spend_floor,
            conversions_floor=conversions_floor,
            recency_days=recency_days,
        )
        if recomputed is not None and original_band > recomputed:
            findings.append(
                _Finding(
                    verdict=VERDICT_DOWNGRADE,
                    failed_input="band_earned",
                    reason="stated confidence exceeds what the rubric inputs (sample/recency/tier) support",
                    revised_band=recomputed,
                )
            )

    # 5. direction — the action's direction contradicts its own cited metric vs the account goal.
    direction = _direction_contradiction(action=action, evidence=evidence, policy=policy)
    if direction is not None:
        findings.append(
            _Finding(
                verdict=VERDICT_REFUTED,
                failed_input="direction",
                reason=direction,
            )
        )

    # 6. external — external evidence is a hypothesis, never a confirmation: cap live calls at low.
    if tier == EvidenceTier.external.name and original_band > Band.low:
        findings.append(
            _Finding(
                verdict=VERDICT_DOWNGRADE,
                failed_input="external",
                reason="external evidence is a hypothesis, not confirmation — route to `experiment define`",
                revised_band=Band.low,
            )
        )

    return _resolve(original_band, findings)


def _resolve(original_band: Band, findings: list[_Finding]) -> ReviewResult:
    """Combine findings: most-conservative verdict wins, all reasons/inputs accumulate. A downgrade
    whose revised band lands on ``abstain`` becomes an ``insufficient`` verdict (renders as
    ⚪ "keep running", never 🔴 Low)."""
    if not findings:
        return ReviewResult(verdict=VERDICT_STANDS, original_band=original_band.name)

    reasons = [f.reason for f in findings]
    failed_inputs = [f.failed_input for f in findings]
    verdict = max((f.verdict for f in findings), key=lambda v: _VERDICT_RANK[v])

    revised_band: Band | None = None
    if verdict == VERDICT_DOWNGRADE:
        targets = [f.revised_band for f in findings if f.revised_band is not None]
        # Most-conservative target wins; never weaker than one band below the claim.
        revised = min(targets, default=_one_lower(original_band))
        revised = min(revised, _one_lower(original_band))
        if revised <= Band.abstain:
            verdict = VERDICT_INSUFFICIENT
            revised_band = Band.abstain
        else:
            revised_band = revised
    elif verdict == VERDICT_INSUFFICIENT:
        revised_band = Band.abstain

    return ReviewResult(
        verdict=verdict,
        original_band=original_band.name,
        revised_band=revised_band.name if revised_band is not None else None,
        reasons=reasons,
        failed_inputs=failed_inputs,
    )


def _one_lower(band: Band) -> Band:
    return Band(max(Band.abstain, band - 1))


def _causal_capped_band(confidence: dict[str, Any], tier: Any) -> Band | None:
    """The band the call WOULD read if the causal guard were applied: the data axis combined with the
    grounding ceiling downgraded for a non-experimental causal claim. Used to verify the producer
    actually applied the guard (and to catch a band that escaped it)."""
    data_band = _band(confidence.get("data_band"))
    if data_band is None:
        return None
    try:
        capped_grounding, _ = grounding_strength(tier, causal_claim=True)
    except ValueError:
        return None
    return combine_bands(data_band, capped_grounding)


def _recompute_band(
    *,
    evidence: dict[str, Any],
    tier: Any,
    spend_floor: float,
    conversions_floor: float,
    recency_days: int | None,
) -> Band | None:
    """Re-derive the band from the cited evidence + tier via :func:`confidence.assess`, deliberately
    ignoring the rationale (no ``causal_text``) and any pre-baked band. This is the structural defense
    against a drifted / hand-edited band: it answers "what does the rubric actually support here?"
    Returns ``None`` when the tier is unrecognized (cannot recompute → do not fabricate a verdict)."""
    try:
        EvidenceTier[str(tier)]
    except KeyError:
        return None
    recomputed = assess(
        evidence=evidence_from_dict(evidence),
        tier=tier,
        spend_floor=spend_floor,
        conversions_floor=conversions_floor,
        recency_days=recency_days,
    )
    return recomputed.band


def _direction_contradiction(
    *,
    action: dict[str, Any],
    evidence: dict[str, Any],
    policy: dict[str, Any],
) -> str | None:
    """For a ROAS-goal account, return a reason string when the action's direction contradicts its own
    cited ROAS vs the account target, else ``None``. Conservative: only fires on a ROAS goal with a
    numeric target and a cited ROAS metric (install-goal direction is intentionally not judged here)."""
    if policy.get("primary_goal") != "roas":
        return None
    target = _num(policy.get("target_roas"))
    if target is None:
        return None
    if evidence.get("metric_name") != "blended_roas":
        return None
    roas = _num(evidence.get("metric_value"))
    if roas is None:
        return None
    action_type = str(action.get("action_type") or "")
    if action_type in _SCALE_ACTIONS and roas < target:
        return (
            f"recommendation contradicts its cited metric vs the account goal: scaling an entity "
            f"whose ROAS {roas:.2f} is below the {target:g} target"
        )
    if action_type == "pause_ad" and roas >= target * _PAUSE_WINNER_MARGIN:
        return (
            f"recommendation contradicts its cited metric vs the account goal: pausing an entity "
            f"whose ROAS {roas:.2f} is comfortably above the {target:g} target"
        )
    return None


def review_result_to_dict(result: ReviewResult) -> dict[str, Any]:
    """Serialize a :class:`ReviewResult` to the JSON shape stored on an action's ``review`` block."""
    return {
        "verdict": result.verdict,
        "original_band": result.original_band,
        "revised_band": result.revised_band,
        "reasons": list(result.reasons),
        "failed_inputs": list(result.failed_inputs),
    }


def review_action_plan(
    plan: dict[str, Any],
    *,
    spend_floor: float = MIN_WASTE_SPEND,
    conversions_floor: float = CONFIDENCE_CONVERSIONS_FLOOR,
    min_window_days: int = REVIEW_MIN_WINDOW_DAYS,
    recency_stale_days: int = CONFIDENCE_RECENCY_STALE_DAYS,
) -> dict[str, Any]:
    """Return a NEW plan (the input is never mutated) in which every recommendation-bearing action
    carries a ``review`` block and has its ``confidence``/``status``/``executable`` adjusted per
    verdict.

    Only actions carrying a ``confidence`` block (``pause_ad``, ``increase_adset_budget``,
    ``consider_scale_budget``, ``refresh_creative``) are reviewed — informational actions
    (``measurement_review``, ``disable_meta_ai_controls`` follow-ups, anything without
    confidence/evidence) pass through untouched. An action that already carries a ``review`` block is
    left as-is, which makes the gate idempotent: reviewing an already-reviewed plan changes nothing.
    """
    reviewed = _deepcopy_plan(plan)
    policy = plan.get("account_action_policy") if isinstance(plan.get("account_action_policy"), dict) else {}
    run_date = plan.get("run_date")

    for action in reviewed.get("actions") or []:
        if not isinstance(action, dict):
            continue
        confidence = action.get("confidence")
        if not isinstance(confidence, dict) or _band(confidence.get("band")) is None:
            continue  # not a recommendation-bearing action — skip entirely
        if isinstance(action.get("review"), dict) and action["review"]:
            continue  # already reviewed — idempotent no-op
        evidence = action.get("evidence") if isinstance(action.get("evidence"), dict) else {}
        result = review_recommendation(
            evidence=evidence,
            confidence=confidence,
            action=action,
            policy=policy,
            spend_floor=_spend_floor_for(action.get("action_type"), spend_floor),
            conversions_floor=conversions_floor,
            min_window_days=min_window_days,
            recency_stale_days=recency_stale_days,
            recency_days=_recency_days_from_window(run_date, evidence.get("window")),
        )
        action["review"] = review_result_to_dict(result)
        _apply_verdict(action, result)

    return reviewed


def _apply_verdict(action: dict[str, Any], result: ReviewResult) -> None:
    """Apply one verdict to an action IN PLACE. Only ever demotes — never raises a band, never
    promotes a status, never flips executable to true."""
    confidence = action.get("confidence")
    if not isinstance(confidence, dict):
        return

    if result.verdict == VERDICT_STANDS:
        return

    if result.verdict == VERDICT_DOWNGRADE and result.revised_band is not None:
        revised = result.revised_band
        confidence["band"] = revised
        # Cap both axes at the revised band so combine_bands(data, grounding) stays == band.
        confidence["data_band"] = _min_band_name(confidence.get("data_band"), revised)
        confidence["grounding_band"] = _min_band_name(confidence.get("grounding_band"), revised)
        confidence["factors"] = list(confidence.get("factors") or []) + list(result.reasons)
        return

    if result.verdict == VERDICT_INSUFFICIENT:
        # Pin the data axis to abstain (the absence of a score) — combine yields abstain. Mirrors the
        # producer's below-floor abstention: a promising test to keep running, never a winner/loser.
        confidence["band"] = Band.abstain.name
        confidence["data_band"] = Band.abstain.name
        confidence["factors"] = list(confidence.get("factors") or []) + list(result.reasons)
        action["executable"] = False
        action["status"] = "proposed"
        action["verdict"] = "insufficient_data"
        action["rationale"] = (
            "Insufficient data to act on yet — review found the cited evidence below the significance "
            "floor. Treat as a promising test: keep running and re-check as more data accrues."
        )
        return

    if result.verdict == VERDICT_REFUTED:
        confidence["factors"] = list(confidence.get("factors") or []) + list(result.reasons)
        action["executable"] = False
        action["verdict"] = "refuted"
        if action.get("status") == "approved":
            action["status"] = "proposed"  # demote out of approved — never the reverse
        return


def _min_band_name(existing: Any, revised: str) -> str:
    existing_band = _band(existing)
    revised_band = _band(revised) or Band.abstain
    if existing_band is None:
        return revised_band.name
    return combine_bands(existing_band, revised_band).name


def _deepcopy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy the plan but deep-copy each action (and its nested dicts we mutate), so the input
    plan is never mutated. Plans are JSON, so a recursive copy is sufficient and clock-free."""
    import copy

    return copy.deepcopy(plan)


def _fmt_count(value: float | None) -> str:
    if value is None:
        return "0"
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"


def _fmt_spend(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.0f}"
