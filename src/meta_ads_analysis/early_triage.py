"""Early-life ad triage (pure engine + a DuckDB data seam).

``monitor.classify_ad`` protects brand-new ads by abstaining — a sub-floor ad reads ``insufficient``
and an in-grace ad reads ``watch``. That is correct but *silent*: a genuinely-dead new ad and a
slow-starting eventual winner look identical, both "keep running" with no scrutiny. This module is
the constructive complement: when a brand-new (≈ day 1–3) ad is struggling, grade it against **this
account's own history of comparable new ads at the same age**. If similar past ads that started
equally badly later turned around, keep it; if comparable past ads at that age stayed bad — or there
is no comparable history — flag it as an early pause candidate through the normal guarded flow.

Everything here is **clock-free**: ``as_of`` (the run date) is passed in, never read from the system
clock — matching ``monitor.py``'s discipline. Ages are ``(as_of - first_seen).days`` (day 1 == age 0).

The split (this ticket vs. the monitor-integration sibling): this module owns the ``HistoryProvider``
seam, the pure matching/recovery/verdict engine (:func:`triage_ad`), and the one concrete provider
(:class:`DuckDBHistoryProvider`). It never wires CLI, never touches the monitor, never writes a
follow-up — that is the integration ticket, which fetches histories via the provider and calls
:func:`triage_ad` (which stays pure by taking the already-fetched ``histories``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from .confidence import (
    Evidence,
    EvidenceTier,
    abstain_confidence,
    analog_confidence,
    build_regenerating_query,
    confidence_to_dict,
    evidence_to_dict,
)
from .config import (
    ANALOG_RATIO_TOLERANCE,
    EARLY_LIFE_DECISION_AGE,
    EARLY_LIFE_MAX_AGE,
    EARLY_LIFE_MIN_ANALOGS,
    EARLY_LIFE_MIN_SPEND,
    EARLY_LIFE_RECOVERY_HORIZON,
    EARLY_LIFE_RECOVERY_RATE,
    EARLY_LIFE_STRONG_ANALOGS,
)

# Account goal sentinels — kept identical to actions._select_action_metric / _should_pause_ad.
INSTALL_GOAL = "maximize_in_app_subscriptions"
ROAS_GOAL = "roas"

# Verdicts the engine can return (the integration ticket routes each through the guarded flow).
VERDICT_NOT_STRUGGLING = "not_struggling"
VERDICT_ABSTAIN_KEEP = "abstain_keep"
VERDICT_KEEP_WATCH = "keep_watch"
VERDICT_PAUSE_CANDIDATE = "pause_candidate"

# Own-sample (life-to-date observed window) verdicts — distinct from the analog VERDICT_* values above.
# Used by the monitor's day-3 forced decision so the keep/kill is graded on the ACCOUNT GOAL metric,
# not ROAS. classify_own_sample is the goal-aware analog of monitor.classify_ad.
OWN_SAMPLE_INSUFFICIENT = "insufficient"  # below the significance floor — caller defers to analogs
OWN_SAMPLE_KEEP = "keep"  # cleared the floor and is at/above the goal bar
OWN_SAMPLE_PAUSE = "pause_candidate"  # cleared the floor and is below/over the goal bar

# "≥1 result" threshold. Purchases / installs are integer counts, so a small epsilon below 1 cleanly
# separates "has a conversion" from the (common day-1) zero-result case without float-noise surprises.
_MIN_RESULT = 1.0


# --------------------------------------------------------------------------------------------------
# Data seam — per-ad daily time series, behind a provider protocol so the engine never touches SQL.
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class AdDailyPoint:
    """One active day of an ad's life. Fields mirror the normalized ``ad_daily_metrics`` columns the
    engine needs; ``results`` is the raw goal primary-result count carried for completeness, while the
    engine derives its goal-aware result count from ``purchase_count`` (ROAS) / ``app_installs``
    (install) so the metric matches ``actions._select_action_metric``."""

    report_date: date
    spend: float
    results: float
    purchase_count: float
    purchase_value: float
    app_installs: float


@dataclass(slots=True)
class AdHistory:
    """One ad's full daily series for an account, sorted ascending by ``report_date`` (one point per
    active day). Always non-empty — the provider drops ads with no usable rows."""

    ad_id: str
    ad_name: str | None
    points: list[AdDailyPoint]

    @property
    def first_seen(self) -> date:
        return min(point.report_date for point in self.points)

    @property
    def last_seen(self) -> date:
        return max(point.report_date for point in self.points)

    def age_on(self, as_of: date) -> int:
        """Age in days at ``as_of`` (day 1 of life == age 0). May be negative if ``as_of`` predates
        ``first_seen`` (clock/data skew); :func:`triage_ad` clamps that to 0."""
        return (as_of - self.first_seen).days

    @property
    def last_age(self) -> int:
        """Age on the ad's most recent active day — i.e. the oldest age we can slice it at."""
        return (self.last_seen - self.first_seen).days


class HistoryProvider(Protocol):
    """Yields per-ad daily series for an account. The pure engine takes the resulting
    ``list[AdHistory]`` directly, so analog matching is testable with a hand-built fake — no live
    Meta, no DuckDB."""

    def ad_histories(self, account_slug: str) -> list[AdHistory]: ...


# --------------------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class EarlyTriageVerdict:
    verdict: str
    age: int
    reasons: list[str]
    analog_basis: dict[str, Any]
    confidence: dict[str, Any]
    evidence: dict[str, Any]


@dataclass(slots=True)
class OwnSampleVerdict:
    """Goal-aware grade of an ad's OWN observed window (life-to-date), NOT analogs. Returned by
    :func:`classify_own_sample` so the monitor's day-3 forced decision grades the keep/kill on the
    account-goal metric (cost-per-install on an install account) instead of ROAS."""

    verdict: str  # OWN_SAMPLE_*
    kind: str  # "roas" | "install"
    metric_name: str  # "blended_roas" | "cost_per_app_install"
    metric_value: float | None
    target: float | None  # the goal floor/target used (None when the install target is unknown)
    results: float  # goal-aware result count (purchases for ROAS, installs for install)
    reasons: list[str]


# --------------------------------------------------------------------------------------------------
# Goal-aware metric selection — consistent with actions._select_action_metric / monitor._policy_floors
# (the same goal→metric mapping and the same floors/targets; no threshold numbers are duplicated).
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _GoalProfile:
    kind: str  # "roas" | "install"
    struggling_threshold: float  # ROAS pause floor, or install cost target
    recovery_threshold: float  # ROAS target, or install cost target
    metric_name: str  # "blended_roas" | "cost_per_app_install"


@dataclass(slots=True)
class _Sums:
    spend: float
    results: float  # goal-aware result count (purchases for ROAS, installs for install)
    purchase_value: float


def _goal_kind(policy: dict[str, Any]) -> str:
    return "install" if policy.get("primary_goal") == INSTALL_GOAL else "roas"


def goal_kind(policy: dict[str, Any]) -> str:
    """Public wrapper for the goal→metric-kind mapping (``"roas"`` | ``"install"``), so callers (the
    monitor's forced decision) can branch on the account goal without reaching into a private helper."""
    return _goal_kind(policy)


def _goal_thresholds(
    kind: str,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
) -> tuple[float, float] | None:
    """(struggling_threshold, recovery_threshold) for the goal, or ``None`` if an install-goal account
    exposes no target install cost (the caller then degrades to ``abstain_keep`` rather than crash)."""
    if kind == "install":
        target = _number(policy.get("secondary_cost_per_app_install_target"))
        if target is None:
            target = _number(policy.get("pause_if_no_primary_and_secondary_cost_above"))
        if target is None:
            return None
        return target, target
    return roas_floor, roas_target


def _metric_name(kind: str) -> str:
    return "cost_per_app_install" if kind == "install" else "blended_roas"


def _select_results(point: AdDailyPoint, kind: str) -> float:
    return point.app_installs if kind == "install" else point.purchase_count


def _sum_window(points: list[AdDailyPoint], kind: str) -> _Sums:
    spend = results = purchase_value = 0.0
    for point in points:
        spend += point.spend or 0.0
        results += _select_results(point, kind) or 0.0
        purchase_value += point.purchase_value or 0.0
    return _Sums(spend=spend, results=results, purchase_value=purchase_value)


def _metric_value(sums: _Sums, kind: str) -> float | None:
    """ROAS (purchase_value / spend) or cost-per-install (spend / installs); ``None`` when undefined
    (e.g. the zero-install day-1 case) — callers must NOT divide by zero."""
    if kind == "install":
        return (sums.spend / sums.results) if sums.results > 0 else None
    return (sums.purchase_value / sums.spend) if sums.spend > 0 else None


def _has_result(sums: _Sums) -> bool:
    return sums.results >= _MIN_RESULT


def _is_struggling(sums: _Sums, profile: _GoalProfile, non_trivial_spend: float) -> bool:
    """Goal-aware "struggling" test on a cumulative window. Below the non-trivial spend floor we
    decline to call it struggling at all (a $0.50 ad is not force-graded). Above it: zero results on
    that spend is struggling; otherwise compare the goal metric to its floor/target."""
    if sums.spend < non_trivial_spend:
        return False
    if not _has_result(sums):
        return True  # zero results on non-trivial spend
    metric = _metric_value(sums, profile.kind)
    if metric is None:
        return True
    if profile.kind == "install":
        return metric > profile.struggling_threshold  # cost worse (higher) than target
    return metric < profile.struggling_threshold  # ROAS below the pause floor


def _cleared_target(sums: _Sums, profile: _GoalProfile) -> bool:
    """Did this window clear the account TARGET (the recovery bar)?"""
    metric = _metric_value(sums, profile.kind)
    if metric is None:
        return False
    if profile.kind == "install":
        return metric <= profile.recovery_threshold
    return metric >= profile.recovery_threshold


def _ratio_within(value_a: float, value_b: float, tolerance: float) -> bool:
    """True when ``value_a`` is within a multiplicative ``tolerance`` band of ``value_b`` — i.e.
    ``tolerance <= a/b <= 1/tolerance`` (0.5 → 0.5×–2.0×). Both values must be positive."""
    if value_a <= 0 or value_b <= 0:
        return False
    ratio = value_a / value_b
    return tolerance <= ratio <= (1.0 / tolerance)


# --------------------------------------------------------------------------------------------------
# Age slicing
# --------------------------------------------------------------------------------------------------


def _age_of(point: AdDailyPoint, first_seen: date) -> int:
    return (point.report_date - first_seen).days


def _points_through_age(history: AdHistory, age: int) -> list[AdDailyPoint]:
    first_seen = history.first_seen
    return [point for point in history.points if _age_of(point, first_seen) <= age]


def _points_in_age_range(history: AdHistory, low: int, high: int) -> list[AdDailyPoint]:
    first_seen = history.first_seen
    return [point for point in history.points if low <= _age_of(point, first_seen) <= high]


# --------------------------------------------------------------------------------------------------
# Analog matching + recovery
# --------------------------------------------------------------------------------------------------


def _is_analog(
    triaged_sums: _Sums,
    candidate: AdHistory,
    age: int,
    profile: _GoalProfile,
    tolerance: float,
    non_trivial_spend: float,
) -> bool:
    """Is ``candidate`` a comparable analog of the triaged ad at ``age``? It must (a) have reached at
    least ``age`` so we can slice it there, (b) have *also* been struggling through ``age`` (same
    goal-aware test — we compare against ads that started badly, not all ads), and (c) be
    magnitude-comparable within ``tolerance``. Magnitude uses the cost-per-result ratio when both
    sides have ≥1 result, else the zero-result fallback (the common day-1 reality): the candidate
    must *also* have ~0 results and a cumulative spend within tolerance."""
    if candidate.last_age < age:
        return False
    candidate_sums = _sum_window(_points_through_age(candidate, age), profile.kind)
    if not _is_struggling(candidate_sums, profile, non_trivial_spend):
        return False

    triaged_has = _has_result(triaged_sums)
    candidate_has = _has_result(candidate_sums)
    if triaged_has and candidate_has:
        return _ratio_within(
            triaged_sums.spend / triaged_sums.results,
            candidate_sums.spend / candidate_sums.results,
            tolerance,
        )
    if not triaged_has:
        # Zero-result fallback: ratio is undefined, so match on zero-result + spend magnitude.
        if candidate_has:
            return False
        return _ratio_within(triaged_sums.spend, candidate_sums.spend, tolerance)
    # Triaged ad has results but the candidate has none → not comparable.
    return False


# --------------------------------------------------------------------------------------------------
# Public engine
# --------------------------------------------------------------------------------------------------


def triage_ad(
    *,
    ad_id: str,
    account_slug: str,
    as_of: date,
    histories: list[AdHistory],
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    early_life_max_age: int = EARLY_LIFE_MAX_AGE,
    decision_age: int = EARLY_LIFE_DECISION_AGE,
    recovery_horizon: int = EARLY_LIFE_RECOVERY_HORIZON,
    min_analogs: int = EARLY_LIFE_MIN_ANALOGS,
    strong_analogs: int = EARLY_LIFE_STRONG_ANALOGS,
    recovery_rate: float = EARLY_LIFE_RECOVERY_RATE,
    ratio_tolerance: float = ANALOG_RATIO_TOLERANCE,
    non_trivial_spend: float = EARLY_LIFE_MIN_SPEND,
) -> EarlyTriageVerdict | None:
    """Grade one brand-new struggling ad against the account's own history of comparable new ads.

    Returns ``None`` when the ad is not found in ``histories`` or is no longer early-life
    (``age > early_life_max_age``) — the caller then leaves it to the normal monitor/action flow.
    Otherwise returns an :class:`EarlyTriageVerdict`. Pure & deterministic: identical inputs →
    identical verdict (matched ids are sorted; no clock, no randomness)."""
    target = next((history for history in histories if history.ad_id == ad_id), None)
    if target is None:
        return None

    # Age is purely (as_of - first_seen).days; clamp clock/data skew (as_of before first_seen) to 0.
    age = max(0, target.age_on(as_of))
    if age > early_life_max_age:
        return None

    first_seen = target.first_seen
    window = f"{first_seen.isoformat()}..{as_of.isoformat()}"
    recheck_day = decision_age + 1  # "day 3" with the default decision age of 2
    kind = _goal_kind(policy)
    triaged_sums = _sum_window(_points_through_age(target, age), kind)
    metric_value = _metric_value(triaged_sums, kind)

    evidence = Evidence(
        metric_name=_metric_name(kind),
        metric_value=metric_value,
        metric_display=_fmt_metric(metric_value, kind),
        window=window,
        sample_conversions=triaged_sums.results,
        sample_spend=round(triaged_sums.spend, 2),
        entity_level="ad",
        entity_id=ad_id,
        entity_name=target.ad_name,
        regenerating_query=build_regenerating_query(
            account_slug, "ad", first_seen.isoformat(), as_of.isoformat()
        ),
    )
    evidence_dict = evidence_to_dict(evidence)

    thresholds = _goal_thresholds(kind, policy, roas_floor, roas_target)
    if thresholds is None:
        # Install-goal account with no target install cost in policy: degrade gracefully — keep
        # running rather than crash or guess a threshold.
        reasons = [
            "account goal 'maximize_in_app_subscriptions' has no target install cost in policy — "
            f"cannot grade early life; keep running and re-check by day {recheck_day}"
        ]
        return EarlyTriageVerdict(
            verdict=VERDICT_ABSTAIN_KEEP,
            age=age,
            reasons=reasons,
            analog_basis=_basis(0, 0, [], age, recovery_horizon, min_analogs),
            confidence=confidence_to_dict(_abstain(reasons)),
            evidence=evidence_dict,
        )

    profile = _GoalProfile(
        kind=kind,
        struggling_threshold=thresholds[0],
        recovery_threshold=thresholds[1],
        metric_name=_metric_name(kind),
    )

    # Not struggling → short-circuit before any analog work; leave it to the normal flow.
    if not _is_struggling(triaged_sums, profile, non_trivial_spend):
        reasons = [
            f"early-life ad (age {age}) is not struggling on its goal metric "
            f"({evidence.metric_display}) — leave to the normal flow"
        ]
        return EarlyTriageVerdict(
            verdict=VERDICT_NOT_STRUGGLING,
            age=age,
            reasons=reasons,
            analog_basis=_basis(0, 0, [], age, recovery_horizon, min_analogs),
            confidence=confidence_to_dict(_abstain(reasons)),
            evidence=evidence_dict,
        )

    # Analog matching + survivorship-aware recovery over the whole matched population.
    matched_ids: list[str] = []
    recovered_ids: list[str] = []
    for candidate in histories:
        if candidate.ad_id == ad_id:
            continue
        if not _is_analog(triaged_sums, candidate, age, profile, ratio_tolerance, non_trivial_spend):
            continue
        if candidate.last_age < recovery_horizon:
            # Matched but too short-lived to judge recovery → excluded from the population entirely
            # (neither a recovery nor a stayed-bad — a paused-early ad must not read as "stayed bad").
            continue
        matched_ids.append(candidate.ad_id)
        recovery_sums = _sum_window(
            _points_in_age_range(candidate, age + 1, recovery_horizon), kind
        )
        if _cleared_target(recovery_sums, profile):
            recovered_ids.append(candidate.ad_id)

    matched_ids.sort()
    analogs = len(matched_ids)
    recovered = len(recovered_ids)
    rate = (recovered / analogs) if analogs else 0.0
    basis = _basis(analogs, recovered, matched_ids, age, recovery_horizon, min_analogs)

    if analogs < min_analogs:
        reasons = [
            f"only {analogs} comparable analog(s) at age {age} (need {min_analogs}) — not enough "
            f"account history for a confident early call; keep running and re-check by day {recheck_day}"
        ]
        return EarlyTriageVerdict(
            verdict=VERDICT_ABSTAIN_KEEP,
            age=age,
            reasons=reasons,
            analog_basis=basis,
            confidence=confidence_to_dict(_abstain(reasons)),
            evidence=evidence_dict,
        )

    factors = [
        f"{analogs} comparable analog(s) started this badly at age {age} (cross-sectional)",
        f"{recovered} recovered by day {recovery_horizon + 1} (rate {rate:.0%})",
    ]
    confidence = confidence_to_dict(
        analog_confidence(
            analogs=analogs,
            recovered=recovered,
            min_analogs=min_analogs,
            strong_analogs=strong_analogs,
            factors=factors,
        )
    )

    if rate >= recovery_rate:
        reasons = [
            f"{analogs} comparable ads started this badly at age {age}; {recovered} recovered "
            f"(rate {rate:.0%} ≥ {recovery_rate:.0%} keep threshold) — keep running, re-check by "
            f"day {recheck_day}"
        ]
        verdict = VERDICT_KEEP_WATCH
    else:
        reasons = [
            f"{analogs} comparable ads started this badly at age {age}; only {recovered} recovered "
            f"(rate {rate:.0%} < {recovery_rate:.0%} keep threshold) — early pause candidate"
        ]
        verdict = VERDICT_PAUSE_CANDIDATE

    return EarlyTriageVerdict(
        verdict=verdict,
        age=age,
        reasons=reasons,
        analog_basis=basis,
        confidence=confidence,
        evidence=evidence_dict,
    )


def classify_own_sample(
    *,
    spend: float,
    purchase_value: float | None,
    purchases: float | None,
    app_installs: float | None,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    min_spend: float,
) -> OwnSampleVerdict:
    """Goal-aware grade of an ad's OWN observed window (NOT analogs).

    Below ``min_spend`` → :data:`OWN_SAMPLE_INSUFFICIENT` (the caller falls through to the analog
    verdict). Above it, build a :class:`_Sums` from the window and reuse
    :func:`_goal_kind`/:func:`_goal_thresholds`/:class:`_GoalProfile`/:func:`_is_struggling` so the
    bar is identical to the analog engine: ROAS below the pause floor / install cost over target (or
    zero results on the spend) → :data:`OWN_SAMPLE_PAUSE`; otherwise :data:`OWN_SAMPLE_KEEP`.

    ``min_spend`` is also threaded into :func:`_is_struggling` as its ``non_trivial_spend`` floor, so
    the ``spend >= min_spend`` gate is the single significance threshold (no second, conflicting
    floor). When ``kind == "install"`` and the policy carries no target install cost, returns
    :data:`OWN_SAMPLE_INSUFFICIENT` so the caller defers to the analog path (which degrades to keep)."""
    kind = _goal_kind(policy)
    spend = spend or 0.0
    results = (app_installs if kind == "install" else purchases) or 0.0
    sums = _Sums(spend=spend, results=results, purchase_value=purchase_value or 0.0)
    metric_name = _metric_name(kind)
    metric_value = _metric_value(sums, kind)

    if spend < min_spend:
        return OwnSampleVerdict(
            verdict=OWN_SAMPLE_INSUFFICIENT,
            kind=kind,
            metric_name=metric_name,
            metric_value=metric_value,
            target=None,
            results=results,
            reasons=[
                f"own sample ${spend:.0f} < ${min_spend:.0f} significance floor — defer to analogs"
            ],
        )

    thresholds = _goal_thresholds(kind, policy, roas_floor, roas_target)
    if thresholds is None:
        # Install-goal account with no target install cost in policy: defer to the analog path rather
        # than guess a threshold (the analog engine degrades such accounts to keep).
        return OwnSampleVerdict(
            verdict=OWN_SAMPLE_INSUFFICIENT,
            kind=kind,
            metric_name=metric_name,
            metric_value=metric_value,
            target=None,
            results=results,
            reasons=[
                "account goal 'maximize_in_app_subscriptions' has no target install cost in policy — "
                "defer to analogs"
            ],
        )

    profile = _GoalProfile(
        kind=kind,
        struggling_threshold=thresholds[0],
        recovery_threshold=thresholds[1],
        metric_name=metric_name,
    )
    target = thresholds[0]
    display = _fmt_metric(metric_value, kind)

    if _is_struggling(sums, profile, non_trivial_spend=min_spend):
        if not _has_result(sums):
            reason = f"~0 {'installs' if kind == 'install' else 'purchases'} on ${spend:.0f}"
        elif kind == "install":
            reason = f"{display} over the ${target:.2f} target on ${spend:.0f}"
        else:
            reason = f"{display} below the {target:.2f} pause floor on ${spend:.0f}"
        verdict = OWN_SAMPLE_PAUSE
    else:
        if kind == "install":
            reason = f"{display} at/under the ${target:.2f} target on ${spend:.0f}"
        else:
            reason = f"{display} at/above the {target:.2f} pause floor on ${spend:.0f}"
        verdict = OWN_SAMPLE_KEEP

    return OwnSampleVerdict(
        verdict=verdict,
        kind=kind,
        metric_name=metric_name,
        metric_value=metric_value,
        target=target,
        results=results,
        reasons=[reason],
    )


def _basis(
    analogs: int,
    recovered: int,
    matched_ids: list[str],
    age: int,
    horizon: int,
    min_analogs: int,
) -> dict[str, Any]:
    return {
        "analogs": analogs,
        "recovered": recovered,
        "rate": round((recovered / analogs) if analogs else 0.0, 4),
        "horizon": horizon,
        "matched_ids": list(matched_ids),
        "age": age,
        "min_analogs": min_analogs,
    }


def _abstain(factors: list[str]):
    """Early-triage abstention, expressed through the shared :func:`abstain_confidence` factory so
    every :class:`~confidence.Confidence` is still built in one place. Grounding is ``correlational``
    (the call is cross-sectional even when we decline to score it); the data axis abstains."""
    return abstain_confidence(
        tier=EvidenceTier.correlational,
        factors=factors,
        would_raise="more comparable account history at this age",
        causal_claim=False,
    )


def _fmt_metric(value: float | None, kind: str) -> str:
    if kind == "install":
        return f"cost/install ${value:.2f}" if value is not None else "cost/install n/a"
    return f"ROAS {value:.2f}" if value is not None else "ROAS n/a"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------------
# Concrete provider — the only place that touches DuckDB/SQL.
# --------------------------------------------------------------------------------------------------


class DuckDBHistoryProvider:
    """Reads the **latest** ingestion run's ``ad_daily_metrics`` rows for an account and groups them
    into per-ad :class:`AdHistory` objects. A single synced snapshot carries daily ``report_date``
    rows spanning the whole account history, so one latest run is enough to reconstruct every past
    ad's metrics at any age. All SQL lives here; the engine only ever sees ``list[AdHistory]``."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def ad_histories(self, account_slug: str) -> list[AdHistory]:
        from . import storage

        with storage.connect(self._db_path) as con:
            storage.initialize_database(con)  # idempotent; a brand-new DB then yields []
            run_date = _latest_ingestion_run_date(con, account_slug)
            if run_date is None:
                return []
            rows = storage.fetch_run_rows(con, run_date, account_slug)
        return group_histories(rows)


def _latest_ingestion_run_date(con: Any, account_slug: str) -> str | None:
    row = con.execute(
        "SELECT max(ingestion_run_date) FROM ad_daily_metrics WHERE account_slug = ?",
        [account_slug],
    ).fetchone()
    value = row[0] if row else None
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def group_histories(rows: list[dict[str, Any]]) -> list[AdHistory]:
    """Group flat ``ad_daily_metrics`` rows into per-``ad_id`` :class:`AdHistory` objects, each sorted
    ascending by ``report_date``. Rows with no ``ad_id`` or unparseable ``report_date`` are dropped;
    the first non-empty ``ad_name`` seen for an id is used. Output is sorted by ``ad_id`` for
    deterministic downstream verdicts."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str | None] = {}
    for row in rows:
        ad_id = row.get("ad_id")
        if ad_id is None or str(ad_id) == "":
            continue
        key = str(ad_id)
        grouped[key].append(row)
        if not names.get(key):
            names[key] = row.get("ad_name")

    histories: list[AdHistory] = []
    for ad_id, ad_rows in grouped.items():
        points: list[AdDailyPoint] = []
        for row in ad_rows:
            report_date = _as_date(row.get("report_date"))
            if report_date is None:
                continue
            points.append(
                AdDailyPoint(
                    report_date=report_date,
                    spend=_float(row.get("spend")),
                    results=_float(row.get("results")),
                    purchase_count=_float(row.get("purchase_count")),
                    purchase_value=_float(row.get("purchase_value")),
                    app_installs=_float(row.get("app_installs")),
                )
            )
        if not points:
            continue
        points.sort(key=lambda point: point.report_date)
        histories.append(AdHistory(ad_id=ad_id, ad_name=names.get(ad_id), points=points))

    histories.sort(key=lambda history: history.ad_id)
    return histories


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
