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

# Adversarial-review gate (see review.py). The minimum representative window span for the
# ``window_length`` refutation check: a recommendation resting on a window shorter than this many
# days is downgraded ("window may be unrepresentative; recommend a wider window"). This is the ONLY
# new number the gate introduces — the floor and recency re-checks deliberately reuse the producer's
# existing constants (MIN_WASTE_SPEND / MIN_SCALING_SPEND, CONFIDENCE_CONVERSIONS_FLOOR,
# CONFIDENCE_RECENCY_STALE_DAYS) so the gate and the producer share one set of thresholds.
REVIEW_MIN_WINDOW_DAYS = 7
