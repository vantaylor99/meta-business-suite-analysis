"""Analysis and scoring logic for normalized ad data."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from .config import (
    FATIGUE_WINDOW_DAYS,
    MIN_FATIGUE_HISTORY_DAYS,
    MIN_SCALING_SPEND,
    MIN_WASTE_SPEND,
    TOP_FINDINGS_LIMIT,
)
from .utils import clamp, percent_change, safe_divide


@dataclass(slots=True)
class WindowMetrics:
    spend: float
    impressions: int
    outbound_clicks: int
    results: float
    app_installs: float
    purchase_count: float
    purchase_value: float
    average_frequency: float | None
    hook_rate: float | None

    @property
    def ctr(self) -> float | None:
        return safe_divide(self.outbound_clicks, self.impressions)

    @property
    def roas(self) -> float | None:
        return safe_divide(self.purchase_value, self.spend)

    @property
    def cpa(self) -> float | None:
        return safe_divide(self.spend, self.purchase_count)

    @property
    def cost_per_result(self) -> float | None:
        return safe_divide(self.spend, self.results)

    @property
    def cost_per_app_install(self) -> float | None:
        return safe_divide(self.spend, self.app_installs)


def build_report_payload(
    rows: list[dict[str, Any]],
    run_date: str,
    *,
    measurement_focus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not rows:
        raise ValueError(f"No normalized rows found for run date {run_date}")

    sorted_rows = sorted(rows, key=lambda row: (row["report_date"], row.get("ad_name") or ""))
    grouped = _group_rows(sorted_rows)

    total_spend = sum(row.get("spend") or 0.0 for row in sorted_rows)
    total_purchase_value = sum(row.get("purchase_value") or 0.0 for row in sorted_rows)
    total_purchase_count = sum(row.get("purchase_count") or 0.0 for row in sorted_rows)
    total_results = sum(row.get("results") or 0.0 for row in sorted_rows)
    total_app_installs = sum(row.get("app_installs") or 0.0 for row in sorted_rows)
    campaign_count = len({row.get("campaign_id") or row.get("campaign_name") for row in sorted_rows})
    adset_count = len({row.get("adset_id") or row.get("adset_name") for row in sorted_rows})
    ad_count = len(grouped)
    unique_dates = sorted({row["report_date"] for row in sorted_rows})

    ad_summaries = [_summarize_ad(rows_for_ad) for rows_for_ad in grouped.values()]
    account_roas = _reliable_roas(
        total_purchase_value,
        total_spend,
        total_results,
        any(item["tracking_confidence"] == "low_results_without_revenue" for item in ad_summaries),
    )
    account_cost_per_result = safe_divide(total_spend, total_results)
    account_cost_per_app_install = safe_divide(total_spend, total_app_installs)
    benchmark_ctr = _median_defined([item["outbound_ctr"] for item in ad_summaries])
    benchmark_hook = _median_defined([item["hook_rate"] for item in ad_summaries])
    benchmark_cpa = _median_defined([item["cpa"] for item in ad_summaries])

    for item in ad_summaries:
        _apply_fatigue(item)
        _apply_waste(
            item,
            total_spend,
            total_results,
            total_app_installs,
            account_roas,
            account_cost_per_result,
            account_cost_per_app_install,
            benchmark_ctr,
            benchmark_hook,
        )
        _apply_scaling(
            item,
            account_roas,
            account_cost_per_result,
            account_cost_per_app_install,
            benchmark_cpa,
            benchmark_hook,
        )

    waste_ads = sorted(
        [item for item in ad_summaries if item["waste_status"] != "insufficient_data"],
        key=lambda item: (item["waste_score"], item["total_spend"]),
        reverse=True,
    )[:TOP_FINDINGS_LIMIT]
    fatigued_ads = sorted(
        [item for item in ad_summaries if item["fatigue_score"] is not None],
        key=lambda item: item["fatigue_score"],
        reverse=True,
    )[:TOP_FINDINGS_LIMIT]
    strong_hooks = sorted(
        [item for item in ad_summaries if item["hook_rate"] is not None],
        key=lambda item: (item["hook_rate"], item["hold_rate"] or 0.0),
        reverse=True,
    )[:TOP_FINDINGS_LIMIT]
    weak_hooks = sorted(
        [item for item in ad_summaries if item["hook_rate"] is not None],
        key=lambda item: (item["hook_rate"], -(item["hold_rate"] or 0.0)),
    )[:TOP_FINDINGS_LIMIT]
    scaling_candidates = sorted(
        [item for item in ad_summaries if item["scaling_candidate"]],
        key=lambda item: item["scaling_score"],
        reverse=True,
    )[:TOP_FINDINGS_LIMIT]

    tracking_concerns = _build_tracking_concerns(ad_summaries)
    actions = _build_recommendations(waste_ads, fatigued_ads, scaling_candidates, tracking_concerns)

    return {
        "run_date": run_date,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "measurement_focus": measurement_focus or {},
        "account_summary": {
            "start_date": unique_dates[0].isoformat(),
            "end_date": unique_dates[-1].isoformat(),
            "days": len(unique_dates),
            "campaign_count": campaign_count,
            "adset_count": adset_count,
            "ad_count": ad_count,
            "total_spend": round(total_spend, 2),
            "total_purchase_value": round(total_purchase_value, 2),
            "total_purchase_count": round(total_purchase_count, 2),
            "total_results": round(total_results, 2),
            "total_app_installs": round(total_app_installs, 2),
            "blended_roas": round(account_roas, 4) if account_roas is not None else None,
        },
        "benchmarks": {
            "account_blended_roas": round(account_roas, 4) if account_roas is not None else None,
            "account_cost_per_result": round(account_cost_per_result, 2)
            if account_cost_per_result is not None
            else None,
            "account_cost_per_app_install": round(account_cost_per_app_install, 2)
            if account_cost_per_app_install is not None
            else None,
            "median_outbound_ctr": round(benchmark_ctr, 4) if benchmark_ctr is not None else None,
            "median_hook_rate": round(benchmark_hook, 4) if benchmark_hook is not None else None,
            "median_cpa": round(benchmark_cpa, 2) if benchmark_cpa is not None else None,
            "min_waste_spend": MIN_WASTE_SPEND,
            "min_scaling_spend": MIN_SCALING_SPEND,
        },
        "budget_waste": [_serialize_ad_summary(item) for item in waste_ads],
        "fatigue_findings": [_serialize_ad_summary(item) for item in fatigued_ads],
        "hook_findings": {
            "strong": [_serialize_ad_summary(item) for item in strong_hooks],
            "weak": [_serialize_ad_summary(item) for item in weak_hooks],
        },
        "scaling_candidates": [_serialize_ad_summary(item) for item in scaling_candidates],
        "tracking_concerns": tracking_concerns,
        "next_7_day_actions": actions,
    }


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = row.get("ad_id") or row.get("ad_name") or "unknown_ad"
        grouped[key].append(row)
    return grouped


def _summarize_ad(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["report_date"])
    total_spend = sum(row.get("spend") or 0.0 for row in rows)
    total_purchase_value = sum(row.get("purchase_value") or 0.0 for row in rows)
    total_purchase_count = sum(row.get("purchase_count") or 0.0 for row in rows)
    total_results = sum(row.get("results") or 0.0 for row in rows)
    total_app_installs = sum(row.get("app_installs") or 0.0 for row in rows)
    total_impressions = sum(row.get("impressions") or 0 for row in rows)
    total_outbound_clicks = sum(row.get("outbound_clicks") or 0 for row in rows)
    total_video_3s_plays = sum(row.get("video_3s_plays") or 0.0 for row in rows)
    total_thruplays = sum(row.get("thruplays") or 0.0 for row in rows)
    frequencies = [row["frequency"] for row in rows if row.get("frequency") is not None]
    has_video_metrics = any(row.get("has_video_metrics") for row in rows)

    prior_window, recent_window = _split_windows(rows)
    return {
        "ad_id": rows[0].get("ad_id"),
        "ad_name": rows[0].get("ad_name"),
        "campaign_name": rows[0].get("campaign_name"),
        "adset_name": rows[0].get("adset_name"),
        "creative_type": rows[0].get("creative_type"),
        "tracking_confidence": _worst_tracking_confidence(rows),
        "days_active": len({row["report_date"] for row in rows}),
        "first_seen": rows[0]["report_date"],
        "last_seen": rows[-1]["report_date"],
        "total_spend": total_spend,
        "total_purchase_value": total_purchase_value,
        "total_purchase_count": total_purchase_count,
        "total_results": total_results,
        "result_label": _dominant_result_label(rows),
        "total_app_installs": total_app_installs,
        "impressions": total_impressions,
        "outbound_clicks": total_outbound_clicks,
        "outbound_ctr": safe_divide(total_outbound_clicks, total_impressions),
        "hook_rate": safe_divide(total_video_3s_plays, total_impressions) if has_video_metrics else None,
        "hold_rate": safe_divide(total_thruplays, total_video_3s_plays) if has_video_metrics else None,
        "blended_roas": _reliable_roas(
            total_purchase_value,
            total_spend,
            total_results,
            _worst_tracking_confidence(rows) == "low_results_without_revenue",
        ),
        "cpa": safe_divide(total_spend, total_purchase_count),
        "cost_per_result": safe_divide(total_spend, total_results),
        "cost_per_app_install": safe_divide(total_spend, total_app_installs),
        "average_frequency": sum(frequencies) / len(frequencies) if frequencies else None,
        "has_video_metrics": has_video_metrics,
        "prior_window": _window_metrics(prior_window),
        "recent_window": _window_metrics(recent_window),
        "fatigue_score": None,
        "fatigue_status": "insufficient_history",
        "fatigue_reasons": [],
        "waste_score": 0.0,
        "waste_status": "insufficient_data" if total_spend < MIN_WASTE_SPEND else "monitor",
        "waste_reasons": [],
        "scaling_candidate": False,
        "scaling_score": 0.0,
    }


def _split_windows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    unique_dates = sorted({row["report_date"] for row in rows})
    if len(unique_dates) < MIN_FATIGUE_HISTORY_DAYS:
        return [], []
    prior_dates = set(unique_dates[-(FATIGUE_WINDOW_DAYS * 2) : -FATIGUE_WINDOW_DAYS])
    recent_dates = set(unique_dates[-FATIGUE_WINDOW_DAYS:])
    prior_rows = [row for row in rows if row["report_date"] in prior_dates]
    recent_rows = [row for row in rows if row["report_date"] in recent_dates]
    return prior_rows, recent_rows


def _window_metrics(rows: list[dict[str, Any]]) -> WindowMetrics:
    if not rows:
        return WindowMetrics(0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, None, None)
    spend = sum(row.get("spend") or 0.0 for row in rows)
    impressions = sum(row.get("impressions") or 0 for row in rows)
    outbound_clicks = sum(row.get("outbound_clicks") or 0 for row in rows)
    results = sum(row.get("results") or 0.0 for row in rows)
    app_installs = sum(row.get("app_installs") or 0.0 for row in rows)
    purchase_count = sum(row.get("purchase_count") or 0.0 for row in rows)
    purchase_value = sum(row.get("purchase_value") or 0.0 for row in rows)
    frequencies = [row["frequency"] for row in rows if row.get("frequency") is not None]
    total_video_3s_plays = sum(row.get("video_3s_plays") or 0.0 for row in rows)
    return WindowMetrics(
        spend=spend,
        impressions=impressions,
        outbound_clicks=outbound_clicks,
        results=results,
        app_installs=app_installs,
        purchase_count=purchase_count,
        purchase_value=purchase_value,
        average_frequency=(sum(frequencies) / len(frequencies) if frequencies else None),
        hook_rate=safe_divide(total_video_3s_plays, impressions),
    )


def _apply_fatigue(summary: dict[str, Any]) -> None:
    prior: WindowMetrics = summary["prior_window"]
    recent: WindowMetrics = summary["recent_window"]
    if prior.spend == 0 or recent.spend == 0:
        summary["fatigue_status"] = "insufficient_history"
        return

    score = 0.0
    reasons: list[str] = []

    frequency_change = percent_change(recent.average_frequency, prior.average_frequency)
    if frequency_change is not None and frequency_change > 0.15:
        score += clamp(frequency_change * 60, 0, 30)
        reasons.append(
            f"frequency rose {frequency_change * 100:.1f}% between the prior and recent windows"
        )

    ctr_change = percent_change(recent.ctr, prior.ctr)
    if ctr_change is not None and ctr_change < -0.15:
        score += clamp(abs(ctr_change) * 70, 0, 25)
        reasons.append(f"outbound CTR fell {abs(ctr_change) * 100:.1f}%")

    cost_per_result_change = percent_change(recent.cost_per_result, prior.cost_per_result)
    if cost_per_result_change is not None and cost_per_result_change > 0.15:
        score += clamp(cost_per_result_change * 60, 0, 25)
        reasons.append(f"cost per result rose {cost_per_result_change * 100:.1f}%")
    else:
        cost_per_install_change = percent_change(recent.cost_per_app_install, prior.cost_per_app_install)
        if cost_per_install_change is not None and cost_per_install_change > 0.15:
            score += clamp(cost_per_install_change * 45, 0, 18)
            reasons.append(f"cost per app install rose {cost_per_install_change * 100:.1f}%")

    roas_change = percent_change(recent.roas, prior.roas)
    if roas_change is not None and roas_change < -0.15:
        score += clamp(abs(roas_change) * 35, 0, 15)
        reasons.append(f"ROAS fell {abs(roas_change) * 100:.1f}%")

    summary["fatigue_score"] = round(clamp(score), 1) if reasons else None
    if summary["fatigue_score"] is None:
        summary["fatigue_status"] = "stable_or_inconclusive"
    elif summary["fatigue_score"] >= 60:
        summary["fatigue_status"] = "high"
    elif summary["fatigue_score"] >= 35:
        summary["fatigue_status"] = "medium"
    else:
        summary["fatigue_status"] = "low"
    summary["fatigue_reasons"] = reasons


def _apply_waste(
    summary: dict[str, Any],
    total_spend: float,
    total_results: float,
    total_app_installs: float,
    account_roas: float | None,
    account_cost_per_result: float | None,
    account_cost_per_app_install: float | None,
    benchmark_ctr: float | None,
    benchmark_hook: float | None,
) -> None:
    spend = summary["total_spend"]
    if spend < MIN_WASTE_SPEND:
        summary["waste_status"] = "insufficient_data"
        summary["waste_reasons"] = [
            f"spent ${spend:.2f}, below the waste-review threshold of ${MIN_WASTE_SPEND:.2f}"
        ]
        summary["waste_score"] = 0.0
        return

    score = 0.0
    reasons: list[str] = []
    total_results_for_ad = summary["total_results"]
    total_app_installs_for_ad = summary["total_app_installs"]
    cost_per_result = summary["cost_per_result"]
    cost_per_app_install = summary["cost_per_app_install"]
    roas = summary["blended_roas"]

    if total_results_for_ad in (None, 0):
        score += clamp((spend / MIN_WASTE_SPEND) * 20, 20, 45)
        reasons.append(f"spent ${spend:.2f} without recorded primary results")

        if total_app_installs_for_ad in (None, 0):
            score += 10
            reasons.append("delivery also failed to produce recorded app installs")
        elif (
            account_cost_per_app_install is not None
            and cost_per_app_install is not None
            and cost_per_app_install > account_cost_per_app_install * 1.5
        ):
            score += 10
            reasons.append(
                f"cost per app install is materially worse than the account benchmark at ${cost_per_app_install:.2f}"
            )
    elif (
        account_cost_per_result is not None
        and cost_per_result is not None
        and cost_per_result > account_cost_per_result * 1.4
    ):
        score += 18
        reasons.append(
            f"cost per result is materially worse than the account benchmark at ${cost_per_result:.2f}"
        )

    if roas is not None and roas < 1.0:
        score += clamp((1.0 - roas) * 18, 0, 12)
        reasons.append(f"blended ROAS is below 1.0 at {roas:.2f}")

    if roas is not None and account_roas is not None and roas < account_roas * 0.6:
        score += 8
        reasons.append(f"ROAS materially trails the account benchmark of {account_roas:.2f}")

    spend_share = safe_divide(spend, total_spend) or 0.0
    if total_results > 0:
        value_share = safe_divide(summary["total_results"], total_results) or 0.0
    elif total_app_installs > 0:
        value_share = safe_divide(summary["total_app_installs"], total_app_installs) or 0.0
    else:
        value_share = 0.0
    if spend_share > value_share * 1.5:
        score += clamp((spend_share - value_share) * 100, 0, 20)
        reasons.append("spend share is materially higher than contribution share")

    if benchmark_ctr is not None and summary["outbound_ctr"] is not None and summary["outbound_ctr"] < benchmark_ctr * 0.7:
        score += 8
        reasons.append("outbound CTR is weak versus the account median")

    if benchmark_hook is not None and summary["hook_rate"] is not None and summary["hook_rate"] < benchmark_hook * 0.7:
        score += 7
        reasons.append("hook rate is weak versus the account median")

    summary["waste_score"] = round(clamp(score), 1)
    if summary["waste_score"] >= 70:
        summary["waste_status"] = "high"
    elif summary["waste_score"] >= 45:
        summary["waste_status"] = "medium"
    else:
        summary["waste_status"] = "monitor"
    summary["waste_reasons"] = reasons


def _apply_scaling(
    summary: dict[str, Any],
    account_roas: float | None,
    account_cost_per_result: float | None,
    account_cost_per_app_install: float | None,
    benchmark_cpa: float | None,
    benchmark_hook: float | None,
) -> None:
    spend = summary["total_spend"]
    roas = summary["blended_roas"]
    fatigue_score = summary["fatigue_score"] or 0.0
    total_results = summary["total_results"]
    total_app_installs = summary["total_app_installs"]
    cost_per_result = summary["cost_per_result"]
    cost_per_app_install = summary["cost_per_app_install"]
    if spend < MIN_SCALING_SPEND:
        summary["scaling_candidate"] = False
        return

    score = 0.0
    if total_results and account_cost_per_result is not None and cost_per_result is not None:
        if cost_per_result <= account_cost_per_result * 0.85:
            score += 35
        elif cost_per_result <= account_cost_per_result:
            score += 20
    elif total_results == 0 and total_app_installs and account_cost_per_app_install is not None and cost_per_app_install is not None:
        if cost_per_app_install <= account_cost_per_app_install * 0.8:
            score += 18
        elif cost_per_app_install <= account_cost_per_app_install:
            score += 10

    if fatigue_score < 35:
        score += 20
    if benchmark_cpa is not None and summary["cpa"] is not None and summary["cpa"] < benchmark_cpa * 0.85:
        score += 10
    if benchmark_hook is not None and summary["hook_rate"] is not None and summary["hook_rate"] >= benchmark_hook:
        score += 10
    if roas is not None and account_roas is not None and roas >= max(1.25, account_roas):
        score += 10
    if summary["tracking_confidence"] == "high":
        score += 10

    summary["scaling_score"] = round(clamp(score), 1)
    summary["scaling_candidate"] = (
        summary["scaling_score"] >= 55
        and total_results not in (None, 0)
        and total_results >= 2
    )


def _worst_tracking_confidence(rows: list[dict[str, Any]]) -> str:
    priority = {
        "low_results_without_revenue": 4,
        "low_purchase_value_missing": 3,
        "medium_roas_unavailable": 2,
        "high": 1,
    }
    values = [row.get("tracking_confidence") or "high" for row in rows]
    return max(values, key=lambda item: priority.get(item, 0))


def _build_tracking_concerns(ad_summaries: list[dict[str, Any]]) -> list[str]:
    concerns: list[str] = []
    low_revenue_visibility_ads = [
        item for item in ad_summaries if item["tracking_confidence"] == "low_results_without_revenue"
    ]
    low_confidence_ads = [item for item in ad_summaries if item["tracking_confidence"] == "low_purchase_value_missing"]
    medium_confidence_ads = [item for item in ad_summaries if item["tracking_confidence"] == "medium_roas_unavailable"]
    video_coverage = safe_divide(
        sum(1 for item in ad_summaries if item["has_video_metrics"]),
        len(ad_summaries),
    ) or 0.0

    if low_revenue_visibility_ads:
        concerns.append(
            f"{len(low_revenue_visibility_ads)} ads recorded primary results without purchase value, so revenue-based ROAS is not trustworthy for those ads yet."
        )
    if low_confidence_ads:
        concerns.append(
            f"{len(low_confidence_ads)} ads recorded purchases without reliable purchase value, so ROAS is low-confidence for those ads."
        )
    if medium_confidence_ads and not low_confidence_ads and not low_revenue_visibility_ads:
        concerns.append(
            f"{len(medium_confidence_ads)} ads were missing direct ROAS fields and relied on derived calculations or partial commercial data."
        )
    if video_coverage < 0.5:
        concerns.append(
            "Less than half of ads have usable video metrics, so hook-rate findings reflect only the covered creative subset."
        )
    if not concerns:
        concerns.append(
            "No major measurement integrity issue was inferred from the supplied exports, but direct Pixel and Conversions API health was not provided."
        )
    return concerns


def _build_recommendations(
    waste_ads: list[dict[str, Any]],
    fatigued_ads: list[dict[str, Any]],
    scaling_candidates: list[dict[str, Any]],
    tracking_concerns: list[str],
) -> list[str]:
    actions: list[str] = []
    if waste_ads:
        top = waste_ads[0]
        actions.append(
            f"Reduce or pause budget on {top['ad_name']} first because it combines meaningful spend with weak value output."
        )
    if fatigued_ads:
        top = fatigued_ads[0]
        actions.append(
            f"Refresh or rotate {top['ad_name']} because recent delivery patterns suggest fatigue rather than simple volatility."
        )
    if scaling_candidates:
        top = scaling_candidates[0]
        actions.append(
            f"Consider carefully scaling {top['ad_name']} because it pairs stronger efficiency with manageable fatigue risk."
        )
    if tracking_concerns:
        actions.append(
            "Review measurement quality before making aggressive budget decisions if purchase value or ROAS confidence is limited."
        )
    return actions[:TOP_FINDINGS_LIMIT]


def _serialize_ad_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "ad_id": summary["ad_id"],
        "ad_name": summary["ad_name"],
        "campaign_name": summary["campaign_name"],
        "adset_name": summary["adset_name"],
        "creative_type": summary["creative_type"],
        "days_active": summary["days_active"],
        "first_seen": summary["first_seen"].isoformat(),
        "last_seen": summary["last_seen"].isoformat(),
        "total_spend": round(summary["total_spend"], 2),
        "total_purchase_value": round(summary["total_purchase_value"], 2),
        "total_purchase_count": round(summary["total_purchase_count"], 2),
        "total_results": round(summary["total_results"], 2),
        "result_label": summary["result_label"],
        "cost_per_result": round(summary["cost_per_result"], 2)
        if summary["cost_per_result"] is not None
        else None,
        "total_app_installs": round(summary["total_app_installs"], 2),
        "cost_per_app_install": round(summary["cost_per_app_install"], 2)
        if summary["cost_per_app_install"] is not None
        else None,
        "blended_roas": round(summary["blended_roas"], 4) if summary["blended_roas"] is not None else None,
        "cpa": round(summary["cpa"], 2) if summary["cpa"] is not None else None,
        "outbound_ctr": round(summary["outbound_ctr"], 4) if summary["outbound_ctr"] is not None else None,
        "hook_rate": round(summary["hook_rate"], 4) if summary["hook_rate"] is not None else None,
        "hold_rate": round(summary["hold_rate"], 4) if summary["hold_rate"] is not None else None,
        "tracking_confidence": summary["tracking_confidence"],
        "fatigue_score": summary["fatigue_score"],
        "fatigue_status": summary["fatigue_status"],
        "fatigue_reasons": summary["fatigue_reasons"],
        "waste_score": summary["waste_score"],
        "waste_status": summary["waste_status"],
        "waste_reasons": summary["waste_reasons"],
        "scaling_score": summary["scaling_score"],
        "scaling_candidate": summary["scaling_candidate"],
    }


def _median_defined(values: list[float | None]) -> float | None:
    defined = [value for value in values if value is not None]
    if not defined:
        return None
    return median(defined)


def _dominant_result_label(rows: list[dict[str, Any]]) -> str | None:
    labels: dict[str, int] = defaultdict(int)
    for row in rows:
        label = row.get("result_label")
        if label:
            labels[label] += 1
    if not labels:
        return None
    return max(labels, key=labels.get)


def _reliable_roas(
    purchase_value: float | None,
    spend: float | None,
    total_results: float | None,
    revenue_visibility_is_low: bool,
) -> float | None:
    if revenue_visibility_is_low and purchase_value in (None, 0):
        return None
    if total_results not in (None, 0) and purchase_value in (None, 0):
        return None
    return safe_divide(purchase_value, spend)
