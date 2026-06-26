"""Meta API sync orchestration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .account_registry import MetaAdsAccount, resolve_account
from .config import (
    DEFAULT_ACCOUNTS_CONFIG_PATH,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_META_API_VERSION,
    DEFAULT_NORMALIZED_ROOT,
    DEFAULT_RAW_ROOT,
    DEFAULT_REPORTS_ROOT,
)
from .meta_api import MetaMarketingApiClient
from .reader_provider import MetaReaderProvider, as_reader
from .utils import ensure_dir, parse_date, safe_divide, write_csv_rows, write_json

PERFORMANCE_HEADERS = [
    "Account ID",
    "Account name",
    "Campaign ID",
    "Campaign name",
    "Ad set ID",
    "Ad set name",
    "Ad ID",
    "Ad name",
    "Day",
    "Reporting starts",
    "Reporting ends",
    "Reach",
    "Impressions",
    "Frequency",
    "Clicks (all)",
    "Link clicks",
    "Outbound clicks",
    "CTR (all)",
    "CPC (cost per link click)",
    "CPC (all)",
    "CPM (cost per 1,000 impressions)",
    "Amount spent (USD)",
    "Objective",
    "Result type",
    "Results",
    "Cost per result",
    "App installs",
    "Cost per app install",
    "Purchases",
    "Average purchases conversion value",
    "Purchase ROAS (return on ad spend)",
    "Results ROAS",
    "Action values",
    "Actions",
    "Cost per action type",
]

VIDEO_HEADERS = [
    "Ad ID",
    "Ad name",
    "Day",
    "Impressions",
    "3-second video plays",
    "ThruPlays",
]

CREATIVE_HEADERS = [
    "Ad ID",
    "Ad name",
    "Media type",
    "Primary text",
    "Headline",
    "Launch date",
    "Preview link",
    "Post link",
]

PURCHASE_KEYS = [
    "purchase",
    "website_purchase",
    "onsite_conversion.purchase",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
]
APP_INSTALL_KEYS = [
    "mobile_app_install",
    "app_install",
    "omni_app_install",
]
OUTBOUND_CLICK_KEYS = [
    "outbound_click",
]
LINK_CLICK_KEYS = [
    "link_click",
    "inline_link_click",
]
VIDEO_3S_KEYS = [
    "video_view",
    "video_3_sec_watched_actions",
]
THRUPLAY_KEYS = [
    "video_thruplay_watched_actions",
    "thruplay",
]


@dataclass(slots=True)
class ApiSyncArtifacts:
    account: MetaAdsAccount
    run_date: str
    date_from: str
    date_to: str
    raw_dir: Path
    performance_rows: list[dict[str, Any]]
    video_rows: list[dict[str, Any]]
    creative_rows: list[dict[str, Any]]
    warnings: list[str]
    api_version: str


def sync_account_from_api(
    *,
    account_slug: str,
    run_date: str,
    raw_root: Path | None = None,
    accounts_config_path: Path | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    api_version: str | None = None,
    reader: MetaReaderProvider | MetaMarketingApiClient | None = None,
) -> ApiSyncArtifacts:
    """Sync one account's reporting from Meta (read-only).

    ``reader`` accepts either a :class:`MetaReaderProvider` or a raw
    ``MetaMarketingApiClient`` (wrapped in a ``DirectMetaReader``); when omitted, a direct
    client is built from env. Reads flow through the reader seam so a future MCP read backend
    can supply them unchanged.
    """
    effective_raw_root = raw_root or DEFAULT_RAW_ROOT
    effective_accounts_config_path = accounts_config_path or DEFAULT_ACCOUNTS_CONFIG_PATH
    account = resolve_account(account_slug, effective_accounts_config_path)
    resolved_run_date = _require_date(run_date, "run_date")
    resolved_date_from, resolved_date_to = resolve_date_window(
        resolved_run_date,
        date_from=date_from,
        date_to=date_to,
    )
    effective_api_version = api_version or os.environ.get("META_API_VERSION") or DEFAULT_META_API_VERSION
    if reader is None:
        # Construct via the module-local MetaMarketingApiClient symbol so it stays patchable
        # (tests monkeypatch sync_api.MetaMarketingApiClient); then wrap it in the read seam.
        access_token = os.environ.get("META_ACCESS_TOKEN", "").strip()
        reader = MetaMarketingApiClient(access_token=access_token, api_version=effective_api_version)
    effective_reader = as_reader(reader)
    raw_dir = effective_raw_root / account.account_slug / resolved_run_date.isoformat()
    ensure_dir(raw_dir)

    warnings: list[str] = []
    insights_fields = [
        "account_id",
        "account_name",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "date_start",
        "date_stop",
        "reach",
        "impressions",
        "frequency",
        "clicks",
        "inline_link_clicks",
        "outbound_clicks",
        "ctr",
        "inline_link_click_ctr",
        "cpc",
        "cost_per_inline_link_click",
        "cpm",
        "spend",
        "objective",
        "actions",
        "action_values",
        "cost_per_action_type",
        "purchase_roas",
    ]
    insights_rows = effective_reader.fetch_insights(
        account.ad_account_id,
        fields=insights_fields,
        date_from=resolved_date_from,
        date_to=resolved_date_to,
    )
    performance_rows = [
        _build_performance_row(row, account, warnings)
        for row in insights_rows
    ]
    video_rows = [
        _build_video_row(row)
        for row in insights_rows
    ]

    ad_rows = effective_reader.fetch_ads(
        account.ad_account_id,
        fields=[
            "id",
            "name",
            "created_time",
            "creative{id,object_story_spec,asset_feed_spec,effective_object_story_id,object_type,body,title}",
        ],
    )
    creative_rows = [_build_creative_row(row, warnings) for row in ad_rows]

    write_csv_rows(raw_dir / "performance_daily.csv", performance_rows, PERFORMANCE_HEADERS)
    write_csv_rows(raw_dir / "video_daily.csv", video_rows, VIDEO_HEADERS)
    write_csv_rows(raw_dir / "creative_lookup.csv", creative_rows, CREATIVE_HEADERS)

    return ApiSyncArtifacts(
        account=account,
        run_date=resolved_run_date.isoformat(),
        date_from=resolved_date_from,
        date_to=resolved_date_to,
        raw_dir=raw_dir,
        performance_rows=performance_rows,
        video_rows=video_rows,
        creative_rows=creative_rows,
        warnings=sorted(set(warnings)),
        api_version=effective_api_version,
    )


def write_api_sync_summary(
    artifacts: ApiSyncArtifacts,
    *,
    normalized_dir: Path | None = None,
    report_dir: Path | None = None,
    completed_full_pipeline: bool,
) -> Path:
    payload = {
        "account_slug": artifacts.account.account_slug,
        "account_name": artifacts.account.account_name,
        "ad_account_id": artifacts.account.ad_account_id,
        "run_date": artifacts.run_date,
        "date_from": artifacts.date_from,
        "date_to": artifacts.date_to,
        "raw_dir": str(artifacts.raw_dir),
        "normalized_dir": str(normalized_dir) if normalized_dir else None,
        "report_dir": str(report_dir) if report_dir else None,
        "api_version": artifacts.api_version,
        "row_counts": {
            "performance_daily": len(artifacts.performance_rows),
            "video_daily": len(artifacts.video_rows),
            "creative_lookup": len(artifacts.creative_rows),
        },
        "completed_full_pipeline": completed_full_pipeline,
        "warnings": artifacts.warnings,
    }
    summary_path = artifacts.raw_dir / "api_sync_summary.json"
    write_json(summary_path, payload)
    return summary_path


def default_normalized_dir(run_date: str, account_slug: str) -> Path:
    return DEFAULT_NORMALIZED_ROOT / account_slug / run_date


def default_report_dir(run_date: str, account_slug: str) -> Path:
    return DEFAULT_REPORTS_ROOT / account_slug / run_date


def resolve_date_window(
    run_date_value: date,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[str, str]:
    resolved_date_to = _require_date(date_to, "date_to") if date_to else run_date_value
    if date_from:
        resolved_date_from = _require_date(date_from, "date_from")
    else:
        resolved_date_from = resolved_date_to - timedelta(days=lookback_days - 1)
    if resolved_date_from > resolved_date_to:
        raise ValueError("date_from must be on or before date_to.")
    return resolved_date_from.isoformat(), resolved_date_to.isoformat()


def _build_performance_row(
    row: dict[str, Any],
    account: MetaAdsAccount,
    warnings: list[str],
) -> dict[str, Any]:
    actions = _metric_blob_list(row.get("actions"))
    action_values = _metric_blob_list(row.get("action_values"))
    cost_per_action_type = _metric_blob_list(row.get("cost_per_action_type"))
    purchase_roas_blob = _metric_blob_list(row.get("purchase_roas"))

    primary_result_key = account.primary_result_action_type or _infer_primary_result_action(actions)
    if primary_result_key is None:
        warnings.append(
            f"No primary result action could be inferred for account {account.account_slug}; Results may be blank."
        )
    result_label = account.primary_result_label or _label_for_action(primary_result_key)
    results = _find_metric(actions, [primary_result_key] if primary_result_key else [])
    cost_per_result = _find_metric(cost_per_action_type, [primary_result_key] if primary_result_key else [])

    purchase_count = _find_metric(actions, PURCHASE_KEYS)
    purchase_value = _find_metric(action_values, PURCHASE_KEYS)
    average_purchase_value = safe_divide(purchase_value, purchase_count)
    purchase_roas = _find_metric(purchase_roas_blob, PURCHASE_KEYS)
    if purchase_roas is None:
        purchase_roas = safe_divide(purchase_value, _number(row.get("spend")))

    app_installs = _find_metric(actions, APP_INSTALL_KEYS)
    cost_per_app_install = _find_metric(cost_per_action_type, APP_INSTALL_KEYS)

    link_clicks = _extract_metric_value(row.get("inline_link_clicks"), LINK_CLICK_KEYS)
    outbound_clicks = _extract_metric_value(row.get("outbound_clicks"), OUTBOUND_CLICK_KEYS)
    if outbound_clicks is None:
        outbound_clicks = _find_metric(actions, OUTBOUND_CLICK_KEYS)
    if link_clicks is None:
        link_clicks = _find_metric(actions, LINK_CLICK_KEYS)

    return {
        "Account ID": row.get("account_id") or "",
        "Account name": row.get("account_name") or account.account_name,
        "Campaign ID": row.get("campaign_id") or "",
        "Campaign name": row.get("campaign_name") or "",
        "Ad set ID": row.get("adset_id") or "",
        "Ad set name": row.get("adset_name") or "",
        "Ad ID": row.get("ad_id") or "",
        "Ad name": row.get("ad_name") or "",
        "Day": row.get("date_start") or "",
        "Reporting starts": row.get("date_start") or "",
        "Reporting ends": row.get("date_stop") or row.get("date_start") or "",
        "Reach": _stringify_number(row.get("reach")),
        "Impressions": _stringify_number(row.get("impressions")),
        "Frequency": _stringify_number(row.get("frequency")),
        "Clicks (all)": _stringify_number(row.get("clicks")),
        "Link clicks": _stringify_number(link_clicks),
        "Outbound clicks": _stringify_number(outbound_clicks),
        "CTR (all)": _stringify_number(row.get("ctr")),
        "CPC (cost per link click)": _stringify_number(row.get("cost_per_inline_link_click")),
        "CPC (all)": _stringify_number(row.get("cpc")),
        "CPM (cost per 1,000 impressions)": _stringify_number(row.get("cpm")),
        "Amount spent (USD)": _stringify_number(row.get("spend")),
        "Objective": row.get("objective") or "",
        "Result type": result_label or "",
        "Results": _stringify_number(results),
        "Cost per result": _stringify_number(cost_per_result),
        "App installs": _stringify_number(app_installs),
        "Cost per app install": _stringify_number(cost_per_app_install),
        "Purchases": _stringify_number(purchase_count),
        "Average purchases conversion value": _stringify_number(average_purchase_value),
        "Purchase ROAS (return on ad spend)": _stringify_metric_blob(purchase_roas_blob)
        if purchase_roas_blob
        else _stringify_number(purchase_roas),
        "Results ROAS": "",
        "Action values": _stringify_metric_blob(action_values),
        "Actions": _stringify_metric_blob(actions),
        "Cost per action type": _stringify_metric_blob(cost_per_action_type),
    }


def _build_video_row(row: dict[str, Any]) -> dict[str, Any]:
    actions = _metric_blob_list(row.get("actions"))
    video_3_sec = _find_metric(actions, VIDEO_3S_KEYS)
    thruplays = _find_metric(actions, THRUPLAY_KEYS)
    return {
        "Ad ID": row.get("ad_id") or "",
        "Ad name": row.get("ad_name") or "",
        "Day": row.get("date_start") or "",
        "Impressions": _stringify_number(row.get("impressions")),
        "3-second video plays": _stringify_number(video_3_sec),
        "ThruPlays": _stringify_number(thruplays),
    }


def _build_creative_row(row: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    creative = row.get("creative") if isinstance(row.get("creative"), dict) else {}
    object_story_spec = creative.get("object_story_spec") if isinstance(creative.get("object_story_spec"), dict) else {}
    asset_feed_spec = creative.get("asset_feed_spec") if isinstance(creative.get("asset_feed_spec"), dict) else {}

    preview_link = ""
    post_link = ""
    effective_story_id = creative.get("effective_object_story_id")
    if effective_story_id:
        post_link = f"https://www.facebook.com/{effective_story_id}"
    else:
        warnings.append("Some creative rows were missing effective_object_story_id, so post links were left blank.")

    return {
        "Ad ID": row.get("id") or "",
        "Ad name": row.get("name") or "",
        "Media type": _infer_creative_type(object_story_spec, asset_feed_spec),
        "Primary text": _extract_primary_text(object_story_spec, asset_feed_spec),
        "Headline": _extract_headline(object_story_spec, asset_feed_spec, creative),
        "Launch date": row.get("created_time") or "",
        "Preview link": preview_link,
        "Post link": post_link,
    }


def _metric_blob_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _extract_metric_value(value: Any, keys: list[str]) -> float | None:
    if isinstance(value, (str, int, float)):
        return _number(value)
    if isinstance(value, list):
        return _find_metric(_metric_blob_list(value), keys)
    return None


def _find_metric(metrics: list[dict[str, Any]], keys: list[str]) -> float | None:
    lowered_keys = [key.lower() for key in keys if key]
    for item in metrics:
        action_type = str(item.get("action_type") or item.get("metric") or "").strip().lower()
        if action_type in lowered_keys:
            return _number(item.get("value"))
    return None


def _infer_primary_result_action(actions: list[dict[str, Any]]) -> str | None:
    action_types = [str(item.get("action_type") or "").strip().lower() for item in actions]
    for action_type in action_types:
        if "subscribe" in action_type or "subscription" in action_type:
            return action_type
    for candidate_group in (PURCHASE_KEYS, APP_INSTALL_KEYS):
        for candidate in candidate_group:
            if candidate in action_types:
                return candidate
    return None


def _label_for_action(action_type: str | None) -> str | None:
    if not action_type:
        return None
    lowered = action_type.lower()
    if "subscribe" in lowered or "subscription" in lowered:
        return "In-app subscriptions"
    if "purchase" in lowered:
        return "Purchases"
    if "install" in lowered:
        return "App installs"
    return action_type.replace("_", " ").title()


def _infer_creative_type(
    object_story_spec: dict[str, Any],
    asset_feed_spec: dict[str, Any],
) -> str:
    if asset_feed_spec:
        return "Dynamic"
    if "video_data" in object_story_spec:
        return "Video"
    if "photo_data" in object_story_spec:
        return "Image"
    link_data = object_story_spec.get("link_data")
    if isinstance(link_data, dict) and link_data.get("child_attachments"):
        return "Carousel"
    if isinstance(link_data, dict):
        return "Image"
    return "Unknown"


def _extract_primary_text(
    object_story_spec: dict[str, Any],
    asset_feed_spec: dict[str, Any],
) -> str:
    for key in ("link_data", "video_data", "photo_data"):
        section = object_story_spec.get(key)
        if isinstance(section, dict):
            message = section.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    bodies = asset_feed_spec.get("bodies")
    if isinstance(bodies, list):
        for body in bodies:
            if isinstance(body, dict) and isinstance(body.get("text"), str) and body["text"].strip():
                return body["text"].strip()
    return ""


def _extract_headline(
    object_story_spec: dict[str, Any],
    asset_feed_spec: dict[str, Any],
    creative: dict[str, Any],
) -> str:
    for key in ("link_data", "video_data"):
        section = object_story_spec.get(key)
        if isinstance(section, dict):
            for candidate in ("name", "title"):
                value = section.get(candidate)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    titles = asset_feed_spec.get("titles")
    if isinstance(titles, list):
        for title in titles:
            if isinstance(title, dict) and isinstance(title.get("text"), str) and title["text"].strip():
                return title["text"].strip()
    for candidate in ("title", "body"):
        value = creative.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stringify_metric_blob(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return json.dumps(items, separators=(",", ":"))


def _stringify_number(value: Any) -> str:
    parsed = _number(value)
    if parsed is None:
        return ""
    if parsed.is_integer():
        return str(int(parsed))
    return f"{parsed:.6f}".rstrip("0").rstrip(".")


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _require_date(value: str, label: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{label} must be a valid date in YYYY-MM-DD format.")
    return parsed
