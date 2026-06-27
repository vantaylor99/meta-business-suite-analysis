"""Project-wide constants and defaults."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "data" / "raw" / "meta_ads"
DEFAULT_NORMALIZED_ROOT = PROJECT_ROOT / "data" / "normalized" / "meta_ads"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "normalized" / "meta_ads.duckdb"
DEFAULT_REPORTS_ROOT = PROJECT_ROOT / "reports"
DEFAULT_ACCOUNTS_CONFIG_PATH = PROJECT_ROOT / "config" / "meta_ads_accounts.json"
DEFAULT_META_API_VERSION = "v22.0"
DEFAULT_META_API_TIMEOUT_SECONDS = 30
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_GRAPH_API_ROOT = "https://graph.facebook.com"

MIN_WASTE_SPEND = 100.0
MIN_SCALING_SPEND = 75.0
FATIGUE_WINDOW_DAYS = 7
MIN_FATIGUE_HISTORY_DAYS = FATIGUE_WINDOW_DAYS * 2
TOP_FINDINGS_LIMIT = 5
PERFORMANCE_WINDOWS_DAYS = (30, 7, 3)
MIN_TRAJECTORY_BASE_SPEND = 75.0
MIN_TRAJECTORY_RECENT_SPEND = 50.0
MIN_TRAJECTORY_SHORT_SPEND = 100.0

# Confidence engine (see confidence.py). The data-strength floors are NOT new constants:
# callers pass the existing gates — spend uses MIN_WASTE_SPEND (100.0) / MIN_SCALING_SPEND (75.0)
# / monitor's min_spend (100.0); conversions uses experiment.py's min_conversions default (25).
# CONFIDENCE_RECENCY_STALE_DAYS is the recency knee: a window whose end is older than this many
# days is "stale" and rounds the data band down one level.
CONFIDENCE_RECENCY_STALE_DAYS = 14
# Conversions significance floor for the confidence engine — mirrors experiment.py's
# ``min_conversions`` default (25). A sample below BOTH this and the relevant spend floor abstains
# ("insufficient data") instead of being scored as a confident pause/scale.
CONFIDENCE_CONVERSIONS_FLOOR = 25

# Knowledge-vault staleness (see knowledge_provenance.py / the `lint-vault` checker). A `fast`
# learning whose **Verified:** date is older than this many days before `today` is flagged
# "⏳ re-verify" (a warning, not a fatal error). Deliberately LONGER than
# CONFIDENCE_RECENCY_STALE_DAYS (14) — that governs *live-recommendation* recency, this governs how
# long a written-down account fact may sit un-reconfirmed before the vault nags. ≈6 weeks.
# `evergreen` learnings (platform/API mechanics, durable principles) are NEVER age-flagged.
KNOWLEDGE_REVERIFY_DAYS = 42

# Knowledge-vault drift (see the `audit-vault` re-check). When `audit-vault` re-pulls a stored
# `metric:` over a fresh trailing window, a relative change of at least this fraction
# (|fresh − stored| / stored) counts as drift and contradicts the stored belief. 0.25 (25%)
# deliberately absorbs second-decimal ROAS noise (see "ROAS is partly derived" in learnings.md) so a
# quiet week can't refute a real fact; a *policy-threshold crossing* (target_roas / pause_roas_floor)
# refutes regardless of magnitude because it flips a scale/pause decision. A contradiction never
# deletes a fact — it lowers the confidence band one level and logs a dated `➖`; a human decides
# deletion.
KNOWLEDGE_DRIFT_PCT = 0.25

# Adversarial-review gate (see review.py). The minimum representative window span for the
# ``window_length`` refutation check: a recommendation resting on a window shorter than this many
# days is downgraded ("window may be unrepresentative; recommend a wider window"). This is the ONLY
# new number the gate introduces — the floor and recency re-checks deliberately reuse the producer's
# existing constants (MIN_WASTE_SPEND / MIN_SCALING_SPEND, CONFIDENCE_CONVERSIONS_FLOOR,
# CONFIDENCE_RECENCY_STALE_DAYS) so the gate and the producer share one set of thresholds.
REVIEW_MIN_WINDOW_DAYS = 7

# Budget-decrease safety (wired by ``control`` set_daily_budget — see ``control._capped_budget_request``).
# NOTE: the budget *increase* cap is op-param-driven (``control._build_budget_request`` reads
# ``params["max_increase_percent"]``, default 20) and is deliberately left untouched — these two
# numbers govern the DECREASE direction only, the separate symmetric guard:
#   - MAX_BUDGET_DECREASE_PERCENT: a single set_daily_budget may not cut the live daily budget by more
#     than this percent. An op-param ``max_decrease_percent`` overrides it; a per-account
#     ``max_budget_decrease_percent`` in ``action_policy`` is folded into that op-param by the budget
#     builder. Picked by SIGN of (new - current), so it can never block a valid increase.
#   - MIN_DAILY_BUDGET_CENTS: an absolute floor (account minor units) a decrease may not cross, so a
#     reduction can never silently pause delivery. Deliberately conservative — ``validate_only`` against
#     Meta surfaces the real per-currency minimum as the final check; this is the local sanity floor.
# (The ``write-config-registry-controls`` ticket also documents/tunes these and adds the per-account
# registry field; if it lands after this ticket it should reconcile to these definitions, not duplicate.)
MAX_BUDGET_DECREASE_PERCENT = 50.0
MIN_DAILY_BUDGET_CENTS = 100

# Early-life ad triage (see early_triage.py). monitor.classify_ad correctly PROTECTS brand-new ads
# (under the significance floor → `insufficient`; inside the grace window → `watch`) but does so
# *silently* — a dead new ad and a slow-starting eventual winner look identical. Early triage is the
# constructive complement: when a brand-new struggling ad is too young for the normal flow, grade it
# against THIS account's own history of comparable new ads at the same age. All ages are computed as
# `(as_of - first_seen).days` (day 1 of life == age 0), never from the system clock.
#
# - EARLY_LIFE_MAX_AGE: triage only APPLIES to ads this young (age ≤ this → days 1–3). Older ads stay
#   with the existing monitor/action logic.
# - EARLY_LIFE_DECISION_AGE: by this age (day 3) the engine must give a keep/kill, not an indefinite
#   abstain. Consumed by the monitor-integration ticket to force a day-3 re-check; the engine just
#   exposes the verdict.
# - EARLY_LIFE_RECOVERY_HORIZON: an analog must have lived at least this long (by ~day 8) before we
#   can judge whether it "turned around". A matched analog younger than this is excluded from the
#   population entirely (neither a recovery nor a stayed-bad — it was simply too short-lived to know).
# - EARLY_LIFE_MIN_ANALOGS: fewer qualifying analogs than this → abstain & keep (defer to the day-3
#   re-check). This is the "not enough comparable history" fallback — NEVER a confident early kill.
# - EARLY_LIFE_STRONG_ANALOGS: the analog count at/above which the correlational data band reads
#   `medium` (below it, `low`). The narrative in the source ticket loosely called a 5-analog call
#   "medium"; the authoritative knee is this constant, so 5 analogs read `low` and 6+ read `medium`.
# - EARLY_LIFE_RECOVERY_RATE: survivorship guard. Keep only if the recovery RATE over the whole
#   matched population (R/A) clears this — one lucky turnaround in twenty deaths (0.05) is well below.
# - ANALOG_RATIO_TOLERANCE: multiplicative magnitude band for "comparable". 0.5 → 0.5×–2.0× on the
#   cost-per-result ratio (or, for the common zero-result day-1 case, on cumulative spend).
# - EARLY_LIFE_MIN_SPEND: the "non-trivial spend" floor (≈10% of MIN_WASTE_SPEND). An early-life ad
#   below this has spent too little to be called struggling at all, so a $0.50 day-1 ad is never
#   force-graded; analogs must likewise clear it to count as "also struggling".
EARLY_LIFE_MAX_AGE = 2
EARLY_LIFE_DECISION_AGE = 2
EARLY_LIFE_RECOVERY_HORIZON = 7
EARLY_LIFE_MIN_ANALOGS = 3
EARLY_LIFE_STRONG_ANALOGS = 6
EARLY_LIFE_RECOVERY_RATE = 0.33
ANALOG_RATIO_TOLERANCE = 0.5
EARLY_LIFE_MIN_SPEND = 10.0
