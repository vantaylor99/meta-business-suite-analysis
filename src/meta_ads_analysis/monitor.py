"""Runaway / outlier watch scanner (read-only, flag-only).

Catches ads that are spending while performing badly — WITHOUT killing promising ads too early.
A cheap deterministic triage classifies each delivering ad as urgent / underperforming / watch /
ok using context-aware guards; the agent then reasons over the flagged few case-by-case and pauses
(if warranted) through the normal guarded flow. This module never writes to the account.

Protective by design:
- **Significance floor**: ignore ads that haven't spent enough to judge (protects brand-new ads).
- **Recently-created-or-changed grace**: an ad created/edited within `grace_days` is "learning" →
  classified `watch`, NEVER `urgent`. Using `updated_time` means a just-re-enabled/edited ad (the
  mid-relearn case) is protected automatically, no decision-log parsing needed.
- **Account goal anchored**: ROAS floor / target come from the account's action policy.

Persistence: a watchlist tracks how many consecutive scans an ad has been flagged, so a *consistent*
underperformer is distinguishable from a one-day blip.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import account_registry
from .config import (
    CONFIDENCE_CONVERSIONS_FLOOR,
    DEFAULT_REPORTS_ROOT,
    EARLY_LIFE_DECISION_AGE,
    EARLY_LIFE_MAX_AGE,
    EARLY_LIFE_MIN_ANALOGS,
    EARLY_LIFE_RECOVERY_HORIZON,
    EARLY_LIFE_RECOVERY_RATE,
    EARLY_LIFE_STRONG_ANALOGS,
)
from .confidence import (
    Confidence,
    Evidence,
    EvidenceTier,
    abstain_confidence,
    assess,
    build_regenerating_query,
    confidence_to_dict,
    evidence_to_dict,
)
from .control import fetch_entity_metrics
from .early_triage import (
    AdHistory,
    HistoryProvider,
    OWN_SAMPLE_INSUFFICIENT,
    OWN_SAMPLE_PAUSE,
    VERDICT_NOT_STRUGGLING,
    VERDICT_PAUSE_CANDIDATE,
    classify_own_sample,
    goal_kind,
    triage_ad,
)
from .followups import EARLY_LIFE_MARKER, Followup, early_life_ad_id, early_life_slug
from .meta_api import MetaMarketingApiClient
from .reader_provider import MetaReaderProvider, as_reader
from .utils import ensure_dir, write_json

AD_META_FIELDS = ["id", "name", "status", "effective_status", "adset_id", "created_time", "updated_time"]
DELIVERING = {"ACTIVE", "IN_PROCESS"}


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _abstain_confidence(factors: list[str]) -> Confidence:
    """The monitor's "too thin / too young to judge" abstention, expressed through the shared
    :func:`confidence.abstain_confidence` factory so every :class:`~confidence.Confidence` is still
    constructed in one place. ``abstain`` is the deliberate refusal to score data the significance
    floor (too little spend) or the grace window (a still-learning ad) deems untrustworthy — NOT a
    fabricated number. Grounding is ``direct_observation`` (live metrics), but the data axis abstains,
    so the combined verdict abstains (the weaker axis governs)."""
    return abstain_confidence(
        tier=EvidenceTier.direct_observation,
        factors=factors,
        would_raise="more spend past the significance floor / a matured (post-learning) window",
        causal_claim=False,
    )


def classify_ad(
    *,
    spend: float,
    roas: float | None,
    results: float | None,
    days_since_change: int | None,
    accelerating: bool,
    min_spend: float,
    grace_days: int,
    roas_floor: float,
    roas_target: float,
    recency_days: int | None = 0,
) -> dict[str, Any]:
    """Pure classification of one ad. Returns classification + reasons + $ at risk + a ``confidence``
    block in the shared :mod:`confidence` vocabulary.

    ``recency_days`` is the staleness of the *data window* (days since its end), passed in so this
    stays clock-free; it is 0 for a watch scan whose window ends at ``as_of``. The significance floor
    (``insufficient``) and the protective grace window both map to ``abstain`` — the same
    "insufficient data" verdict the action plan uses — while ``urgent``/``underperforming`` rows get
    a ``direct_observation`` data band computed from spend/purchases/recency.
    """
    metric_display = f"ROAS {roas:.2f}" if roas is not None else "ROAS n/a"
    evidence = Evidence(
        metric_name="roas", metric_value=roas, metric_display=metric_display,
        window="n/a", sample_conversions=results, sample_spend=spend,
        entity_level="ad", entity_id=None, entity_name=None, regenerating_query=None,
    )

    if spend < min_spend:
        reasons = [f"only ${spend:.0f} spent (< ${min_spend:.0f} significance floor) — too early to judge"]
        return {"classification": "insufficient", "dollars_at_risk": 0.0, "reasons": reasons,
                "confidence": confidence_to_dict(_abstain_confidence(reasons))}

    r = roas if roas is not None else 0.0
    dollars_at_risk = round(spend * max(0.0, 1.0 - (r / roas_target)), 2)
    reasons = []
    protected = days_since_change is not None and days_since_change < grace_days
    if protected:
        reasons.append(f"created/changed {days_since_change}d ago (< {grace_days}d) — learning, protected from kill")
        if r < roas_floor:
            reasons.append(f"ROAS {r:.2f} is below floor {roas_floor} but it's too young to judge")
        conf = _abstain_confidence(reasons + ["too young to judge — abstain, keep running"])
        return {"classification": "watch", "dollars_at_risk": dollars_at_risk, "reasons": reasons,
                "confidence": confidence_to_dict(conf)}

    if r < roas_floor:
        reasons.append(f"ROAS {r:.2f} < pause floor {roas_floor} on ${spend:.0f}")
        if not results:
            reasons.append("~0 results")
        if accelerating:
            reasons.append("spend accelerating vs its recent average")
        cls = "urgent"
    elif r < roas_target:
        reasons.append(f"ROAS {r:.2f} below target {roas_target} (above floor {roas_floor})")
        cls = "underperforming"
    else:
        cls = "ok"

    conf = assess(
        evidence=evidence, tier=EvidenceTier.direct_observation,
        spend_floor=min_spend, conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR,
        recency_days=recency_days, causal_text="; ".join(reasons) or None,
    )
    return {"classification": cls, "dollars_at_risk": dollars_at_risk, "reasons": reasons,
            "confidence": confidence_to_dict(conf)}


def _resolve_policy(account_slug: str) -> dict[str, Any]:
    """The account's action policy (``primary_goal``, ROAS floors, install-cost target, …), or ``{}``
    when unknown. Shared by :func:`_policy_floors` and the early-life triage so the watch scan and the
    engine pick the SAME goal-based metric."""
    try:
        return dict(account_registry.resolve_account(account_slug).action_policy or {})
    except Exception:
        return {}


def _floors_from_policy(policy: dict[str, Any]) -> tuple[float, float]:
    floor = policy.get("pause_roas_floor") or 1.5
    target = policy.get("target_roas") or policy.get("scale_roas_floor") or 3.0
    return float(floor), float(target)


def _policy_floors(account_slug: str) -> tuple[float, float]:
    return _floors_from_policy(_resolve_policy(account_slug))


def _dollars_at_risk(spend: float, roas: float | None, roas_target: float) -> float:
    """The same waste estimate :func:`classify_ad` uses — spend scaled by how far ROAS sits below the
    account target (0 when at/above target, or when target is undefined)."""
    if not roas_target:
        return 0.0
    r = roas if roas is not None else 0.0
    return round(spend * max(0.0, 1.0 - (r / roas_target)), 2)


# --------------------------------------------------------------------------------------------------
# Early-life triage integration. The grace window correctly PROTECTS recently-changed ads, but it does
# so *silently* for brand-new ones — a dead new ad and a slow-start winner are indistinguishable. For
# genuinely brand-new ads (age ≤ EARLY_LIFE_MAX_AGE, age measured from the provider's first_seen, NOT
# the edit-based days_since_change) the early-life triage SUPERSEDES the blanket grace/insufficient
# abstain: it grades the struggling young ad against the account's own comparable new ads and either
# keeps it on a day-3 probation (a follow-up is filed) or surfaces it as a pause candidate. By the
# decision age a probated ad is forced to a real keep/kill so it never abstains indefinitely.
# build_watch_report stays write-free: it RETURNS the follow-up file/close actions for the CLI to apply.
# --------------------------------------------------------------------------------------------------

# Early-life row classifications (in addition to the existing urgent / underperforming / watch / ok).
EARLY_PAUSE_CANDIDATE = "pause_candidate"


def _early_life_row(
    *,
    ad_id: str,
    info: dict[str, Any],
    window_metrics: dict[str, Any],
    classification: str,
    verdict: str,
    age: int,
    reasons: list[str],
    confidence: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
    analog_basis: dict[str, Any],
    dollars_at_risk: float = 0.0,
) -> dict[str, Any]:
    """One early-life watch-report row. Keeps the existing row fields (so the CLI renderer and any
    consumer still works) and adds the early-life block: ``early_life``, ``age``, ``verdict``,
    ``analog_basis`` (plus the shared ``confidence``/``evidence``/``reasons``)."""
    spend = window_metrics.get("spend") or 0.0
    return {
        "ad_id": ad_id,
        "ad_name": info.get("name"),
        "adset_id": info.get("adset_id"),
        "classification": classification,
        "early_life": True,
        "age": age,
        "verdict": verdict,
        "spend": round(spend, 2),
        "roas": window_metrics.get("roas"),
        "purchases": window_metrics.get("purchases"),
        "dollars_at_risk": round(dollars_at_risk, 2),
        "days_since_change": None,
        "accelerating": False,
        "times_flagged": 0,
        "reasons": reasons,
        "confidence": confidence,
        "evidence": evidence,
        "analog_basis": analog_basis,
    }


def _early_life_file_action(
    *,
    ad_id: str,
    info: dict[str, Any],
    history: AdHistory,
    verdict: Any,
    account_slug: str,
    decision_age: int,
) -> dict[str, Any]:
    """A deterministic, clock-free ``file`` follow-up action for the CLI to apply. ``due`` is derived
    from ``first_seen + decision_age`` (NOT the wall clock); the note carries the analog basis so the
    day-3 reader has context without re-deriving it."""
    due = (history.first_seen + timedelta(days=decision_age)).isoformat()
    basis = verdict.analog_basis or {}
    name = info.get("name") or "unnamed"
    note = (
        f"Ad {ad_id} ({name}) was kept on early-life probation at age {verdict.age}.\n\n"
        f"Analog basis: {basis.get('analogs', 0)} comparable new ad(s) at age {basis.get('age')}, "
        f"{basis.get('recovered', 0)} recovered (rate {basis.get('rate', 0.0):.0%}).\n\n"
        f"Verdict at filing: {verdict.verdict}. " + " ".join(verdict.reasons) + "\n\n"
        "On/after the due date, re-run `watch_account` (the scan forces a keep/kill decision and "
        "closes this follow-up automatically) or decide manually and route a pause through "
        "`propose-pause-ads`."
    )
    return {
        "action": "file",
        "account": account_slug,
        "ad_id": ad_id,
        "slug": early_life_slug(ad_id),
        "marker": EARLY_LIFE_MARKER,
        "title": f"Early-life day-{decision_age + 1} keep/kill decision — {name}",
        "due": due,
        "note": note,
    }


def _early_life_forced_decision(
    *,
    ad_id: str,
    info: dict[str, Any],
    window_metrics: dict[str, Any],
    histories: list[AdHistory],
    as_of: date,
    account_slug: str,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    min_spend: float,
    grace_days: int,
    accelerating: bool,
    open_followup: Followup,
    el: dict[str, Any],
    age: int,
    win_from: str,
    to: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Force a confident keep/kill for an ad we deliberately put on probation, then close the
    probation follow-up. Returns ``(row, close_action)``.

    The grace-window abstain is EXPLICITLY overridden here (``days_since_change=None`` → unprotected):
    we chose to probate this ad, so it is owed a decision, not another protective abstain.

    The own-sample grade is GOAL-AWARE: a ROAS-goal account grades on ROAS (``classify_ad``); an
    install-goal account grades on cost-per-install (:func:`_forced_decision_install`), since an
    install ad books ~0 purchases by design and a ROAS-only grade would wrongly force-pause it.

    - Own life-to-date sample clears the significance floor → a real direct-observation decision
      governs: below the goal bar (ROAS pause floor / install cost target) ⇒ pause candidate, else keep.
    - Still below the floor → the correlational analog verdict governs (no indefinite abstain):
      ``pause_candidate`` ⇒ pause candidate, otherwise keep (shared :func:`_forced_decision_analog`).
    """
    close_action = {
        "action": "close",
        "account": account_slug,
        "ad_id": ad_id,
        "task_id": open_followup.task_id,
    }
    m = window_metrics
    spend = m.get("spend") or 0.0

    # Goal-aware own-sample grade. ROAS goals keep the ROAS classify_ad path below (correct for ROAS,
    # and covered by the existing forced-decision tests); install goals grade the OWN sample on
    # cost-per-install so a healthy install ad (few/zero purchases by design) is not force-paused on a
    # ~0 ROAS. Both kinds fall through to the SAME analog path when the own sample is below the floor.
    if goal_kind(policy) == "install":
        return _forced_decision_install(
            ad_id=ad_id,
            info=info,
            window_metrics=m,
            histories=histories,
            as_of=as_of,
            account_slug=account_slug,
            policy=policy,
            roas_floor=roas_floor,
            roas_target=roas_target,
            min_spend=min_spend,
            el=el,
            age=age,
            win_from=win_from,
            to=to,
            close_action=close_action,
        )

    # Direct call with grace DELIBERATELY disabled (see docstring). For a genuinely young ad the
    # trailing window ≈ its life-to-date sample, so this is its own observed performance.
    direct = classify_ad(
        spend=spend,
        roas=m.get("roas"),
        results=m.get("purchases"),
        days_since_change=None,
        accelerating=accelerating,
        min_spend=min_spend,
        grace_days=grace_days,
        roas_floor=roas_floor,
        roas_target=roas_target,
        recency_days=0,
    )
    dcls = direct["classification"]
    if dcls != "insufficient":
        roas_val = m.get("roas")
        evidence = Evidence(
            metric_name="roas",
            metric_value=roas_val,
            metric_display=f"ROAS {roas_val:.2f}" if roas_val is not None else "ROAS n/a",
            window=f"{win_from}..{to}",
            sample_conversions=m.get("purchases"),
            sample_spend=round(spend, 2),
            entity_level="ad",
            entity_id=ad_id,
            entity_name=info.get("name"),
            regenerating_query=build_regenerating_query(account_slug, "ad", win_from, to),
        )
        if dcls == "urgent":
            classification, verdict = EARLY_PAUSE_CANDIDATE, EARLY_PAUSE_CANDIDATE
            head = (
                f"day-{el['decision_age'] + 1} decision (age {age}): own sample cleared the "
                "significance floor and is below the pause floor — pause candidate"
            )
        else:
            classification, verdict = "watch", "keep"
            head = (
                f"day-{el['decision_age'] + 1} decision (age {age}): own sample cleared the "
                "significance floor and held at/above the pause floor — keep"
            )
        row = _early_life_row(
            ad_id=ad_id,
            info=info,
            window_metrics=m,
            classification=classification,
            verdict=verdict,
            age=age,
            reasons=[head, *direct["reasons"]],
            confidence=direct["confidence"],
            evidence=evidence_to_dict(evidence),
            analog_basis={"analogs": 0, "decision": "direct_observation"},
            dollars_at_risk=direct["dollars_at_risk"],
        )
        return row, close_action

    # Own sample still below the significance floor at the decision age → the analog verdict governs.
    return _forced_decision_analog(
        ad_id=ad_id,
        info=info,
        window_metrics=m,
        histories=histories,
        as_of=as_of,
        account_slug=account_slug,
        policy=policy,
        roas_floor=roas_floor,
        roas_target=roas_target,
        el=el,
        age=age,
        close_action=close_action,
    )


def _forced_decision_analog(
    *,
    ad_id: str,
    info: dict[str, Any],
    window_metrics: dict[str, Any],
    histories: list[AdHistory],
    as_of: date,
    account_slug: str,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    el: dict[str, Any],
    age: int,
    close_action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The own-sample-insufficient tail of :func:`_early_life_forced_decision`, shared by both the
    ROAS and install branches. The probated ad's own sample is below the significance floor, so the
    correlational analog verdict governs the keep-vs-pause call (no indefinite abstain):
    ``pause_candidate`` ⇒ pause candidate, otherwise keep. Always returns the ``close_action`` — a
    probation is owed a decision regardless of verdict."""
    m = window_metrics
    spend = m.get("spend") or 0.0
    # Override the engine's age gate (max_age=age) so a late scan still grades instead of returning None.
    v = triage_ad(
        ad_id=ad_id,
        account_slug=account_slug,
        as_of=as_of,
        histories=histories,
        policy=policy,
        roas_floor=roas_floor,
        roas_target=roas_target,
        early_life_max_age=max(age, el["max_age"]),
        decision_age=el["decision_age"],
        recovery_horizon=el["recovery_horizon"],
        min_analogs=el["min_analogs"],
        strong_analogs=el["strong_analogs"],
        recovery_rate=el["recovery_rate"],
    )
    head = (
        f"day-{el['decision_age'] + 1} decision (age {age}): still below the significance floor — "
        "analog verdict governs (no indefinite abstain). "
    )
    if v is None:
        # No gradable history (should not happen — this ad has history). Keep + close, never loop.
        row = _early_life_row(
            ad_id=ad_id,
            info=info,
            window_metrics=m,
            classification="watch",
            verdict="keep",
            age=age,
            reasons=[head + "no comparable history — keep."],
            confidence=None,
            evidence=None,
            analog_basis={"analogs": 0},
            dollars_at_risk=_dollars_at_risk(spend, m.get("roas"), roas_target),
        )
        return row, close_action
    classification = EARLY_PAUSE_CANDIDATE if v.verdict == VERDICT_PAUSE_CANDIDATE else "watch"
    row = _early_life_row(
        ad_id=ad_id,
        info=info,
        window_metrics=m,
        classification=classification,
        verdict=v.verdict,
        age=age,
        reasons=[head + r for r in v.reasons] or [head],
        confidence=v.confidence,
        evidence=v.evidence,
        analog_basis=v.analog_basis,
        dollars_at_risk=_dollars_at_risk(spend, m.get("roas"), roas_target),
    )
    return row, close_action


def _forced_decision_install(
    *,
    ad_id: str,
    info: dict[str, Any],
    window_metrics: dict[str, Any],
    histories: list[AdHistory],
    as_of: date,
    account_slug: str,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    min_spend: float,
    el: dict[str, Any],
    age: int,
    win_from: str,
    to: str,
    close_action: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Install-goal forced decision: grade the probated ad's OWN window on cost-per-install (NOT ROAS,
    which an install ad books ~0 of by design). Below the significance floor → defer to the analog
    path; above it → a direct-observation keep/kill on the install metric. Always returns the
    ``close_action`` (a probation is owed a decision regardless of verdict)."""
    m = window_metrics
    spend = m.get("spend") or 0.0
    own = classify_own_sample(
        spend=spend,
        purchase_value=m.get("purchase_value"),
        purchases=m.get("purchases"),
        app_installs=m.get("app_installs"),
        policy=policy,
        roas_floor=roas_floor,
        roas_target=roas_target,
        min_spend=min_spend,
    )
    if own.verdict == OWN_SAMPLE_INSUFFICIENT:
        return _forced_decision_analog(
            ad_id=ad_id,
            info=info,
            window_metrics=m,
            histories=histories,
            as_of=as_of,
            account_slug=account_slug,
            policy=policy,
            roas_floor=roas_floor,
            roas_target=roas_target,
            el=el,
            age=age,
            close_action=close_action,
        )

    cpi = own.metric_value
    evidence = Evidence(
        metric_name="cost_per_app_install",
        metric_value=cpi,
        metric_display=f"cost/install ${cpi:.2f}" if cpi is not None else "cost/install n/a",
        window=f"{win_from}..{to}",
        sample_conversions=own.results,  # installs as the conversion count (assess is metric-agnostic)
        sample_spend=round(spend, 2),
        entity_level="ad",
        entity_id=ad_id,
        entity_name=info.get("name"),
        regenerating_query=build_regenerating_query(account_slug, "ad", win_from, to),
    )
    if own.verdict == OWN_SAMPLE_PAUSE:
        classification = verdict = EARLY_PAUSE_CANDIDATE
        head = (
            f"day-{el['decision_age'] + 1} decision (age {age}): own sample cleared the "
            "significance floor and is over the cost-per-install target — pause candidate"
        )
        dollars_at_risk = round(spend, 2)  # the whole window spend is the waste estimate (no ROAS)
    else:
        classification, verdict = "watch", "keep"
        head = (
            f"day-{el['decision_age'] + 1} decision (age {age}): own sample cleared the "
            "significance floor and held at/under the cost-per-install target — keep"
        )
        dollars_at_risk = 0.0
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=min_spend,
        conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR,
        recency_days=0,
        causal_text="; ".join(own.reasons) or None,
    )
    row = _early_life_row(
        ad_id=ad_id,
        info=info,
        window_metrics=m,
        classification=classification,
        verdict=verdict,
        age=age,
        reasons=[head, *own.reasons],
        confidence=confidence_to_dict(conf),
        evidence=evidence_to_dict(evidence),
        analog_basis={"analogs": 0, "decision": "direct_observation"},
        dollars_at_risk=dollars_at_risk,
    )
    return row, close_action


def _early_life_branch(
    *,
    ad_id: str,
    info: dict[str, Any],
    window_metrics: dict[str, Any],
    history: AdHistory | None,
    histories: list[AdHistory],
    as_of: date,
    account_slug: str,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    min_spend: float,
    grace_days: int,
    accelerating: bool,
    open_followup: Followup | None,
    el: dict[str, Any],
    win_from: str,
    to: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Early-life triage for one delivering ad. Returns ``(row, followup_action, handled)``.

    ``handled is False`` → this ad is not early-life (no provider history, or too old and never put on
    probation): the caller falls through to the normal ``classify_ad`` path, so there is no
    contradiction with the grace window. ``handled is True`` → the triage is the single source of
    truth for this ad; ``row`` (if any) is appended and ``followup_action`` (if any) returned for the
    CLI to apply (the scan itself never touches the filesystem)."""
    if history is None:
        return None, None, False  # no synced history yet → fall back to today's behavior

    age = max(0, history.age_on(as_of))
    on_probation = open_followup is not None

    # Day-3 forced decision: a probated ad must get a real keep/kill by the decision age — reachable
    # even past max_age (a late scan) precisely because the open follow-up says we owe it a decision.
    if on_probation and age >= el["decision_age"]:
        row, action = _early_life_forced_decision(
            ad_id=ad_id,
            info=info,
            window_metrics=window_metrics,
            histories=histories,
            as_of=as_of,
            account_slug=account_slug,
            policy=policy,
            roas_floor=roas_floor,
            roas_target=roas_target,
            min_spend=min_spend,
            grace_days=grace_days,
            accelerating=accelerating,
            open_followup=open_followup,
            el=el,
            age=age,
            win_from=win_from,
            to=to,
        )
        return row, action, True

    # Brand-new window (age ≤ max_age): the triage supersedes the blanket grace/insufficient abstain.
    if age <= el["max_age"]:
        v = triage_ad(
            ad_id=ad_id,
            account_slug=account_slug,
            as_of=as_of,
            histories=histories,
            policy=policy,
            roas_floor=roas_floor,
            roas_target=roas_target,
            early_life_max_age=el["max_age"],
            decision_age=el["decision_age"],
            recovery_horizon=el["recovery_horizon"],
            min_analogs=el["min_analogs"],
            strong_analogs=el["strong_analogs"],
            recovery_rate=el["recovery_rate"],
        )
        if v is None or v.verdict == VERDICT_NOT_STRUGGLING:
            return None, None, False  # healthy / un-gradable young ad → behave exactly as today
        if v.verdict == VERDICT_PAUSE_CANDIDATE:
            row = _early_life_row(
                ad_id=ad_id,
                info=info,
                window_metrics=window_metrics,
                classification=EARLY_PAUSE_CANDIDATE,
                verdict=v.verdict,
                age=age,
                reasons=v.reasons,
                confidence=v.confidence,
                evidence=v.evidence,
                analog_basis=v.analog_basis,
                dollars_at_risk=_dollars_at_risk(
                    window_metrics.get("spend") or 0.0, window_metrics.get("roas"), roas_target
                ),
            )
            return row, None, True  # flag-only; no follow-up, no account write
        # keep_watch / abstain_keep → keep on probation + file a day-3 follow-up (deduped across runs).
        row = _early_life_row(
            ad_id=ad_id,
            info=info,
            window_metrics=window_metrics,
            classification="watch",
            verdict=v.verdict,
            age=age,
            reasons=v.reasons,
            confidence=v.confidence,
            evidence=v.evidence,
            analog_basis=v.analog_basis,
        )
        action = None
        if not on_probation:
            action = _early_life_file_action(
                ad_id=ad_id,
                info=info,
                history=history,
                verdict=v,
                account_slug=account_slug,
                decision_age=el["decision_age"],
            )
        return row, action, True

    return None, None, False  # older than max_age and not on probation → normal flow


def build_watch_report(
    reader: MetaReaderProvider | MetaMarketingApiClient,
    ad_account_id: str,
    *,
    account_slug: str,
    as_of: date,
    window_days: int = 7,
    recent_days: int = 3,
    min_spend: float = 100.0,
    grace_days: int = 5,
    roas_floor: float | None = None,
    roas_target: float | None = None,
    prior_watchlist: dict[str, Any] | None = None,
    early_life: bool = True,
    history_provider: HistoryProvider | None = None,
    open_followups: list[Followup] | None = None,
    policy: dict[str, Any] | None = None,
    early_life_max_age: int = EARLY_LIFE_MAX_AGE,
    early_life_decision_age: int = EARLY_LIFE_DECISION_AGE,
    early_life_recovery_horizon: int = EARLY_LIFE_RECOVERY_HORIZON,
    early_life_min_analogs: int = EARLY_LIFE_MIN_ANALOGS,
    early_life_strong_analogs: int = EARLY_LIFE_STRONG_ANALOGS,
    early_life_recovery_rate: float = EARLY_LIFE_RECOVERY_RATE,
) -> dict[str, Any]:
    """Read-only runaway/outlier scan. ``reader`` (a :class:`MetaReaderProvider`, or a raw
    ``MetaMarketingApiClient`` which is wrapped) supplies the live insights + ad metadata; this
    module never writes to the account.

    When ``early_life`` and a ``history_provider`` are supplied, genuinely brand-new struggling ads
    (age ≤ ``early_life_max_age``, age from the provider's ``first_seen``) are graded by the early-life
    triage instead of being silently abstained by the grace/significance floor. The scan stays
    filesystem-free for follow-ups too: it RETURNS a ``followup_actions`` list (file/close) for the
    CLI to apply. ``open_followups`` are the account's existing open early-life follow-ups, used to
    detect probation (the day-3 forced decision) and to dedupe filing across runs."""
    reader = as_reader(reader)
    if policy is None:
        policy = _resolve_policy(account_slug)
    if roas_floor is None or roas_target is None:
        pf, pt = _floors_from_policy(policy)
        roas_floor = roas_floor if roas_floor is not None else pf
        roas_target = roas_target if roas_target is not None else pt
    win_from = (as_of - timedelta(days=window_days - 1)).isoformat()
    rec_from = (as_of - timedelta(days=recent_days - 1)).isoformat()
    to = as_of.isoformat()

    window = {str(m["id"]): m for m in fetch_entity_metrics(reader, ad_account_id, level="ad", date_from=win_from, date_to=to)}
    recent = {str(m["id"]): m for m in fetch_entity_metrics(reader, ad_account_id, level="ad", date_from=rec_from, date_to=to)}
    meta = {
        str(a.get("id")): a
        for a in reader.iter_paginated(f"/{ad_account_id}/ads", params={"fields": ",".join(AD_META_FIELDS), "limit": 200})
    }

    # Early-life setup (once per scan): the account's per-ad daily histories (for age + analogs) and a
    # by-ad index of existing OPEN probation follow-ups (for the day-3 decision + cross-run dedupe).
    early_life_enabled = early_life and history_provider is not None
    histories: list[AdHistory] = history_provider.ad_histories(account_slug) if early_life_enabled else []
    hist_by_id = {h.ad_id: h for h in histories}
    open_followup_by_id: dict[str, Followup] = {}
    for f in open_followups or []:
        fad = early_life_ad_id(f)
        if fad:
            open_followup_by_id[fad] = f
    el = {
        "max_age": early_life_max_age,
        "decision_age": early_life_decision_age,
        "recovery_horizon": early_life_recovery_horizon,
        "min_analogs": early_life_min_analogs,
        "strong_analogs": early_life_strong_analogs,
        "recovery_rate": early_life_recovery_rate,
    }

    prior = (prior_watchlist or {}).get("ads", {})
    rows: list[dict[str, Any]] = []
    new_watchlist: dict[str, Any] = {}
    followup_actions: list[dict[str, Any]] = []
    for ad_id, m in window.items():
        info = meta.get(ad_id, {})
        if info.get("effective_status") not in DELIVERING:
            continue  # not currently delivering -> not a runaway
        spend = m.get("spend") or 0.0
        changed = _as_date(info.get("updated_time")) or _as_date(info.get("created_time"))
        days_since_change = (as_of - changed).days if changed else None
        win_daily = spend / window_days
        rec_daily = (recent.get(ad_id, {}).get("spend") or 0.0) / recent_days
        accelerating = win_daily > 0 and rec_daily >= 1.3 * win_daily

        if early_life_enabled:
            el_row, el_action, handled = _early_life_branch(
                ad_id=ad_id, info=info, window_metrics=m, history=hist_by_id.get(ad_id),
                histories=histories, as_of=as_of, account_slug=account_slug, policy=policy,
                roas_floor=roas_floor, roas_target=roas_target, min_spend=min_spend,
                grace_days=grace_days, accelerating=accelerating,
                open_followup=open_followup_by_id.get(ad_id), el=el, win_from=win_from, to=to,
            )
            if handled:
                if el_row is not None:
                    rows.append(el_row)
                if el_action is not None:
                    followup_actions.append(el_action)
                continue  # the triage is the single source of truth for this early-life ad

        verdict = classify_ad(
            spend=spend, roas=m.get("roas"), results=m.get("purchases"),
            days_since_change=days_since_change, accelerating=accelerating,
            min_spend=min_spend, grace_days=grace_days, roas_floor=roas_floor, roas_target=roas_target,
            recency_days=0,  # window ends at as_of, so the data is maximally fresh (deterministic)
        )
        cls = verdict["classification"]
        if cls in ("insufficient", "ok"):
            continue
        flaggable = cls in ("urgent", "underperforming")
        prior_entry = prior.get(ad_id, {})
        times = (prior_entry.get("times_flagged", 0) + 1) if flaggable else prior_entry.get("times_flagged", 0)
        roas_val = m.get("roas")
        evidence = Evidence(
            metric_name="roas", metric_value=roas_val,
            metric_display=f"ROAS {roas_val:.2f}" if roas_val is not None else "ROAS n/a",
            window=f"{win_from}..{to}", sample_conversions=m.get("purchases"), sample_spend=round(spend, 2),
            entity_level="ad", entity_id=ad_id, entity_name=info.get("name"),
            regenerating_query=build_regenerating_query(account_slug, "ad", win_from, to),
        )
        row = {
            "ad_id": ad_id, "ad_name": info.get("name"), "adset_id": info.get("adset_id"),
            "classification": cls, "spend": round(spend, 2), "roas": m.get("roas"),
            "purchases": m.get("purchases"), "dollars_at_risk": verdict["dollars_at_risk"],
            "days_since_change": days_since_change, "accelerating": accelerating,
            "times_flagged": times, "reasons": verdict["reasons"],
            "confidence": verdict["confidence"], "evidence": evidence_to_dict(evidence),
        }
        rows.append(row)
        if flaggable:
            new_watchlist[ad_id] = {
                "ad_name": info.get("name"),
                "first_flagged": prior_entry.get("first_flagged", to),
                "last_flagged": to, "times_flagged": times,
            }

    order = {"urgent": 0, "pause_candidate": 1, "underperforming": 2, "watch": 3}
    rows.sort(key=lambda r: (order.get(r["classification"], 9), -(r.get("dollars_at_risk") or 0.0)))
    return {
        "schema_version": 2,  # v2 adds the early-life rows/fields + followup_actions
        "account_slug": account_slug,
        "ad_account_id": ad_account_id,
        "as_of": to,
        "window": f"{win_from}..{to}",
        "params": {
            "min_spend": min_spend, "grace_days": grace_days,
            "roas_floor": roas_floor, "roas_target": roas_target,
            "early_life": early_life_enabled,
            "early_life_max_age": early_life_max_age,
            "early_life_decision_age": early_life_decision_age,
        },
        "rows": rows,
        "followup_actions": followup_actions,
        "watchlist": {"generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "ads": new_watchlist},
    }


def default_watch_report_path(account_slug: str, run_date: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / run_date / "watch_report.json"


def watchlist_path(account_slug: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    return reports_root / account_slug / "watchlist.json"


def load_watchlist(account_slug: str, reports_root: Path = DEFAULT_REPORTS_ROOT) -> dict[str, Any]:
    path = watchlist_path(account_slug, reports_root)
    if path.exists():
        import json
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_watchlist(account_slug: str, watchlist: dict[str, Any], reports_root: Path = DEFAULT_REPORTS_ROOT) -> Path:
    path = watchlist_path(account_slug, reports_root)
    ensure_dir(path.parent)
    write_json(path, watchlist)
    return path
