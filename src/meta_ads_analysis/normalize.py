"""Normalization for raw Meta Ads CSV exports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .utils import (
    parse_date,
    parse_int,
    parse_metric_blob,
    parse_number,
    read_csv_rows,
    safe_divide,
    standardize_header,
)


@dataclass(slots=True)
class IngestionArtifacts:
    run_date: str
    account_slug: str | None
    input_dir: Path
    normalized_rows: list[dict[str, Any]]
    creative_rows: list[dict[str, Any]]
    warnings: list[str]


PERFORMANCE_ALIASES = {
    "report_date": [
        "day",
        "date",
        "reporting_starts",
        "date_start",
        "reporting_starts_account_timezone",
    ],
    "date_stop": ["date_stop", "reporting_ends"],
    "account_id": ["account_id", "ad_account_id"],
    "account_name": ["account_name", "ad_account_name"],
    "campaign_id": ["campaign_id"],
    "campaign_name": ["campaign_name", "campaign_name_2"],
    "adset_id": ["ad_set_id", "adset_id"],
    "adset_name": ["ad_set_name", "adset_name"],
    "ad_id": ["ad_id"],
    "ad_name": ["ad_name"],
    "objective": ["objective"],
    "spend": ["amount_spent", "amount_spent_usd", "spend"],
    "impressions": ["impressions"],
    "reach": ["reach"],
    "frequency": ["frequency"],
    "clicks": ["clicks", "all_clicks", "clicks_all"],
    "link_clicks": ["inline_link_clicks", "link_clicks"],
    "outbound_clicks": ["outbound_clicks", "outbound_click"],
    "ctr": ["ctr", "ctr_all", "inline_link_ctr"],
    "cpc": ["cpc", "cpc_all", "cost_per_inline_link_click", "cpc_cost_per_link_click"],
    "cpm": ["cpm", "cpm_cost_per_1_000_impressions"],
    "results": ["results", "purchases", "website_purchases"],
    "result_label": ["result_indicator", "result_type", "results_indicator"],
    "cost_per_result": ["cost_per_result", "cost_per_purchase"],
    "app_installs": ["app_installs"],
    "cost_per_app_install": ["cost_per_app_install"],
    "purchase_count": [
        "purchases",
        "website_purchases",
        "omni_purchase",
        "purchase",
    ],
    "purchase_value": [
        "website_purchases_conversion_value",
        "purchase_conversion_value",
        "website_purchase_conversion_value",
        "omni_purchase_conversion_value",
    ],
    "purchase_roas": [
        "purchase_roas_return_on_ad_spend",
        "website_purchase_roas_return_on_ad_spend",
        "purchase_roas",
        "website_purchase_roas",
        "results_roas",
    ],
    "actions": ["actions"],
    "action_values": ["action_values"],
    "cost_per_action_type": ["cost_per_action_type"],
    "purchase_roas_blob": ["purchase_roas_return_on_ad_spend_2", "purchase_roas_blob"],
}

VIDEO_ALIASES = {
    "report_date": ["day", "date", "reporting_starts", "date_start"],
    "ad_id": ["ad_id"],
    "ad_name": ["ad_name"],
    "video_3s_plays": [
        "3_second_video_plays",
        "video_plays_at_3_seconds",
        "three_second_video_plays",
        "video_3_second_plays",
        "video_3_sec_plays",
        "video_plays_3_seconds",
        "video_plays",
    ],
    "thruplays": ["thruplays", "thru_plays"],
    "impressions": ["impressions"],
}

CREATIVE_ALIASES = {
    "ad_id": ["ad_id"],
    "ad_name": ["ad_name"],
    "creative_type": ["creative_type", "format", "ad_type", "media_type"],
    "creative_copy": ["primary_text", "body", "text", "creative_copy"],
    "creative_headline": ["headline", "title", "creative_headline", "headline_ad_settings"],
    "launch_date": ["launch_date", "created_time", "ad_created_time"],
    "preview_link": ["preview_link", "ad_preview_link"],
    "post_link": ["post_link", "facebook_post_link", "instagram_post_link"],
}

PURCHASE_ACTION_KEYS = [
    "purchase",
    "website_purchase",
    "onsite_conversion.purchase",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
]

PURCHASE_VALUE_KEYS = [
    "purchase",
    "website_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
]

PURCHASE_ROAS_KEYS = [
    "purchase",
    "website_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "omni_purchase",
]


def ingest_raw_exports(
    input_dir: Path,
    run_date: str,
    account_slug: str | None = None,
) -> IngestionArtifacts:
    warnings: list[str] = []
    performance_path = input_dir / "performance_daily.csv"
    if not performance_path.exists():
        raise FileNotFoundError(
            f"Required file not found: {performance_path}. Expected performance_daily.csv"
        )

    performance_rows = _normalize_performance_rows(read_csv_rows(performance_path), warnings)

    video_path = input_dir / "video_daily.csv"
    video_rows = _normalize_video_rows(read_csv_rows(video_path), warnings) if video_path.exists() else []
    if not video_path.exists():
        warnings.append(
            "video_daily.csv was not provided, so hook-rate and hold-rate analysis will be limited."
        )

    creative_path = input_dir / "creative_lookup.csv"
    creative_rows = (
        _normalize_creative_rows(read_csv_rows(creative_path), warnings) if creative_path.exists() else []
    )

    merged_rows = _merge_sources(
        performance_rows,
        video_rows,
        creative_rows,
        run_date,
        account_slug,
        input_dir,
    )
    if not merged_rows:
        warnings.append("No normalized performance rows were produced from performance_daily.csv.")

    return IngestionArtifacts(
        run_date=run_date,
        account_slug=account_slug,
        input_dir=input_dir,
        normalized_rows=merged_rows,
        creative_rows=creative_rows,
        warnings=warnings,
    )


def normalized_fieldnames() -> list[str]:
    return [
        "ingestion_run_date",
        "account_slug",
        "source_run_path",
        "report_date",
        "account_id",
        "account_name",
        "campaign_id",
        "campaign_name",
        "adset_id",
        "adset_name",
        "ad_id",
        "ad_name",
        "objective",
        "spend",
        "impressions",
        "reach",
        "frequency",
        "clicks",
        "link_clicks",
        "outbound_clicks",
        "ctr",
        "cpc",
        "cpm",
        "results",
        "result_label",
        "cost_per_result",
        "app_installs",
        "cost_per_app_install",
        "purchase_count",
        "purchase_value",
        "purchase_roas",
        "video_3s_plays",
        "thruplays",
        "hook_rate",
        "hold_rate",
        "average_order_value",
        "creative_type",
        "creative_copy",
        "creative_headline",
        "launch_date",
        "preview_link",
        "post_link",
        "has_video_metrics",
        "tracking_confidence",
    ]


def creative_fieldnames() -> list[str]:
    return [
        "ad_id",
        "ad_name",
        "creative_type",
        "creative_copy",
        "creative_headline",
        "launch_date",
        "preview_link",
        "post_link",
    ]


def _normalize_performance_rows(rows: list[dict[str, str]], warnings: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        cleaned = _clean_row(row)
        report_date = _extract_date(cleaned, PERFORMANCE_ALIASES["report_date"])
        if report_date is None:
            warnings.append("Skipped a performance row because no valid date column was found.")
            continue

        actions = parse_metric_blob(_get_first(cleaned, PERFORMANCE_ALIASES["actions"]))
        action_values = parse_metric_blob(_get_first(cleaned, PERFORMANCE_ALIASES["action_values"]))
        purchase_roas_blob = parse_metric_blob(
            _get_first(cleaned, PERFORMANCE_ALIASES["purchase_roas_blob"])
            or _get_first(cleaned, PERFORMANCE_ALIASES["purchase_roas"])
        )

        purchase_count = _extract_number(cleaned, PERFORMANCE_ALIASES["purchase_count"])
        if purchase_count is None:
            purchase_count = _find_metric(actions, PURCHASE_ACTION_KEYS)

        purchase_value = _extract_number(cleaned, PERFORMANCE_ALIASES["purchase_value"])
        if purchase_value is None:
            purchase_value = _find_metric(action_values, PURCHASE_VALUE_KEYS)

        purchase_roas = _extract_number(cleaned, PERFORMANCE_ALIASES["purchase_roas"])
        if purchase_roas is None:
            purchase_roas = _find_metric(purchase_roas_blob, PURCHASE_ROAS_KEYS)

        spend = _extract_number(cleaned, PERFORMANCE_ALIASES["spend"]) or 0.0
        results = _extract_number(cleaned, PERFORMANCE_ALIASES["results"])
        if results is None:
            results = purchase_count
        app_installs = _extract_number(cleaned, PERFORMANCE_ALIASES["app_installs"])

        link_clicks = _extract_int(cleaned, PERFORMANCE_ALIASES["link_clicks"])
        outbound_clicks = _extract_int(cleaned, PERFORMANCE_ALIASES["outbound_clicks"])
        if outbound_clicks is None:
            outbound_clicks = link_clicks

        row_normalized = {
            "report_date": report_date,
            "account_id": _get_first(cleaned, PERFORMANCE_ALIASES["account_id"]),
            "account_name": _get_first(cleaned, PERFORMANCE_ALIASES["account_name"]),
            "campaign_id": _get_first(cleaned, PERFORMANCE_ALIASES["campaign_id"]),
            "campaign_name": _get_first(cleaned, PERFORMANCE_ALIASES["campaign_name"]),
            "adset_id": _get_first(cleaned, PERFORMANCE_ALIASES["adset_id"]),
            "adset_name": _get_first(cleaned, PERFORMANCE_ALIASES["adset_name"]),
            "ad_id": _get_first(cleaned, PERFORMANCE_ALIASES["ad_id"]),
            "ad_name": _get_first(cleaned, PERFORMANCE_ALIASES["ad_name"]),
            "objective": _get_first(cleaned, PERFORMANCE_ALIASES["objective"]),
            "spend": spend,
            "impressions": _extract_int(cleaned, PERFORMANCE_ALIASES["impressions"]),
            "reach": _extract_int(cleaned, PERFORMANCE_ALIASES["reach"]),
            "frequency": _extract_number(cleaned, PERFORMANCE_ALIASES["frequency"]),
            "clicks": _extract_int(cleaned, PERFORMANCE_ALIASES["clicks"]),
            "link_clicks": link_clicks,
            "outbound_clicks": outbound_clicks,
            "ctr": _extract_number(cleaned, PERFORMANCE_ALIASES["ctr"]),
            "cpc": _extract_number(cleaned, PERFORMANCE_ALIASES["cpc"]),
            "cpm": _extract_number(cleaned, PERFORMANCE_ALIASES["cpm"]),
            "results": results,
            "result_label": _get_first(cleaned, PERFORMANCE_ALIASES["result_label"]),
            "cost_per_result": _extract_number(cleaned, PERFORMANCE_ALIASES["cost_per_result"]),
            "app_installs": app_installs,
            "cost_per_app_install": _extract_number(
                cleaned, PERFORMANCE_ALIASES["cost_per_app_install"]
            ),
            "purchase_count": purchase_count,
            "purchase_value": purchase_value,
            "purchase_roas": purchase_roas,
        }

        if row_normalized["purchase_roas"] is None:
            row_normalized["purchase_roas"] = safe_divide(
                row_normalized["purchase_value"], row_normalized["spend"]
            )

        if row_normalized["cost_per_result"] is None and row_normalized["results"] not in (None, 0):
            row_normalized["cost_per_result"] = safe_divide(
                row_normalized["spend"], row_normalized["results"]
            )
        if (
            row_normalized["cost_per_app_install"] is None
            and row_normalized["app_installs"] not in (None, 0)
        ):
            row_normalized["cost_per_app_install"] = safe_divide(
                row_normalized["spend"], row_normalized["app_installs"]
            )

        normalized.append(row_normalized)
    return normalized


def _normalize_video_rows(rows: list[dict[str, str]], warnings: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        cleaned = _clean_row(row)
        report_date = _extract_date(cleaned, VIDEO_ALIASES["report_date"])
        if report_date is None:
            warnings.append("Skipped a video row because no valid date column was found.")
            continue
        normalized.append(
            {
                "report_date": report_date,
                "ad_id": _get_first(cleaned, VIDEO_ALIASES["ad_id"]),
                "ad_name": _get_first(cleaned, VIDEO_ALIASES["ad_name"]),
                "video_3s_plays": _extract_number(cleaned, VIDEO_ALIASES["video_3s_plays"]),
                "thruplays": _extract_number(cleaned, VIDEO_ALIASES["thruplays"]),
                "impressions": _extract_int(cleaned, VIDEO_ALIASES["impressions"]),
            }
        )
    return normalized


def _normalize_creative_rows(rows: list[dict[str, str]], warnings: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        cleaned = _clean_row(row)
        ad_id = _get_first(cleaned, CREATIVE_ALIASES["ad_id"])
        ad_name = _get_first(cleaned, CREATIVE_ALIASES["ad_name"])
        if not ad_id and not ad_name:
            warnings.append("Skipped a creative lookup row because it had neither ad_id nor ad_name.")
            continue
        normalized.append(
            {
                "ad_id": ad_id,
                "ad_name": ad_name,
                "creative_type": _get_first(cleaned, CREATIVE_ALIASES["creative_type"]),
                "creative_copy": _get_first(cleaned, CREATIVE_ALIASES["creative_copy"]),
                "creative_headline": _get_first(cleaned, CREATIVE_ALIASES["creative_headline"]),
                "launch_date": _extract_date(cleaned, CREATIVE_ALIASES["launch_date"]),
                "preview_link": _get_first(cleaned, CREATIVE_ALIASES["preview_link"]),
                "post_link": _get_first(cleaned, CREATIVE_ALIASES["post_link"]),
            }
        )
    return normalized


def _merge_sources(
    performance_rows: list[dict[str, Any]],
    video_rows: list[dict[str, Any]],
    creative_rows: list[dict[str, Any]],
    run_date: str,
    account_slug: str | None,
    input_dir: Path,
) -> list[dict[str, Any]]:
    video_lookup: dict[tuple[str | None, str | None, date], dict[str, Any]] = {}
    for row in video_rows:
        key = (row.get("ad_id"), row.get("ad_name"), row["report_date"])
        video_lookup[key] = row

    creative_by_id = {row["ad_id"]: row for row in creative_rows if row.get("ad_id")}
    creative_by_name = {
        row["ad_name"]: row for row in creative_rows if not row.get("ad_id") and row.get("ad_name")
    }

    merged: list[dict[str, Any]] = []
    for row in performance_rows:
        video = video_lookup.get((row.get("ad_id"), row.get("ad_name"), row["report_date"]))
        if video is None:
            video = video_lookup.get((row.get("ad_id"), None, row["report_date"]))
        if video is None and row.get("ad_name"):
            video = video_lookup.get((None, row["ad_name"], row["report_date"]))

        creative = creative_by_id.get(row.get("ad_id")) or creative_by_name.get(row.get("ad_name"))
        hook_rate = safe_divide(video.get("video_3s_plays"), row.get("impressions")) if video else None
        hold_rate = (
            safe_divide(video.get("thruplays"), video.get("video_3s_plays"))
            if video
            else None
        )
        average_order_value = safe_divide(row.get("purchase_value"), row.get("purchase_count"))
        tracking_confidence = "high"
        if row.get("results") not in (None, 0) and row.get("purchase_value") is None:
            tracking_confidence = "low_results_without_revenue"
        elif row.get("purchase_count") not in (None, 0) and row.get("purchase_value") is None:
            tracking_confidence = "low_purchase_value_missing"
        elif row.get("purchase_roas") is None and row.get("purchase_value") is None:
            tracking_confidence = "medium_roas_unavailable"

        merged.append(
            {
                "ingestion_run_date": run_date,
                "account_slug": account_slug,
                "source_run_path": str(input_dir),
                "report_date": row["report_date"],
                "account_id": row.get("account_id"),
                "account_name": row.get("account_name"),
                "campaign_id": row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "adset_id": row.get("adset_id"),
                "adset_name": row.get("adset_name"),
                "ad_id": row.get("ad_id"),
                "ad_name": row.get("ad_name"),
                "objective": row.get("objective"),
                "spend": row.get("spend"),
                "impressions": row.get("impressions"),
                "reach": row.get("reach"),
                "frequency": row.get("frequency"),
                "clicks": row.get("clicks"),
                "link_clicks": row.get("link_clicks"),
                "outbound_clicks": row.get("outbound_clicks"),
                "ctr": row.get("ctr"),
                "cpc": row.get("cpc"),
                "cpm": row.get("cpm"),
                "results": row.get("results"),
                "result_label": row.get("result_label"),
                "cost_per_result": row.get("cost_per_result"),
                "app_installs": row.get("app_installs"),
                "cost_per_app_install": row.get("cost_per_app_install"),
                "purchase_count": row.get("purchase_count"),
                "purchase_value": row.get("purchase_value"),
                "purchase_roas": row.get("purchase_roas"),
                "video_3s_plays": video.get("video_3s_plays") if video else None,
                "thruplays": video.get("thruplays") if video else None,
                "hook_rate": hook_rate,
                "hold_rate": hold_rate,
                "average_order_value": average_order_value,
                "creative_type": creative.get("creative_type") if creative else None,
                "creative_copy": creative.get("creative_copy") if creative else None,
                "creative_headline": creative.get("creative_headline") if creative else None,
                "launch_date": creative.get("launch_date") if creative else None,
                "preview_link": creative.get("preview_link") if creative else None,
                "post_link": creative.get("post_link") if creative else None,
                "has_video_metrics": bool(video and video.get("video_3s_plays") not in (None, 0)),
                "tracking_confidence": tracking_confidence,
            }
        )
    return merged


def _clean_row(row: dict[str, str]) -> dict[str, str]:
    return {standardize_header(key): value for key, value in row.items()}


def _get_first(cleaned: dict[str, str], aliases: list[str]) -> str | None:
    for alias in aliases:
        raw = cleaned.get(alias)
        if raw is not None and raw.strip():
            return raw.strip()
    return None


def _extract_number(cleaned: dict[str, str], aliases: list[str]) -> float | None:
    value = _get_first(cleaned, aliases)
    return parse_number(value)


def _extract_int(cleaned: dict[str, str], aliases: list[str]) -> int | None:
    value = _get_first(cleaned, aliases)
    return parse_int(value)


def _extract_date(cleaned: dict[str, str], aliases: list[str]) -> date | None:
    value = _get_first(cleaned, aliases)
    return parse_date(value)


def _find_metric(metrics: dict[str, float], keys: list[str]) -> float | None:
    for key in keys:
        if key in metrics:
            return metrics[key]
    return None
