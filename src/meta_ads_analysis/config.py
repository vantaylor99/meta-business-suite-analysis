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
