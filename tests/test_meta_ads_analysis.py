from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

from meta_ads_analysis.actions import (
    apply_action_plan,
    build_action_plan,
    build_api_operation,
    enrich_action_plan_with_live_state,
    evaluate_action_confidence,
)
from meta_ads_analysis.account_registry import load_account_registry, resolve_account
from meta_ads_analysis.analyze import build_report_payload
from meta_ads_analysis.briefs import build_operator_brief, render_operator_brief
from meta_ads_analysis.cli import build_meta_report_main, ingest_meta_exports_main, sync_meta_api_main
from meta_ads_analysis.config import CONFIDENCE_RECENCY_STALE_DAYS
from meta_ads_analysis.confidence import (
    BAND_PRESENTATION,
    Band,
    Evidence,
    EvidenceTier,
    abstain_confidence,
    analog_confidence,
    assess,
    build_regenerating_query,
    combine_bands,
    confidence_from_dict,
    confidence_to_dict,
    data_strength,
    detect_causal_language,
    evidence_from_dict,
    evidence_to_dict,
    grounding_strength,
    render_confidence_line,
    render_evidence_line,
)
from meta_ads_analysis.knowledge_provenance import (
    AUDIT_CONFIRMED,
    AUDIT_CONTRADICTED,
    AUDIT_COULD_NOT,
    AUDIT_INSUFFICIENT,
    AUDIT_REFUTED,
    BAND_EMOJIS,
    FreshSample,
    TIER_NAMES,
    apply_entry_edits,
    audit_claim,
    classify_drift,
    lint,
    lint_profile_baseline,
    lower_band_emoji,
    parse_learnings,
    plan_edits,
    render_report,
    select_auditable,
)
from meta_ads_analysis.early_triage import (
    AdDailyPoint,
    AdHistory,
    DuckDBHistoryProvider,
    OWN_SAMPLE_INSUFFICIENT,
    OWN_SAMPLE_KEEP,
    OWN_SAMPLE_PAUSE,
    classify_own_sample,
    goal_kind,
    group_histories,
    triage_ad,
)
from meta_ads_analysis.meta_api import MetaApiError, MetaMarketingApiClient
from meta_ads_analysis.normalize import ingest_raw_exports
from meta_ads_analysis.review import (
    ReviewResult,
    review_action_plan,
    review_recommendation,
)
from meta_ads_analysis.reporting import render_markdown_report
from meta_ads_analysis.storage import connect, fetch_run_rows, replace_run_rows
from meta_ads_analysis.sync_api import resolve_date_window
from meta_ads_analysis.utils import ensure_dir


def test_ingest_flattens_action_fields_and_preserves_measurement_flags(tmp_path: Path) -> None:
    run_date = "2026-04-21"
    input_dir = tmp_path / "raw" / run_date
    ensure_dir(input_dir)

    _write_csv(
        input_dir / "performance_daily.csv",
        [
            {
                "Day": "2026-04-20",
                "Campaign name": "C1",
                "Ad set name": "AS1",
                "Ad ID": "1001",
                "Ad name": "Creative A",
                "Amount spent (USD)": "$120.00",
                "Impressions": "10000",
                "Reach": "8000",
                "Frequency": "1.25",
                "Clicks (all)": "410",
                "Outbound clicks": "400",
                "Results": "",
                "Actions": '[{"action_type":"offsite_conversion.fb_pixel_purchase","value":"6"}]',
                "Action values": '[{"action_type":"offsite_conversion.fb_pixel_purchase","value":"900"}]',
                "Purchase ROAS (return on ad spend)": '[{"action_type":"offsite_conversion.fb_pixel_purchase","value":"7.5"}]',
                "CTR (all)": "4.1",
                "CPC (cost per link click)": "0.30",
                "CPM (cost per 1,000 impressions)": "12.00",
            },
            {
                "Day": "2026-04-20",
                "Campaign name": "C1",
                "Ad set name": "AS2",
                "Ad ID": "1002",
                "Ad name": "Creative B",
                "Amount spent (USD)": "$80.00",
                "Impressions": "9000",
                "Reach": "7000",
                "Frequency": "1.29",
                "Clicks (all)": "220",
                "Outbound clicks": "220",
                "Purchases": "2",
                "Purchase ROAS": "",
                "App installs": "11",
                "Cost per app install": "7.27",
            },
        ],
    )
    _write_csv(
        input_dir / "video_daily.csv",
        [
            {
                "Day": "2026-04-20",
                "Ad ID": "1001",
                "Ad name": "Creative A",
                "3-second video plays": "3000",
                "ThruPlays": "900",
            }
        ],
    )
    _write_csv(
        input_dir / "creative_lookup.csv",
        [
            {
                "Ad ID": "1001",
                "Media type": "video",
                "Headline (ad settings)": "Buy now",
            }
        ],
    )

    artifacts = ingest_raw_exports(input_dir, run_date)
    assert len(artifacts.normalized_rows) == 2

    creative_a = next(row for row in artifacts.normalized_rows if row["ad_id"] == "1001")
    assert creative_a["account_slug"] is None
    assert creative_a["purchase_count"] == 6
    assert creative_a["purchase_value"] == 900
    assert round(creative_a["purchase_roas"], 2) == 7.5
    assert round(creative_a["spend"], 2) == 120.0
    assert creative_a["clicks"] == 410
    assert round(creative_a["hook_rate"], 2) == 0.30
    assert round(creative_a["hold_rate"], 2) == 0.30
    assert creative_a["creative_type"] == "video"
    assert creative_a["creative_headline"] == "Buy now"

    creative_b = next(row for row in artifacts.normalized_rows if row["ad_id"] == "1002")
    assert creative_b["tracking_confidence"] == "low_results_without_revenue"
    assert creative_b["purchase_count"] == 2
    assert creative_b["purchase_value"] is None
    assert creative_b["app_installs"] == 11
    assert round(creative_b["cost_per_app_install"], 2) == 7.27


def test_report_highlights_waste_fatigue_scaling_and_insufficient_data(tmp_path: Path) -> None:
    run_date = "2026-04-21"
    input_dir = tmp_path / "raw" / run_date
    ensure_dir(input_dir)

    performance_rows: list[dict[str, str]] = []
    video_rows: list[dict[str, str]] = []

    for day in range(1, 15):
        date_string = f"2026-04-{day:02d}"
        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Scale Campaign",
                "Ad set name": "Scale Set",
                "Ad ID": "2001",
                "Ad name": "Scale Winner",
                "Amount spent": "40",
                "Impressions": "10000",
                "Reach": "8500",
                "Frequency": "1.2",
                "Outbound clicks": "400",
                "Purchases": "4",
                "Website purchases conversion value": "320",
                "Purchase ROAS": "8.0",
            }
        )
        video_rows.append(
            {
                "Day": date_string,
                "Ad ID": "2001",
                "Ad name": "Scale Winner",
                "3-second video plays": "3800",
                "ThruPlays": "1500",
            }
        )

        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Waste Campaign",
                "Ad set name": "Waste Set",
                "Ad ID": "2002",
                "Ad name": "Budget Drainer",
                "Amount spent": "30",
                "Impressions": "9000",
                "Reach": "7600",
                "Frequency": "1.4",
                "Outbound clicks": "170",
                "Purchases": "0",
                "Website purchases conversion value": "0",
                "Purchase ROAS": "0",
            }
        )

        recent = day > 7
        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Fatigue Campaign",
                "Ad set name": "Fatigue Set",
                "Ad ID": "2003",
                "Ad name": "Burning Out",
                "Amount spent": "25",
                "Impressions": "8000",
                "Reach": "5000",
                "Frequency": "2.6" if recent else "1.4",
                "Outbound clicks": "120" if recent else "240",
                "Purchases": "1" if recent else "3",
                "Website purchases conversion value": "75" if recent else "210",
                "Purchase ROAS": "3.0" if recent else "8.4",
            }
        )
        video_rows.append(
            {
                "Day": date_string,
                "Ad ID": "2003",
                "Ad name": "Burning Out",
                "3-second video plays": "1100" if recent else "2600",
                "ThruPlays": "250" if recent else "900",
            }
        )

        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Test Campaign",
                "Ad set name": "Test Set",
                "Ad ID": "2004",
                "Ad name": "Tiny Tester",
                "Amount spent": "5",
                "Impressions": "1200",
                "Reach": "1100",
                "Frequency": "1.1",
                "Outbound clicks": "18",
                "Purchases": "0",
                "Website purchases conversion value": "0",
                "Purchase ROAS": "0",
            }
        )

    _write_csv(input_dir / "performance_daily.csv", performance_rows)
    _write_csv(input_dir / "video_daily.csv", video_rows)

    artifacts = ingest_raw_exports(input_dir, run_date)
    db_path = tmp_path / "meta_ads.duckdb"
    with connect(db_path) as con:
        replace_run_rows(con, None, run_date, artifacts.normalized_rows, artifacts.creative_rows)
        rows = fetch_run_rows(con, run_date)

    payload = build_report_payload(rows, run_date)
    markdown = render_markdown_report(payload)

    waste_names = [item["ad_name"] for item in payload["budget_waste"]]
    scaling_names = [item["ad_name"] for item in payload["scaling_candidates"]]
    fatigue_names = [item["ad_name"] for item in payload["fatigue_findings"]]

    assert "Budget Drainer" in waste_names
    assert "Scale Winner" in scaling_names
    assert "Burning Out" in fatigue_names
    assert "Tiny Tester" not in waste_names
    assert "Scale Winner" in markdown
    assert "Budget Drainer" in markdown
    assert "Burning Out" in markdown
    assert payload["tracking_concerns"]


def test_cli_functions_write_outputs(tmp_path: Path, monkeypatch) -> None:
    run_date = "2026-04-21"
    input_dir = tmp_path / "raw" / run_date
    normalized_root = tmp_path / "normalized"
    reports_root = tmp_path / "reports"
    db_path = tmp_path / "meta_ads.duckdb"
    ensure_dir(input_dir)

    _write_csv(
        input_dir / "performance_daily.csv",
        [
            {
                "Day": "2026-04-20",
                "Campaign name": "CLI Campaign",
                "Ad set name": "CLI Set",
                "Ad ID": "9001",
                "Ad name": "CLI Ad",
                "Amount spent": "100",
                "Impressions": "5000",
                "Reach": "4500",
                "Frequency": "1.11",
                "Outbound clicks": "200",
                "Purchases": "3",
                "Website purchases conversion value": "450",
                "Purchase ROAS": "4.5",
            }
        ],
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ingest_meta_exports",
            "--account",
            "Pollen Sense",
            "--run-date",
            run_date,
            "--input-dir",
            str(input_dir),
            "--db-path",
            str(db_path),
            "--normalized-root",
            str(normalized_root),
        ],
    )
    ingest_meta_exports_main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_meta_report",
            "--account",
            "Pollen Sense",
            "--run-date",
            run_date,
            "--db-path",
            str(db_path),
            "--output-dir",
            str(reports_root / run_date),
        ],
    )
    build_meta_report_main()

    assert (normalized_root / "pollen_sense" / run_date / "ad_daily_metrics.csv").exists()
    assert (normalized_root / "pollen_sense" / run_date / "ingestion_summary.json").exists()
    assert (reports_root / run_date / "meta_ads_report.md").exists()
    assert (reports_root / run_date / "meta_ads_report.json").exists()

    summary = json.loads((reports_root / run_date / "meta_ads_report.json").read_text(encoding="utf-8"))
    assert summary["account_summary"]["ad_count"] == 1
    assert summary["account_slug"] == "pollen_sense"


def test_report_uses_app_installs_as_secondary_signal_when_results_are_sparse(tmp_path: Path) -> None:
    run_date = "2026-04-21"
    input_dir = tmp_path / "raw" / run_date
    ensure_dir(input_dir)

    performance_rows: list[dict[str, str]] = []
    for day in range(1, 9):
        date_string = f"2026-04-{day:02d}"
        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Install Campaign",
                "Ad set name": "Install Set",
                "Ad ID": "3001",
                "Ad name": "Install Leader",
                "Amount spent": "15",
                "Impressions": "4000",
                "Reach": "3200",
                "Frequency": "1.2",
                "Outbound clicks": "90",
                "Results": "",
                "App installs": "5",
                "Cost per app install": "3.0",
            }
        )
        performance_rows.append(
            {
                "Day": date_string,
                "Campaign name": "Install Campaign",
                "Ad set name": "Install Set",
                "Ad ID": "3002",
                "Ad name": "Install Laggard",
                "Amount spent": "20",
                "Impressions": "4500",
                "Reach": "3600",
                "Frequency": "1.3",
                "Outbound clicks": "70",
                "Results": "",
                "App installs": "1",
                "Cost per app install": "20.0",
            }
        )

    _write_csv(input_dir / "performance_daily.csv", performance_rows)
    artifacts = ingest_raw_exports(input_dir, run_date)

    payload = build_report_payload(artifacts.normalized_rows, run_date)
    waste_names = [item["ad_name"] for item in payload["budget_waste"]]

    assert "Install Laggard" in waste_names
    assert payload["account_summary"]["total_app_installs"] == 48.0


def test_report_builds_multi_window_trajectory_from_daily_rows() -> None:
    run_date = "2026-04-30"
    rows: list[dict[str, object]] = []
    start = date(2026, 4, 1)
    for index in range(30):
        report_date = start + timedelta(days=index)
        is_recent_week = index >= 23
        rows.append(
            _daily_metric_row(
                report_date,
                ad_id="5001",
                ad_name="Improving Ad",
                spend=10.0,
                app_installs=5.0 if is_recent_week else 1.0,
            )
        )
        rows.append(
            _daily_metric_row(
                report_date,
                ad_id="5002",
                ad_name="Degrading Ad",
                spend=10.0,
                app_installs=1.0 if is_recent_week else 5.0,
            )
        )

    payload = build_report_payload(rows, run_date)
    by_name = {item["ad_name"]: item for item in payload["ad_window_summaries"]}

    assert payload["window_comparison_meta"]["window_end"] == "2026-04-30"
    assert payload["account_window_summary"]["30d"]["days_with_data"] == 30
    assert by_name["Improving Ad"]["trajectory"]["seven_vs_thirty"]["status"] == "improving"
    assert by_name["Degrading Ad"]["trajectory"]["seven_vs_thirty"]["status"] == "degrading"
    assert by_name["Improving Ad"]["trajectory"]["three_vs_seven"]["status"] == "insufficient_data"

    rendered = render_markdown_report(payload)
    assert "## Performance By Window" in rendered
    assert "## Trajectory Highlights" in rendered


def test_report_marks_clipped_window_coverage() -> None:
    run_date = "2026-04-10"
    rows = [
        _daily_metric_row(
            date(2026, 4, 1) + timedelta(days=index),
            ad_id="6001",
            ad_name="Short Coverage Ad",
            spend=10.0,
            app_installs=1.0,
        )
        for index in range(10)
    ]

    payload = build_report_payload(rows, run_date)
    coverage = payload["window_comparison_meta"]["coverage"]["30d"]

    assert coverage["days_with_data"] == 10
    assert coverage["coverage_note"] == "10 of 30 requested days had exported rows."


def test_storage_keeps_same_run_date_separate_by_account_slug(tmp_path: Path) -> None:
    db_path = tmp_path / "meta_ads.duckdb"
    run_date = "2026-04-21"

    pollen_rows = [
        {
            "ingestion_run_date": run_date,
            "account_slug": "pollen_sense",
            "source_run_path": "raw/pollen_sense/2026-04-21",
            "report_date": "2026-04-21",
            "account_id": "",
            "account_name": "",
            "campaign_id": "1",
            "campaign_name": "Campaign",
            "adset_id": "1",
            "adset_name": "Set",
            "ad_id": "1",
            "ad_name": "Pollen Ad",
            "objective": "",
            "spend": 10.0,
            "impressions": 100,
            "reach": 90,
            "frequency": 1.1,
            "clicks": 5,
            "link_clicks": 5,
            "outbound_clicks": 5,
            "ctr": 0.05,
            "cpc": 2.0,
            "cpm": 100.0,
            "results": 1.0,
            "result_label": "In-app subscriptions",
            "cost_per_result": 10.0,
            "app_installs": 2.0,
            "cost_per_app_install": 5.0,
            "purchase_count": 0.0,
            "purchase_value": 0.0,
            "purchase_roas": None,
            "video_3s_plays": None,
            "thruplays": None,
            "hook_rate": None,
            "hold_rate": None,
            "average_order_value": None,
            "creative_type": None,
            "creative_copy": None,
            "creative_headline": None,
            "launch_date": None,
            "preview_link": None,
            "post_link": None,
            "has_video_metrics": False,
            "tracking_confidence": "low_results_without_revenue",
        }
    ]
    divine_rows = [
        {
            **pollen_rows[0],
            "account_slug": "divine_designs",
            "source_run_path": "raw/divine_designs/2026-04-21",
            "ad_id": "2",
            "ad_name": "Divine Ad",
        }
    ]

    with connect(db_path) as con:
        replace_run_rows(con, "pollen_sense", run_date, pollen_rows, [])
        replace_run_rows(con, "divine_designs", run_date, divine_rows, [])

        pollen = fetch_run_rows(con, run_date, "pollen_sense")
        divine = fetch_run_rows(con, run_date, "divine_designs")

    assert [row["ad_name"] for row in pollen] == ["Pollen Ad"]
    assert [row["ad_name"] for row in divine] == ["Divine Ad"]


def test_account_registry_resolves_valid_slug(tmp_path: Path) -> None:
    config_path = tmp_path / "meta_ads_accounts.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "Pollen Sense",
                        "account_name": "Pollen Sense",
                        "ad_account_id": "12345",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    accounts = load_account_registry(config_path)
    account = resolve_account("pollen_sense", config_path)

    assert "pollen_sense" in accounts
    assert account.ad_account_id == "act_12345"
    # No action_policy at all → the new field parses to None, not a raise.
    assert account.max_budget_decrease_percent is None


def test_account_registry_max_budget_decrease_percent_override(tmp_path: Path) -> None:
    config_path = tmp_path / "meta_ads_accounts.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "test_account",
                        "account_name": "Test Account",
                        "ad_account_id": "99999",
                        "action_policy": {
                            "max_budget_decrease_percent": 25,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    accounts = load_account_registry(config_path)

    assert accounts["test_account"].max_budget_decrease_percent == 25.0


def test_account_registry_max_budget_decrease_percent_defaults_absent(tmp_path: Path) -> None:
    config_path = tmp_path / "meta_ads_accounts.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "test_account",
                        "account_name": "Test Account",
                        "ad_account_id": "99999",
                        "action_policy": {
                            "max_budget_increase_percent": 20,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    accounts = load_account_registry(config_path)

    assert accounts["test_account"].max_budget_decrease_percent is None


def test_account_registry_existing_config_loads() -> None:
    from meta_ads_analysis.config import DEFAULT_ACCOUNTS_CONFIG_PATH

    accounts = load_account_registry(DEFAULT_ACCOUNTS_CONFIG_PATH)

    assert "pollen_sense" in accounts
    assert "divine_designs" in accounts
    assert accounts["pollen_sense"].max_budget_decrease_percent is None
    assert accounts["divine_designs"].max_budget_decrease_percent is None


def test_default_date_window_uses_trailing_30_days() -> None:
    date_from, date_to = resolve_date_window(date(2026, 4, 22))
    assert date_from == "2026-03-24"
    assert date_to == "2026-04-22"


def test_meta_api_client_paginates() -> None:
    first = Mock()
    first.status_code = 200
    first.json.return_value = {
        "data": [{"id": "1"}],
        "paging": {"next": "https://example.com/page-2"},
    }
    second = Mock()
    second.status_code = 200
    second.json.return_value = {
        "data": [{"id": "2"}],
        "paging": {},
    }
    session = Mock()
    session.get.side_effect = [first, second]

    client = MetaMarketingApiClient("token", session=session)
    rows = list(client.iter_paginated("/act_123/insights", params={"fields": "ad_id"}))

    assert rows == [{"id": "1"}, {"id": "2"}]
    assert session.get.call_count == 2


def test_meta_api_client_raises_operator_friendly_error() -> None:
    response = Mock()
    response.status_code = 400
    response.json.return_value = {"error": {"message": "Bad token", "code": 190}}
    response.text = "Bad token"
    session = Mock()
    session.get.return_value = response

    client = MetaMarketingApiClient("token", session=session)

    try:
        list(client.iter_paginated("/act_123/insights", params={"fields": "ad_id"}))
    except MetaApiError as exc:
        assert "Bad token" in str(exc)
    else:
        raise AssertionError("Expected MetaApiError")


def test_sync_api_full_pipeline_with_mocked_client(tmp_path: Path, monkeypatch) -> None:
    raw_root = tmp_path / "raw"
    normalized_root = tmp_path / "normalized"
    reports_root = tmp_path / "reports"
    db_path = tmp_path / "meta_ads.duckdb"
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "pollen_sense",
                        "account_name": "Pollen Sense",
                        "ad_account_id": "act_12345",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_insights(self, *args, **kwargs) -> list[dict[str, object]]:
            return [
                {
                    "account_id": "act_12345",
                    "account_name": "Pollen Sense",
                    "campaign_id": "200",
                    "campaign_name": "Subscriptions",
                    "adset_id": "300",
                    "adset_name": "Spring",
                    "ad_id": "400",
                    "ad_name": "API Ad",
                    "date_start": "2026-04-22",
                    "date_stop": "2026-04-22",
                    "reach": "1000",
                    "impressions": "1200",
                    "frequency": "1.2",
                    "clicks": "50",
                    "ctr": "4.2",
                    "cpc": "1.1",
                    "cpm": "45.0",
                    "spend": "55.0",
                    "objective": "APP_INSTALLS",
                    "actions": [
                        {"action_type": "app_custom_event.fb_mobile_subscribe", "value": "2"},
                        {"action_type": "mobile_app_install", "value": "10"},
                    ],
                    "cost_per_action_type": [
                        {"action_type": "app_custom_event.fb_mobile_subscribe", "value": "27.5"},
                        {"action_type": "mobile_app_install", "value": "5.5"},
                    ],
                    "video_3_sec_watched_actions": [
                        {"action_type": "video_view", "value": "300"}
                    ],
                    "video_thruplay_watched_actions": [
                        {"action_type": "video_thruplay_watched_actions", "value": "90"}
                    ],
                }
            ]

        def fetch_ads(self, *args, **kwargs) -> list[dict[str, object]]:
            return [
                {
                    "id": "400",
                    "name": "API Ad",
                    "created_time": "2026-04-01T00:00:00+0000",
                    "creative": {
                        "object_story_spec": {
                            "video_data": {
                                "message": "Primary text",
                                "title": "Headline",
                            }
                        },
                        "effective_object_story_id": "123_456",
                    },
                }
            ]

    monkeypatch.setenv("META_ACCESS_TOKEN", "token")
    monkeypatch.setattr("meta_ads_analysis.cli.DEFAULT_RAW_ROOT", raw_root)
    monkeypatch.setattr("meta_ads_analysis.cli.DEFAULT_NORMALIZED_ROOT", normalized_root)
    monkeypatch.setattr("meta_ads_analysis.cli.DEFAULT_REPORTS_ROOT", reports_root)
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_RAW_ROOT", raw_root)
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_NORMALIZED_ROOT", normalized_root)
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_REPORTS_ROOT", reports_root)
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    monkeypatch.setattr("meta_ads_analysis.sync_api.MetaMarketingApiClient", FakeClient)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sync_meta_api",
            "--account",
            "pollen_sense",
            "--run-date",
            "2026-04-22",
            "--db-path",
            str(db_path),
        ],
    )
    sync_meta_api_main()

    assert (raw_root / "pollen_sense" / "2026-04-22" / "performance_daily.csv").exists()
    assert (normalized_root / "pollen_sense" / "2026-04-22" / "ad_daily_metrics.csv").exists()
    assert (reports_root / "pollen_sense" / "2026-04-22" / "meta_ads_report.json").exists()
    summary = json.loads(
        (raw_root / "pollen_sense" / "2026-04-22" / "api_sync_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["completed_full_pipeline"] is True


def test_sync_api_raw_only_writes_raw_files(tmp_path: Path, monkeypatch) -> None:
    raw_root = tmp_path / "raw"
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "divine_designs",
                        "account_name": "Divine Designs",
                        "ad_account_id": "act_555",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_insights(self, *args, **kwargs) -> list[dict[str, object]]:
            return []

        def fetch_ads(self, *args, **kwargs) -> list[dict[str, object]]:
            return []

    monkeypatch.setenv("META_ACCESS_TOKEN", "token")
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_RAW_ROOT", raw_root)
    monkeypatch.setattr("meta_ads_analysis.sync_api.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    monkeypatch.setattr("meta_ads_analysis.sync_api.MetaMarketingApiClient", FakeClient)

    from meta_ads_analysis.sync_api import sync_account_from_api, write_api_sync_summary

    artifacts = sync_account_from_api(account_slug="divine_designs", run_date="2026-04-22")
    summary_path = write_api_sync_summary(artifacts, completed_full_pipeline=False)

    assert (raw_root / "divine_designs" / "2026-04-22" / "performance_daily.csv").exists()
    assert summary_path.exists()


def test_action_plan_proposes_approved_pause_path_for_high_waste_ad() -> None:
    payload = {
        "account_slug": "pollen_sense",
        "run_date": "2026-05-04",
        "budget_waste": [
            {
                "ad_id": "123",
                "ad_name": "Waste Ad",
                "campaign_name": "Campaign",
                "adset_name": "Ad Set",
                "total_spend": 250.0,
                "total_results": 0.0,
                "total_app_installs": 1.0,
                "waste_score": 82.0,
                "waste_status": "high",
                "waste_reasons": ["spent without results"],
                "tracking_confidence": "medium_roas_unavailable",
            }
        ],
        "fatigue_findings": [],
        "scaling_candidates": [],
        "tracking_concerns": ["ROAS is not fully reliable."],
    }

    plan = build_action_plan(payload)

    pause = next(action for action in plan["actions"] if action["action_type"] == "pause_ad")
    assert pause["status"] == "proposed"
    assert pause["executable"] is True
    assert pause["approval_required"] is True
    assert pause["params"] == {"status": "paused"}
    assert "Advantage+ creative enhancements" in plan["guardrails"]["meta_ai_features"]["keep_disabled"]


def test_action_plan_uses_pollen_policy_for_medium_waste_pause(tmp_path: Path, monkeypatch) -> None:
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "pollen_sense",
                        "account_name": "Pollen Sense",
                        "ad_account_id": "12345",
                        "action_policy": {
                            "primary_goal": "maximize_in_app_subscriptions",
                            "pause_if_no_primary_and_secondary_cost_above": 3.0,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    payload = {
        "account_slug": "pollen_sense",
        "run_date": "2026-06-16",
        "budget_waste": [
            {
                "ad_id": "install-expensive",
                "ad_name": "Install Expensive",
                "total_spend": 120.0,
                "total_results": 0.0,
                "total_app_installs": 10.0,
                "cost_per_app_install": 12.0,
                "waste_score": 50.0,
                "waste_status": "medium",
                "waste_reasons": ["install fallback is expensive"],
            }
        ],
        "fatigue_findings": [],
        "scaling_candidates": [],
        "tracking_concerns": [],
    }

    plan = build_action_plan(payload)

    pause = next(action for action in plan["actions"] if action["action_type"] == "pause_ad")
    assert pause["target"]["id"] == "install-expensive"
    assert "app-install fallback" in pause["rationale"]


def test_action_plan_builds_divine_budget_increase_candidate(tmp_path: Path, monkeypatch) -> None:
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "divine_designs",
                        "account_name": "Divine Designs",
                        "ad_account_id": "act_555",
                        "action_policy": {
                            "primary_goal": "roas",
                            "scale_roas_floor": 3.0,
                            "max_budget_increase_percent": 20,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    payload = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-16",
        "budget_waste": [],
        "fatigue_findings": [],
        "scaling_candidates": [
            {
                "ad_id": "ad-1",
                "ad_name": "Scale Ad",
                "campaign_id": "campaign-1",
                "campaign_name": "Campaign",
                "adset_id": "adset-1",
                "adset_name": "Ad Set",
                "scaling_candidate": True,
                "scaling_score": 80.0,
                "total_spend": 500.0,
                "total_results": 50.0,
                "cost_per_result": 10.0,
                "blended_roas": 3.5,
            }
        ],
        "tracking_concerns": [],
    }

    plan = build_action_plan(payload)

    budget = next(action for action in plan["actions"] if action["action_type"] == "increase_adset_budget")
    assert budget["target"]["id"] == "adset-1"
    assert budget["params"]["max_increase_percent"] == 20
    assert budget["params"]["new_daily_budget_cents"] is None
    assert budget["executable"] is False


def test_report_to_action_plan_preserves_adset_id_for_divine_scale_policy(tmp_path: Path, monkeypatch) -> None:
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "divine_designs",
                        "account_name": "Divine Designs",
                        "ad_account_id": "act_555",
                        "action_policy": {
                            "primary_goal": "roas",
                            "scale_roas_floor": 3.0,
                            "max_budget_increase_percent": 20,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)
    rows = [
        {
            "report_date": date(2026, 6, 1),
            "campaign_id": "campaign-1",
            "campaign_name": "Scale Campaign",
            "adset_id": "adset-winner",
            "adset_name": "Scale Set",
            "ad_id": "winner",
            "ad_name": "Scale Winner",
            "creative_type": "Video",
            "spend": 100.0,
            "purchase_value": 400.0,
            "purchase_count": 20.0,
            "results": 20.0,
            "result_label": "Website purchases",
            "app_installs": 0.0,
            "impressions": 10000,
            "outbound_clicks": 500,
            "frequency": 1.1,
            "video_3s_plays": 4500,
            "thruplays": 2000,
            "has_video_metrics": True,
            "tracking_confidence": "high",
        },
        {
            "report_date": date(2026, 6, 1),
            "campaign_id": "campaign-1",
            "campaign_name": "Scale Campaign",
            "adset_id": "adset-laggard",
            "adset_name": "Laggard Set",
            "ad_id": "laggard",
            "ad_name": "Laggard",
            "creative_type": "Video",
            "spend": 100.0,
            "purchase_value": 100.0,
            "purchase_count": 5.0,
            "results": 5.0,
            "result_label": "Website purchases",
            "app_installs": 0.0,
            "impressions": 10000,
            "outbound_clicks": 250,
            "frequency": 1.1,
            "video_3s_plays": 2500,
            "thruplays": 800,
            "has_video_metrics": True,
            "tracking_confidence": "high",
        },
    ]

    report = build_report_payload(rows, "2026-06-16")
    report["account_slug"] = "divine_designs"
    plan = build_action_plan(report)

    budget = next(action for action in plan["actions"] if action["action_type"] == "increase_adset_budget")
    assert budget["target"]["id"] == "adset-winner"
    assert budget["target"]["source_ad_id"] == "winner"


def test_operator_brief_separates_review_manual_and_meta_ai_followups() -> None:
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-16",
        "account_action_policy": {
            "primary_goal": "roas",
            "target_roas": 3.0,
        },
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"type": "ad", "id": "ad-1", "name": "Bad Ad"},
                "params": {"status": "paused"},
                "rationale": "ROAS is below the account floor.",
            },
            {
                "action_id": "increase_adset_budget_1",
                "action_type": "increase_adset_budget",
                "status": "approved",
                "executable": True,
                "target": {"type": "adset", "id": "adset-1", "name": "Scale Set"},
                "params": {
                    "current_daily_budget_cents": 10000,
                    "new_daily_budget_cents": 12000,
                    "max_increase_percent": 20,
                },
                "rationale": "ROAS meets the scale floor.",
            },
            {
                "action_id": "disable_meta_ai_controls_adset-2",
                "action_type": "disable_meta_ai_controls",
                "status": "proposed",
                "executable": False,
                "target": {"type": "adset", "id": "adset-2", "name": "Automated Set"},
                "params": {},
                "rationale": "Advantage controls were detected.",
            },
        ],
    }
    previous_plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-05-04",
        "actions": [
            {
                "action_id": "pause_ad_old",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
            }
        ],
    }
    report = {
        "run_date": "2026-06-16",
        "account_summary": {
            "total_spend": 200.0,
            "total_results": 25.0,
            "total_app_installs": 0.0,
            "blended_roas": 2.5,
        },
    }
    previous_report = {
        "run_date": "2026-05-04",
        "account_summary": {
            "total_spend": 150.0,
            "total_results": 10.0,
            "total_app_installs": 0.0,
            "blended_roas": 1.5,
        },
    }

    brief = build_operator_brief(
        plan=plan,
        report=report,
        previous_plan=previous_plan,
        previous_report=previous_report,
    )
    markdown = render_operator_brief(brief)

    assert brief["summary"]["approved_executable_count"] == 1
    assert brief["ready_for_review"][0]["action_id"] == "pause_ad_1"
    assert brief["approved_to_execute"][0]["action_id"] == "increase_adset_budget_1"
    assert brief["meta_ai_followups"][0]["action_id"] == "disable_meta_ai_controls_adset-2"
    assert "Optimize toward 3 blended ROAS or better." in markdown
    assert "Spend change: +50.00" in markdown


def test_operator_brief_moves_failed_live_lookup_to_do_not_touch() -> None:
    plan = {
        "account_slug": "pollen_sense",
        "run_date": "2026-05-04",
        "account_action_policy": {
            "primary_goal": "maximize_in_app_subscriptions",
            "secondary_cost_per_app_install_target": 3.0,
        },
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"type": "ad", "id": "ad-1", "name": "Needs Live Check"},
                "params": {"status": "paused"},
                "rationale": "Waste risk.",
                "live_state": {"lookup_status": "failed", "error": "network unavailable"},
            },
            {
                "action_id": "refresh_creative_1",
                "action_type": "refresh_creative",
                "status": "proposed",
                "executable": False,
                "target": {"type": "ad", "id": "ad-2", "name": "Tired Creative"},
                "params": {},
                "rationale": "Creative fatigue.",
                "live_state": {"lookup_status": "failed", "error": "network unavailable"},
            }
        ],
    }

    brief = build_operator_brief(plan=plan)

    assert brief["ready_for_review"] == []
    assert brief["do_not_touch_yet"][0]["action_id"] == "pause_ad_1"
    assert brief["needs_human_judgment"][0]["action_id"] == "refresh_creative_1"


def _evidence_for_brief(*, purchases: float | None, spend: float | None) -> Evidence:
    """A populated, recent Evidence for a divine_designs ad — drives a reproducible re-check line."""
    return Evidence(
        metric_name="blended_roas",
        metric_value=1.20,
        metric_display="ROAS 1.20",
        window="2026-06-10..2026-06-24",
        sample_purchases=purchases,
        sample_spend=spend,
        entity_level="ad",
        entity_id="123",
        entity_name="Cody - Copy",
        regenerating_query=build_regenerating_query("divine_designs", "ad", "2026-06-10", "2026-06-24"),
    )


def _action_with_confidence(
    *,
    action_id: str,
    status: str,
    executable: bool,
    rationale: str,
    evidence: Evidence,
    confidence,
) -> dict:
    return {
        "action_id": action_id,
        "action_type": "pause_ad",
        "status": status,
        "executable": executable,
        "target": {"type": "ad", "id": "123", "name": "Cody - Copy"},
        "params": {"status": "paused"},
        "rationale": rationale,
        "evidence": evidence_to_dict(evidence),
        "confidence": confidence_to_dict(confidence),
    }


def test_operator_brief_renders_high_confidence_evidence_and_recheck_line() -> None:
    # Parent use case: an auditor can re-run the named query to confirm the number. A high-confidence
    # pause must surface the band, the four evidence facts, and the exact account_metrics command.
    evidence = _evidence_for_brief(purchases=120.0, spend=2400.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    assert confidence_to_dict(confidence)["band"] == "high"  # precondition
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _action_with_confidence(
                action_id="pause_ad_123",
                status="approved",
                executable=True,
                rationale="High waste risk: ROAS well below the account floor.",
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    brief = build_operator_brief(plan=plan)
    markdown = render_operator_brief(brief)

    # Evidence + confidence carried through to the JSON (additive).
    carried = brief["approved_to_execute"][0]
    assert carried["confidence"]["band"] == "high"
    assert carried["evidence"]["metric_display"] == "ROAS 1.20"

    # Band (one vocabulary), the four evidence facts, and the re-check command all render.
    assert "🟢 High (~80–100%)" in markdown
    assert "ROAS 1.20" in markdown                       # the number
    assert "2026-06-10..2026-06-24" in markdown          # the time window
    assert "120 conversions" in markdown                 # the sample size
    assert "ad:123 'Cody - Copy'" in markdown            # which ad
    assert (
        "Re-check: account_metrics --account divine_designs --level ad "
        "--date-from 2026-06-10 --date-to 2026-06-24"
    ) in markdown
    assert "Would raise:" in markdown and "Would lower:" in markdown
    # Goal-neutral wording: the assess() would_raise hint reads "conversions", never "purchases",
    # so an install account (which never has purchases) is not told to get "more purchases".
    assert "more conversions" in markdown and "more purchases" not in markdown


def test_operator_brief_abstain_action_reads_as_keep_running_not_a_percentage() -> None:
    # An abstain must read as a promising test ("keep running"), be visually distinct (⚪, not 🔴),
    # and never show a percentage.
    evidence = _evidence_for_brief(purchases=2.0, spend=40.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    assert confidence_to_dict(confidence)["band"] == "abstain"  # precondition
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _action_with_confidence(
                action_id="pause_ad_thin",
                status="proposed",
                executable=False,
                rationale="Treat as a promising test: keep running and re-check.",
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    markdown = render_operator_brief(build_operator_brief(plan=plan))

    assert "⚪ Insufficient data — keep running" in markdown
    assert "🔴 Low" not in markdown
    # No band percentage at all for an abstain (no range token, no precise score).
    assert "%" not in markdown


def test_operator_brief_causal_flag_action_offers_an_ab_experiment() -> None:
    # A correlational claim that asserts cause must carry the visible caveat and the offer to file an
    # experiment to confirm it (the brief surfaces the offer in text; it does not auto-file).
    evidence = _evidence_for_brief(purchases=120.0, spend=2400.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.correlational,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
        causal_text="ROAS is low because this creative drives wasted spend.",
    )
    assert confidence_to_dict(confidence)["causal_flag"] is True  # precondition
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _action_with_confidence(
                action_id="refresh_creative_123",
                status="proposed",
                executable=True,
                rationale="Creative appears to drive waste.",
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    markdown = render_operator_brief(build_operator_brief(plan=plan))

    assert "correlational — confirm via A/B" in markdown
    assert "experiment define" in markdown


def test_operator_brief_never_prints_false_precision_or_none() -> None:
    # The band range "~80–100%" is allowed, but no two-significant-figure precise score (e.g. 82.4%).
    evidence = _evidence_for_brief(purchases=120.0, spend=2400.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _action_with_confidence(
                action_id="pause_ad_123",
                status="approved",
                executable=True,
                rationale="High waste risk.",
                evidence=evidence,
                confidence=confidence,
            ),
            # An action carrying no evidence/confidence must render gracefully (no block, no "None").
            {
                "action_id": "measurement_review_0",
                "action_type": "measurement_review",
                "status": "proposed",
                "executable": False,
                "target": {"type": "account", "id": "acct"},
                "params": {},
                "rationale": "Tracking looks off; verify the pixel.",
                "evidence": {},
            },
        ],
    }

    markdown = render_operator_brief(build_operator_brief(plan=plan))

    assert "~80–100%" in markdown                          # the range is fine
    assert re.search(r"\d{1,3}\.\d+%", markdown) is None    # but no precise percent score
    # The no-evidence action renders its bullet with no block — never "Evidence/Confidence: None".
    assert "measurement_review_0" in markdown
    assert "Evidence: None" not in markdown and "Confidence: None" not in markdown


def test_operator_brief_evidence_without_regen_omits_recheck_line() -> None:
    # The re-check command can be absent (build_regenerating_query returns None when the account /
    # level / window cannot be determined). The Evidence facts must still render, with no orphan
    # "Re-check:" line.
    evidence = Evidence(
        metric_name="blended_roas",
        metric_value=1.20,
        metric_display="ROAS 1.20",
        window="2026-06-10..2026-06-24",
        sample_purchases=120.0,
        sample_spend=2400.0,
        entity_level="ad",
        entity_id="123",
        entity_name="Cody - Copy",
        regenerating_query=None,
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _action_with_confidence(
                action_id="pause_ad_123",
                status="approved",
                executable=True,
                rationale="High waste risk.",
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    markdown = render_operator_brief(build_operator_brief(plan=plan))

    assert "ROAS 1.20" in markdown          # facts still render
    assert "🟢 High" in markdown            # band still renders
    assert "Re-check:" not in markdown      # no orphan re-check line when no query exists


def test_operator_brief_confidence_without_evidence_renders_band_only() -> None:
    # A confidence block can arrive without an evidence block; the band line must still render (no
    # Evidence / Re-check lines, no "None").
    evidence = _evidence_for_brief(purchases=120.0, spend=2400.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    action = _action_with_confidence(
        action_id="pause_ad_123",
        status="approved",
        executable=True,
        rationale="High waste risk.",
        evidence=evidence,
        confidence=confidence,
    )
    action["evidence"] = {}  # confidence present, evidence absent
    plan = {"account_slug": "divine_designs", "run_date": "2026-06-24", "actions": [action]}

    markdown = render_operator_brief(build_operator_brief(plan=plan))

    assert "🟢 High" in markdown
    assert "Evidence:" not in markdown
    assert "Re-check:" not in markdown
    assert "Evidence: None" not in markdown


# ---------------------------------------------------------------------------
# Adversarial review gate (review.py)
# ---------------------------------------------------------------------------


def _review_evidence(
    *,
    window: str,
    purchases: float | None,
    spend: float | None,
    metric_value: float | None = 2.0,
    metric_name: str = "blended_roas",
) -> Evidence:
    return Evidence(
        metric_name=metric_name,
        metric_value=metric_value,
        metric_display=f"ROAS {metric_value:.2f}" if metric_value is not None else "ROAS n/a",
        window=window,
        sample_purchases=purchases,
        sample_spend=spend,
        entity_level="ad",
        entity_id="123",
        entity_name="Cody - Copy",
        regenerating_query=None,
    )


def test_review_below_floor_returns_insufficient() -> None:
    # Parent use case: the "9-purchase winner" must never reach the brief as a confident call.
    evidence = _review_evidence(window="2026-06-19..2026-06-24", purchases=9.0, spend=40.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.correlational,
        spend_floor=75.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": "consider_scale_budget"},
        policy={},
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "insufficient"
    assert "sample_floor" in result.failed_inputs
    assert any("floor" in reason for reason in result.reasons)
    # Goal-neutral wording: the below-floor reason reads "conversions", never "purchases", so the
    # review gate matches the action plan for install accounts that never generate purchases.
    sample_reason = next(r for r in result.reasons if "floor" in r)
    assert "conversions" in sample_reason and "purchases" not in sample_reason


def test_review_short_window_downgrades() -> None:
    # ROAS 1.1 over a 3-day window: the sample clears the floor but the window may be unrepresentative.
    evidence = _review_evidence(
        window="2026-06-21..2026-06-24", purchases=30.0, spend=200.0, metric_value=1.1
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": "pause_ad"},
        policy={},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "downgrade"
    assert "window_length" in result.failed_inputs
    assert Band[result.revised_band] < Band[result.original_band]
    assert any("unrepresentative" in reason for reason in result.reasons)


def test_review_causal_correlational_downgrades() -> None:
    # A causal claim from correlational data whose band was (hand-)inflated above the causal cap.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=4.0
    )
    confidence = {
        "band": "high",
        "data_band": "high",
        "grounding_band": "medium",
        "grounding_tier": "correlational",
        "factors": [],
        "would_raise": "",
        "would_lower": "",
        "causal_flag": True,
    }
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence,
        action={"action_type": "consider_scale_budget"},
        policy={},
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "downgrade"
    assert "causal" in result.failed_inputs
    assert Band[result.revised_band] < Band.high
    assert any("A/B" in reason for reason in result.reasons)


def test_review_band_earned_downgrades() -> None:
    # The claimed band is stronger than confidence.assess recomputes from the same evidence.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=30.0, spend=200.0, metric_value=1.0
    )
    confidence = confidence_to_dict(
        assess(
            evidence=evidence,
            tier=EvidenceTier.direct_observation,
            spend_floor=100.0,
            conversions_floor=25.0,
            recency_days=1,
        )
    )
    assert confidence["band"] == "medium"  # precondition: the rubric supports medium
    confidence["band"] = "high"  # ...but the stored band drifted up to high

    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence,
        action={"action_type": "pause_ad"},
        policy={},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "downgrade"
    assert "band_earned" in result.failed_inputs
    assert result.revised_band == "medium"
    assert Band[result.revised_band] < Band.high


def test_review_external_caps_at_low() -> None:
    # External evidence is a hypothesis: a live call grounded in it cannot read above low.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=3.0
    )
    confidence = {
        "band": "medium",
        "data_band": "medium",
        "grounding_band": "low",
        "grounding_tier": "external",
        "factors": [],
        "would_raise": "",
        "would_lower": "",
        "causal_flag": False,
    }
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence,
        action={"action_type": "consider_scale_budget"},
        policy={},
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "downgrade"
    assert "external" in result.failed_inputs
    assert result.revised_band == "low"
    assert any("experiment define" in reason for reason in result.reasons)


def test_review_direction_contradiction_refutes() -> None:
    # Pausing an ad whose cited ROAS is well above the account target contradicts its own number.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=6.0
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": "pause_ad"},
        policy={"primary_goal": "roas", "target_roas": 3.0},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "refuted"
    assert "direction" in result.failed_inputs
    assert result.revised_band is None  # refuted carries no corrected band


def test_review_clean_call_stands() -> None:
    # A pause with a large sample, a long recent window, and a direct-observation tier survives.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=1.0
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": "pause_ad"},
        policy={},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert isinstance(result, ReviewResult)
    assert result.verdict == "stands"
    assert result.failed_inputs == []
    assert result.revised_band is None
    assert result.original_band == confidence_to_dict(confidence)["band"]


def test_review_causal_ab_experiment_is_never_downgraded() -> None:
    # An A/B experiment IS the causal evidence — a causal claim grounded in it must NOT be downgraded
    # by the causal guard (the exemption the producer's grounding_strength encodes). Locks the
    # tier != ab_experiment guard in check 3.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=4.0
    )
    confidence = {
        "band": "high",
        "data_band": "high",
        "grounding_band": "high",
        "grounding_tier": "ab_experiment",
        "factors": [],
        "would_raise": "",
        "would_lower": "",
        "causal_flag": True,
    }
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence,
        action={"action_type": "consider_scale_budget"},
        policy={},
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "stands"
    assert "causal" not in result.failed_inputs


def test_review_scale_below_target_refutes() -> None:
    # The mirror of the pause-a-winner case: scaling an entity whose cited ROAS is below the account
    # target contradicts its own number.
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=1.5
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=75.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": "increase_adset_budget"},
        policy={"primary_goal": "roas", "target_roas": 3.0},
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "refuted"
    assert "direction" in result.failed_inputs
    assert any("below the 3 target" in reason for reason in result.reasons)


def _install_direction_result(
    action_type: str, cost: float, *, target: float | None = 3.0
):
    # Run one install-goal recommendation through the gate on a clean, above-floor sample so only the
    # cost-polarity `direction` check can fire. The policy carries the install target unless `target`
    # is None (the missing-target guard case).
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24",
        purchases=120.0,
        spend=2400.0,
        metric_value=cost,
        metric_name="cost_per_app_install",
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=75.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    policy = {"primary_goal": "maximize_in_app_subscriptions"}
    if target is not None:
        policy["secondary_cost_per_app_install_target"] = target
    return review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence_to_dict(confidence),
        action={"action_type": action_type},
        policy=policy,
        spend_floor=75.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )


def test_review_install_scale_above_target_refutes() -> None:
    # Install goal (cost-per-install, lower-is-better): scaling an entity whose cited cost/install
    # ($5) sits above the $3 target is scaling a loser — the polarity mirror of a ROAS scale below
    # target. Refuted (a warning, not a band-cap).
    result = _install_direction_result("consider_scale_budget", 5.0)
    assert result.verdict == "refuted"
    assert "direction" in result.failed_inputs
    assert any("above the $3 target" in reason for reason in result.reasons)
    assert any("scaling" in reason for reason in result.reasons)
    assert result.revised_band is None  # refuted carries no corrected band


def test_review_install_pause_below_target_refutes() -> None:
    # Pausing an entity whose cost/install ($1.50) is comfortably below the $3 target (<= 3/1.5 = $2)
    # is killing a winner — the mirror of pausing a ROAS winner.
    result = _install_direction_result("pause_ad", 1.5)
    assert result.verdict == "refuted"
    assert "direction" in result.failed_inputs
    assert any("comfortably below the $3 target" in reason for reason in result.reasons)


def test_review_install_budget_cut_below_target_refutes_at_margin_boundary() -> None:
    # Boundary pin: cost/install ($2.00) sits EXACTLY at the inverted margin (3 / 1.5 = 2.0). The
    # check uses an inclusive `<=`, so a cut at the boundary IS refuted — guards against a future
    # `<` slip silently letting it stand.
    result = _install_direction_result("decrease_adset_budget", 2.0)
    assert result.verdict == "refuted"
    assert "direction" in result.failed_inputs
    assert any("cutting the budget" in reason for reason in result.reasons)


def test_review_install_scale_agreeing_with_target_stands() -> None:
    # A genuine winner: cost/install ($2) below the $3 target — scaling it agrees with the goal, so
    # the direction check does not fire.
    result = _install_direction_result("consider_scale_budget", 2.0)
    assert result.verdict == "stands"
    assert "direction" not in result.failed_inputs


def test_review_install_pause_agreeing_with_target_stands() -> None:
    # A loser worth pausing: cost/install ($5) above target — pausing it agrees with the goal, so the
    # direction check does not fire.
    result = _install_direction_result("pause_ad", 5.0)
    assert result.verdict == "stands"
    assert "direction" not in result.failed_inputs


def test_review_install_scale_at_target_stands() -> None:
    # Boundary pin: cost/install ($3.00) sits EXACTLY at the $3 target. The scale branch uses a strict
    # `>`, so an at-target scale is NOT refuted (mirrors the ROAS scale branch's strict `<`).
    result = _install_direction_result("increase_adset_budget", 3.0)
    assert result.verdict == "stands"
    assert "direction" not in result.failed_inputs


def test_review_install_missing_target_does_not_fire() -> None:
    # Conservative guard: with an install goal but NO secondary_cost_per_app_install_target configured,
    # the cost-polarity direction check cannot fire — a would-be pause-a-winner stands. Pins the guard
    # that keeps install-goal accounts without a cost target out of the gate.
    pause = _install_direction_result("pause_ad", 1.5, target=None)
    scale = _install_direction_result("consider_scale_budget", 5.0, target=None)
    assert pause.verdict == "stands"
    assert "direction" not in pause.failed_inputs
    assert scale.verdict == "stands"
    assert "direction" not in scale.failed_inputs


def test_review_no_claimed_band_is_defensive_noop() -> None:
    # A confidence block with no recognizable band has nothing to refute → stands (never crashes,
    # never fabricates a verdict).
    evidence = _review_evidence(window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0)
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence={"band": None},
        action={"action_type": "pause_ad"},
        policy={},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "stands"
    assert result.failed_inputs == []


def test_review_accumulates_multiple_downgrades_most_conservative_wins() -> None:
    # A short window (one-band downgrade) AND external evidence (cap at low) both fire on one call;
    # the most-conservative revised band wins and BOTH failing inputs are named.
    evidence = _review_evidence(
        window="2026-06-21..2026-06-24", purchases=120.0, spend=2400.0, metric_value=2.0
    )
    confidence = {
        "band": "medium",
        "data_band": "medium",
        "grounding_band": "low",
        "grounding_tier": "external",
        "factors": [],
        "would_raise": "",
        "would_lower": "",
        "causal_flag": False,
    }
    result = review_recommendation(
        evidence=evidence_to_dict(evidence),
        confidence=confidence,
        action={"action_type": "pause_ad"},
        policy={},
        spend_floor=100.0,
        conversions_floor=25.0,
        min_window_days=7,
        recency_stale_days=14,
        recency_days=1,
    )

    assert result.verdict == "downgrade"
    assert {"window_length", "external"} <= set(result.failed_inputs)
    # external caps at low, which is more conservative than the one-band window downgrade (→ low).
    assert result.revised_band == "low"


def _confidence_action(
    *,
    action_id: str,
    action_type: str,
    status: str,
    executable: bool,
    evidence: Evidence,
    confidence,
    rationale: str = "Cited rationale.",
) -> dict:
    return {
        "action_id": action_id,
        "action_type": action_type,
        "status": status,
        "executable": executable,
        "target": {"type": "ad", "id": "123", "name": "Cody - Copy"},
        "params": {"status": "paused"} if action_type == "pause_ad" else {},
        "rationale": rationale,
        "evidence": evidence_to_dict(evidence),
        "confidence": confidence_to_dict(confidence),
    }


def test_review_action_plan_below_floor_flips_to_keep_running() -> None:
    evidence = _review_evidence(window="2026-06-19..2026-06-24", purchases=9.0, spend=40.0)
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.correlational,
        spend_floor=75.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _confidence_action(
                action_id="consider_scale_budget_123",
                action_type="consider_scale_budget",
                status="proposed",
                executable=False,
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    reviewed = review_action_plan(plan)
    action = reviewed["actions"][0]

    assert action["review"]["verdict"] == "insufficient"
    assert action["executable"] is False
    assert action["verdict"] == "insufficient_data"
    assert action["confidence"]["band"] == "abstain"
    assert any("floor" in reason for reason in action["review"]["reasons"])
    # The input plan was not mutated.
    assert "review" not in plan["actions"][0]


def test_review_action_plan_skips_non_recommendation_actions() -> None:
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            {
                "action_id": "measurement_review_1",
                "action_type": "measurement_review",
                "status": "proposed",
                "executable": False,
                "target": {"type": "account", "id": "acct"},
                "params": {},
                "rationale": "Verify the pixel.",
                "evidence": {},
            }
        ],
    }

    reviewed = review_action_plan(plan)
    action = reviewed["actions"][0]

    assert "review" not in action  # no confidence block → never reviewed
    assert action == plan["actions"][0]  # passed through untouched


def test_review_gate_only_ever_demotes() -> None:
    clean_evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=1.0
    )
    clean_conf = assess(
        evidence=clean_evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    winner_evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=6.0
    )
    winner_conf = assess(
        evidence=winner_evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "account_action_policy": {"primary_goal": "roas", "target_roas": 3.0},
        "actions": [
            _confidence_action(
                action_id="pause_ad_clean",
                action_type="pause_ad",
                status="proposed",
                executable=True,
                evidence=clean_evidence,
                confidence=clean_conf,
            ),
            _confidence_action(
                action_id="pause_ad_winner",
                action_type="pause_ad",
                status="approved",
                executable=True,
                evidence=winner_evidence,
                confidence=winner_conf,
            ),
        ],
    }

    reviewed = review_action_plan(plan)

    for before, after in zip(plan["actions"], reviewed["actions"]):
        # executable is never raised
        assert not (after["executable"] and not before["executable"])
        # status is never promoted to approved
        assert not (after.get("status") == "approved" and before.get("status") != "approved")
        # the band is never raised
        assert Band[after["confidence"]["band"]] <= Band[before["confidence"]["band"]]

    # the winner was actually demoted (refuted), proving the gate fired
    winner = reviewed["actions"][1]
    assert winner["review"]["verdict"] == "refuted"
    assert winner["executable"] is False
    assert winner["status"] == "proposed"


def test_review_action_plan_is_idempotent() -> None:
    short_window_evidence = _review_evidence(
        window="2026-06-21..2026-06-24", purchases=30.0, spend=200.0, metric_value=1.1
    )
    short_window_conf = assess(
        evidence=short_window_evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    clean_evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=1.0
    )
    clean_conf = assess(
        evidence=clean_evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "actions": [
            _confidence_action(
                action_id="pause_ad_short",
                action_type="pause_ad",
                status="approved",
                executable=True,
                evidence=short_window_evidence,
                confidence=short_window_conf,
            ),
            _confidence_action(
                action_id="pause_ad_clean",
                action_type="pause_ad",
                status="proposed",
                executable=True,
                evidence=clean_evidence,
                confidence=clean_conf,
            ),
        ],
    }

    once = review_action_plan(plan)
    twice = review_action_plan(once)

    assert once["actions"][0]["review"]["verdict"] == "downgrade"  # precondition: a real correction
    assert twice == once


def test_operator_brief_review_refuted_direction_surfaced_not_approved() -> None:
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=6.0
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "account_action_policy": {"primary_goal": "roas", "target_roas": 3.0},
        "actions": [
            _confidence_action(
                action_id="pause_ad_winner",
                action_type="pause_ad",
                status="approved",
                executable=True,
                evidence=evidence,
                confidence=confidence,
                rationale="High waste risk.",
            )
        ],
    }

    brief = build_operator_brief(plan=plan)

    assert brief["approved_to_execute"] == []
    assert [a["action_id"] for a in brief["refuted_or_downgraded_by_review"]] == ["pause_ad_winner"]
    assert brief["summary"]["reviewed_out_count"] == 1

    markdown = render_operator_brief(brief)
    approved_section = markdown.split("## Approved To Execute", 1)[1].split("## ", 1)[0]
    assert "pause_ad_winner" not in approved_section
    review_section = markdown.split("## Refuted / Downgraded By Review", 1)[1]
    assert "pause_ad_winner" in review_section
    assert "direction" in review_section


def test_operator_brief_no_review_reproduces_pre_gate_behavior() -> None:
    evidence = _review_evidence(
        window="2026-06-10..2026-06-24", purchases=120.0, spend=2400.0, metric_value=6.0
    )
    confidence = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-24",
        "account_action_policy": {"primary_goal": "roas", "target_roas": 3.0},
        "actions": [
            _confidence_action(
                action_id="pause_ad_winner",
                action_type="pause_ad",
                status="approved",
                executable=True,
                evidence=evidence,
                confidence=confidence,
            )
        ],
    }

    brief = build_operator_brief(plan=plan, review_enabled=False)

    # With the gate off the contradictory call is NOT filtered (escape hatch reproduces old behavior).
    assert [a["action_id"] for a in brief["approved_to_execute"]] == ["pause_ad_winner"]
    assert brief["refuted_or_downgraded_by_review"] == []


def test_api_operation_only_allows_explicit_pause_without_meta_ai_params() -> None:
    action = {
        "action_type": "pause_ad",
        "target": {"id": "123"},
        "params": {"status": "paused"},
    }

    operation = build_api_operation(action)

    assert operation == {"resource": "ad", "id": "123", "params": {"status": "PAUSED"}}

    action["params"]["advantage_plus_creative"] = True
    try:
        build_api_operation(action)
    except ValueError as exc:
        assert "Meta AI" in str(exc)
    else:
        raise AssertionError("Expected Meta AI guardrail to block action")


def test_api_operation_allows_capped_adset_budget_increase() -> None:
    action = {
        "action_type": "increase_adset_budget",
        "target": {"id": "adset-1"},
        "params": {
            "current_daily_budget_cents": 10000,
            "new_daily_budget_cents": 12000,
            "max_increase_percent": 20,
        },
    }

    operation = build_api_operation(action)

    assert operation == {"resource": "adset", "id": "adset-1", "params": {"daily_budget": "12000"}}

    action["params"]["new_daily_budget_cents"] = 13000
    try:
        build_api_operation(action)
    except ValueError as exc:
        assert "exceeds max increase" in str(exc)
    else:
        raise AssertionError("Expected budget cap guardrail to block action")


def test_apply_action_plan_dry_run_requires_approval(tmp_path: Path, monkeypatch) -> None:
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": "pollen_sense",
                        "account_name": "Pollen Sense",
                        "ad_account_id": "12345",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path)

    plan = {
        "account_slug": "pollen_sense",
        "run_date": "2026-05-04",
        "actions": [
            {
                "action_id": "pause_ad_123",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"id": "123"},
                "params": {"status": "paused"},
            }
        ],
    }

    skipped = apply_action_plan(plan, execute=False)
    assert skipped[0].status == "skipped"
    assert skipped[0].reason == "Action is not approved."

    plan["actions"][0]["status"] = "approved"
    dry_run = apply_action_plan(plan, execute=False)
    assert dry_run[0].status == "dry_run"
    assert dry_run[0].request == {"resource": "ad", "id": "123", "params": {"status": "PAUSED"}}


class _LiveStateFakeClient:
    """Stands in for MetaMarketingApiClient during live-state enrichment tests."""

    def __init__(self, *, ads=None, adsets=None, ad_error=None):
        self._ads = ads or {}
        self._adsets = adsets or {}
        self._ad_error = ad_error

    def get_ad(self, ad_id, *, fields):
        if self._ad_error is not None:
            raise self._ad_error
        return self._ads[ad_id]

    def get_adset(self, adset_id, *, fields):
        return self._adsets[adset_id]


def test_live_state_enrichment_marks_only_ad_status_paused_as_resolved() -> None:
    plan = {
        "account_slug": "pollen_sense",
        "run_date": "2026-06-16",
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "approval_required": True,
                "target": {"type": "ad", "id": "1"},
                "params": {"status": "paused"},
                "rationale": "Pause bad ad.",
            },
            {
                "action_id": "pause_ad_2",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "approval_required": True,
                "target": {"type": "ad", "id": "2"},
                "params": {"status": "paused"},
                "rationale": "Pause bad ad.",
            },
        ],
    }

    client = _LiveStateFakeClient(
        ads={
            "1": {"id": "1", "name": "Ad 1", "status": "PAUSED", "effective_status": "PAUSED"},
            "2": {"id": "2", "name": "Ad 2", "status": "ACTIVE", "effective_status": "ADSET_PAUSED"},
        }
    )

    enriched = enrich_action_plan_with_live_state(plan, reader=client)
    by_id = {action["action_id"]: action for action in enriched["actions"]}

    assert by_id["pause_ad_1"]["status"] == "already_resolved"
    assert by_id["pause_ad_1"]["executable"] is False
    assert by_id["pause_ad_2"]["status"] == "proposed"
    assert by_id["pause_ad_2"]["executable"] is True
    assert by_id["pause_ad_2"]["live_state"]["effective_status"] == "ADSET_PAUSED"


def test_live_state_enrichment_redacts_tokens_on_api_failure() -> None:
    plan = {
        "account_slug": "pollen_sense",
        "run_date": "2026-06-16",
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"type": "ad", "id": "ad-1"},
                "params": {"status": "paused"},
            }
        ],
    }

    error = MetaApiError(
        "GET /v25.0/ad-1?access_token=EAAabcdefghijklmnopqrstuvwx1234567890&fields=name "
        "token EAAabcdefghijklmnopqrstuvwx1234567890"
    )
    client = _LiveStateFakeClient(ad_error=error)

    enriched = enrich_action_plan_with_live_state(plan, reader=client)
    message = enriched["actions"][0]["live_state"]["error"]

    assert "EAAabcdefghijklmnopqrstuvwx1234567890" not in message
    assert "access_token=[REDACTED]" in message
    assert "[REDACTED_META_TOKEN]" in message


def test_live_state_enrichment_flags_meta_ai_adset_controls() -> None:
    plan = {
        "account_slug": "divine_designs",
        "run_date": "2026-06-16",
        "account_action_policy": {"disable_meta_ai_features": True},
        "actions": [
            {
                "action_id": "consider_scale_budget_1",
                "action_type": "consider_scale_budget",
                "status": "proposed",
                "executable": False,
                "target": {"type": "ad", "id": "ad-1"},
                "params": {},
            }
        ],
    }

    client = _LiveStateFakeClient(
        ads={
            "ad-1": {
                "id": "ad-1",
                "name": "Ad 1",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                "adset_id": "adset-1",
            }
        },
        adsets={
            "adset-1": {
                "id": "adset-1",
                "name": "Ad Set 1",
                "status": "ACTIVE",
                "effective_status": "ACTIVE",
                # The Graph API returns targeting as a JSON object, not a string.
                "targeting": {"targeting_automation": {"advantage_audience": 1}},
            }
        },
    )

    enriched = enrich_action_plan_with_live_state(plan, reader=client)

    assert any(action["action_type"] == "disable_meta_ai_controls" for action in enriched["actions"])


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    ensure_dir(path.parent)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _daily_metric_row(
    report_date: date,
    *,
    ad_id: str,
    ad_name: str,
    spend: float,
    app_installs: float,
) -> dict[str, object]:
    return {
        "report_date": report_date,
        "campaign_id": "campaign-1",
        "campaign_name": "Trajectory Campaign",
        "adset_id": "adset-1",
        "adset_name": "Trajectory Set",
        "ad_id": ad_id,
        "ad_name": ad_name,
        "creative_type": "Dynamic",
        "spend": spend,
        "purchase_value": 0.0,
        "purchase_count": 0.0,
        "results": 0.0,
        "result_label": "In-app subscriptions",
        "app_installs": app_installs,
        "impressions": 1000,
        "outbound_clicks": 20,
        "frequency": 1.1,
        "video_3s_plays": 400,
        "thruplays": 100,
        "has_video_metrics": True,
        "tracking_confidence": "medium_roas_unavailable",
    }


# --- Audience rotation -------------------------------------------------------

from meta_ads_analysis.rotation import (
    apply_rotation_plan,
    build_rotation_plan,
    compute_new_targeting,
)


def _adset(adset_id, name, included, excluded, *, advantage=False):
    targeting = {
        "geo_locations": {"countries": ["US"]},
        "age_min": 25,
        "custom_audiences": [{"id": i, "name": f"aud-{i}"} for i in included],
        "excluded_custom_audiences": [{"id": i, "name": f"aud-{i}"} for i in excluded],
    }
    if advantage:
        targeting["targeting_automation"] = {"advantage_audience": 1}
    return {
        "id": adset_id,
        "name": name,
        "effective_status": "ACTIVE",
        "campaign_id": "camp-1",
        "targeting": targeting,
    }


def _three_adset_partition():
    return [
        _adset("as1", "Set 1", ["A"], ["B", "C"]),
        _adset("as2", "Set 2", ["B"], ["A", "C"]),
        _adset("as3", "Set 3", ["C"], ["A", "B"]),
    ]


class _FakeClient:
    def __init__(self, adsets):
        self._by_id = {a["id"]: a for a in adsets}
        self.updates = []

    def get_adset(self, adset_id, *, fields):
        return self._by_id[adset_id]

    def update_adset(self, adset_id, *, params, validate_only=False):
        self.updates.append((adset_id, params, validate_only))
        return {"id": adset_id, "success": True}


def test_build_rotation_plan_shifts_audiences_and_preserves_partition_invariant() -> None:
    plan = build_rotation_plan(
        _three_adset_partition(),
        account_slug="demo",
        ad_account_id="act_1",
        offset=1,
    )
    rotations = {r["adset_id"]: r for r in plan["rotations"]}
    # Each audience moves forward one ad set: as1 gets C, as2 gets A, as3 gets B.
    assert rotations["as1"]["new_included"] == ["C"]
    assert rotations["as2"]["new_included"] == ["A"]
    assert rotations["as3"]["new_included"] == ["B"]
    # Exclusions are recomputed as "the other two" so the invariant still holds.
    assert sorted(rotations["as1"]["new_excluded"]) == ["A", "B"]
    assert sorted(rotations["as2"]["new_excluded"]) == ["B", "C"]
    assert sorted(rotations["as3"]["new_excluded"]) == ["A", "C"]
    assert all(r["status"] == "proposed" for r in plan["rotations"])


def test_build_rotation_plan_flags_advantage_audience_and_skips_audienceless_adsets() -> None:
    adsets = _three_adset_partition()
    adsets[0]["targeting"]["targeting_automation"] = {"advantage_audience": 1}
    adsets.append(_adset("as4", "No audience", [], []))
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1")
    assert any("Advantage" in w for w in plan["warnings"])
    assert any("as4" in w for w in plan["warnings"])
    assert "as4" not in {r["adset_id"] for r in plan["rotations"]}


def test_compute_new_targeting_preserves_other_fields() -> None:
    live = _adset("as1", "Set 1", ["A"], ["B", "C"])["targeting"]
    new = compute_new_targeting(live, new_included_ids=["C"], new_excluded_ids=["A", "B"])
    assert new["geo_locations"] == {"countries": ["US"]}
    assert new["age_min"] == 25
    assert new["custom_audiences"] == [{"id": "C"}]
    assert new["excluded_custom_audiences"] == [{"id": "A"}, {"id": "B"}]


def test_apply_rotation_dry_run_does_not_write() -> None:
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1")
    for rotation in plan["rotations"]:
        rotation["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=False)
    assert {r.status for r in results} == {"dry_run"}
    assert client.updates == []


def test_apply_rotation_execute_writes_full_targeting_for_approved_only() -> None:
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1")
    plan["rotations"][0]["status"] = "approved"  # only as1 approved
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    statuses = {r.adset_id: r.status for r in results}
    assert statuses["as1"] == "executed"
    assert statuses["as2"] == "skipped"
    assert len(client.updates) == 1
    adset_id, params, validate_only = client.updates[0]
    assert adset_id == "as1"
    assert validate_only is False
    # The full targeting object is sent, not just the audience fields.
    assert params["targeting"]["geo_locations"] == {"countries": ["US"]}
    assert params["targeting"]["custom_audiences"] == [{"id": "C"}]


def test_apply_rotation_blocks_when_live_targeting_drifted() -> None:
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1")
    plan["rotations"][0]["status"] = "approved"
    # Simulate the live ad set's audience changing after the plan was built.
    adsets[0]["targeting"]["custom_audiences"] = [{"id": "Z"}]
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert client.updates == []


def test_compute_new_targeting_can_disable_advantage_audience() -> None:
    live = _adset("as1", "Set 1", ["A"], ["B", "C"], advantage=True)["targeting"]

    # Default: automation is preserved untouched.
    kept = compute_new_targeting(live, new_included_ids=["C"], new_excluded_ids=["A", "B"])
    assert kept["targeting_automation"] == {"advantage_audience": 1}

    # Opt-in: advantage_audience forced off, other targeting preserved.
    off = compute_new_targeting(
        live,
        new_included_ids=["C"],
        new_excluded_ids=["A", "B"],
        disable_advantage_audience=True,
    )
    assert off["targeting_automation"]["advantage_audience"] == 0
    assert off["geo_locations"] == {"countries": ["US"]}
    assert off["custom_audiences"] == [{"id": "C"}]


def test_compute_new_targeting_strips_age_range_when_disabling_advantage_audience() -> None:
    # Meta rejects age_range once targeting automation is off; it must be dropped.
    live = {
        "geo_locations": {"countries": ["US"]},
        "age_min": 18,
        "age_max": 65,
        "age_range": [18, 65],
        "custom_audiences": [{"id": "A"}],
        "targeting_automation": {"advantage_audience": 1},
    }
    off = compute_new_targeting(
        live, new_included_ids=["C"], new_excluded_ids=["A"], disable_advantage_audience=True
    )
    assert "age_range" not in off
    assert off["age_min"] == 18 and off["age_max"] == 65

    # Without disabling, age_range is preserved untouched.
    kept = compute_new_targeting(live, new_included_ids=["C"], new_excluded_ids=["A"])
    assert kept["age_range"] == [18, 65]


def test_rotation_plan_disable_flag_writes_advantage_off_on_apply() -> None:
    adsets = [
        _adset("as1", "Set 1", ["A"], ["B", "C"], advantage=True),
        _adset("as2", "Set 2", ["B"], ["A", "C"], advantage=True),
        _adset("as3", "Set 3", ["C"], ["A", "B"], advantage=True),
    ]
    plan = build_rotation_plan(
        adsets,
        account_slug="demo",
        ad_account_id="act_1",
        disable_advantage_audience=True,
    )
    assert plan["disable_advantage_audience"] is True
    assert all(r["disable_advantage_audience"] for r in plan["rotations"])
    assert all("advantage_audience: on -> off" in r["diff"] for r in plan["rotations"])

    for rotation in plan["rotations"]:
        rotation["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)

    assert {r.status for r in results} == {"executed"}
    for _adset_id, params, _validate_only in client.updates:
        assert params["targeting"]["targeting_automation"]["advantage_audience"] == 0


from meta_ads_analysis.rotation import (
    apply_rename_plan,
    build_rename_plan,
    friendly_audience_name,
)


def test_update_adset_validate_only_injects_execution_options() -> None:
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {"success": True}
    session = Mock()
    session.post.return_value = resp

    client = MetaMarketingApiClient("token", session=session)
    client.update_adset("as1", params={"name": "New Name"}, validate_only=True)

    _args, kwargs = session.post.call_args
    data = kwargs["data"]
    assert data["name"] == "New Name"
    assert data["execution_options"] == json.dumps(["validate_only"])

    # Without the flag, no execution_options is sent.
    client.update_adset("as1", params={"name": "New Name"})
    assert "execution_options" not in session.post.call_args.kwargs["data"]


def test_apply_rotation_validate_only_sends_validate_flag_and_does_not_execute() -> None:
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1")
    for rotation in plan["rotations"]:
        rotation["status"] = "approved"
    client = _FakeClient(adsets)

    results = apply_rotation_plan(plan, client, execute=False, validate_only=True)

    assert {r.status for r in results} == {"validated"}
    assert len(client.updates) == 3
    assert all(validate_only is True for _id, _params, validate_only in client.updates)


def test_friendly_audience_name_prefers_seed_over_lookalike() -> None:
    refs = [
        {"id": "1", "name": "Lookalike (1%) - high-value-customers.csv"},
        {"id": "2", "name": "high-value-customers-facebook-fixed.csv"},
    ]
    assert friendly_audience_name(refs, {}) == "High Value Customers"


def test_build_and_apply_rename_plan_writes_only_name() -> None:
    adsets = [
        {
            "id": "as1",
            "name": "Stills",
            "effective_status": "ACTIVE",
            "campaign_id": "c1",
            "targeting": {
                "custom_audiences": [
                    {"id": "1", "name": "high-value-customers.csv"},
                    {"id": "2", "name": "Lookalike (1%) - high-value-customers.csv"},
                ]
            },
        }
    ]
    plan = build_rename_plan(adsets, account_slug="demo", ad_account_id="act_1")
    assert plan["renames"][0]["old_name"] == "Stills"
    assert plan["renames"][0]["new_name"] == "High Value Customers"

    plan["renames"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rename_plan(plan, client, execute=True)

    assert results[0].status == "executed"
    adset_id, params, validate_only = client.updates[0]
    assert adset_id == "as1"
    assert params == {"name": "High Value Customers"}
    assert validate_only is False


def test_apply_rename_plan_blocks_on_live_name_drift() -> None:
    adsets = [
        {
            "id": "as1",
            "name": "Renamed Already",
            "effective_status": "ACTIVE",
            "campaign_id": "c1",
            "targeting": {"custom_audiences": [{"id": "1", "name": "high-value-customers.csv"}]},
        }
    ]
    plan = build_rename_plan(adsets, account_slug="demo", ad_account_id="act_1")
    plan["renames"][0]["old_name"] = "Stale Old Name"  # simulate plan built against older state
    plan["renames"][0]["status"] = "approved"
    client = _FakeClient(adsets)

    results = apply_rename_plan(plan, client, execute=True)

    assert results[0].status == "blocked"
    assert client.updates == []


from meta_ads_analysis.rotation import (
    apply_advantage_disable_plan,
    build_advantage_disable_plan,
)


def test_build_advantage_disable_plan_flags_on_vs_off() -> None:
    adsets = [
        _adset("as1", "Set 1", ["A"], ["B"], advantage=True),
        _adset("as2", "Set 2", ["B"], ["A"], advantage=False),
    ]
    plan = build_advantage_disable_plan(adsets, account_slug="demo", ad_account_id="act_1")
    by_id = {i["adset_id"]: i for i in plan["items"]}
    assert by_id["as1"]["advantage_audience"] is True
    assert by_id["as2"]["advantage_audience"] is False
    # audiences captured verbatim
    assert by_id["as1"]["included"] == ["A"]
    assert by_id["as1"]["excluded"] == ["B"]


def test_apply_advantage_disable_preserves_audiences_and_turns_off_aa() -> None:
    adsets = [
        _adset("as1", "Set 1", ["A"], ["B", "C"], advantage=True),
        _adset("as2", "Set 2", ["B"], ["A"], advantage=False),
    ]
    plan = build_advantage_disable_plan(adsets, account_slug="demo", ad_account_id="act_1")
    for item in plan["items"]:
        item["status"] = "approved"
    client = _FakeClient(adsets)

    results = apply_advantage_disable_plan(plan, client, execute=True)
    by_id = {r.adset_id: r for r in results}

    # AA was on for as1 -> executed; off for as2 -> skipped (no write)
    assert by_id["as1"].status == "executed"
    assert by_id["as2"].status == "skipped"
    assert len(client.updates) == 1
    adset_id, params, validate_only = client.updates[0]
    assert adset_id == "as1"
    t = params["targeting"]
    assert t["targeting_automation"]["advantage_audience"] == 0
    # audiences preserved exactly, not rotated
    assert t["custom_audiences"] == [{"id": "A"}]
    assert t["excluded_custom_audiences"] == [{"id": "B"}, {"id": "C"}]


# --- Rotation grounding (evidence + correlational-capped confidence + review) ----

from meta_ads_analysis.review import review_rotation_plan

_ROTATION_WINDOW = {"date_from": "2026-06-10", "date_to": "2026-06-24",
                    "recency_days": 1, "run_date": "2026-06-25"}


def _rotation_metric_row(adset_id, name, *, purchases, spend):
    """One fetch_entity_metrics-shaped row for an ad set's window performance."""
    roas = round(spend / spend, 2) if spend else None  # placeholder ROAS; band is sample-driven
    return {"id": adset_id, "name": name, "spend": float(spend), "roas": roas,
            "purchases": float(purchases), "cost_per_app_install": None}


def test_rotation_fatigued_adset_carries_correlational_capped_confidence() -> None:
    # A high-spend ad set proposed for rotation carries the band the rubric COMPUTES from its own
    # window sample — and because fatigue is correlational, a strong sample caps at MEDIUM, never high.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["sample_purchases"] == 120.0
    assert op["confidence"]["grounding_tier"] == "correlational"
    # 120 purchases would read high on the data axis, but correlational grounding caps it at medium.
    assert op["confidence"]["band"] == "medium"
    assert op["review"]["verdict"] == "stands"


def test_rotation_thin_sample_abstains_and_is_flagged_insufficient() -> None:
    # A below-floor fatigue sample abstains (never a fabricated low) and review marks it insufficient
    # — non-executable: rotating on no evidence of fatigue is exactly what grounding prevents.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=9, spend=40)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["confidence"]["band"] == "abstain"
    assert op["review_verdict"] == "insufficient"


def test_rotation_review_iterates_rotations_not_ops() -> None:
    # Pin against the #1 failure mode: review_rotation_plan must iterate plan["rotations"], not a
    # missing plan["ops"]. Every rotation item actually receives a review block.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert len(plan["rotations"]) == 3
    assert all(isinstance(r.get("review"), dict) and r["review"]["verdict"] for r in plan["rotations"])


def test_rotation_review_demotes_overclaimed_band() -> None:
    # A hand-inflated 'high' band over a sample the correlational rubric only supports at 'medium'.
    plan = {
        "plan_type": "audience_rotation", "run_date": "2026-06-25",
        "rotations": [{
            "adset_id": "as1", "adset_name": "Set 1", "status": "approved",
            "evidence": {"metric_name": "blended_roas", "metric_value": 1.0,
                         "window": "2026-06-10..2026-06-24", "sample_purchases": 30.0,
                         "sample_spend": 500.0, "entity_level": "adset", "entity_id": "as1"},
            "confidence": {"band": "high", "data_band": "high", "grounding_band": "high",
                           "grounding_tier": "correlational", "factors": [], "would_raise": "",
                           "would_lower": "", "causal_flag": False},
        }],
    }
    reviewed = review_rotation_plan(plan)
    r = reviewed["rotations"][0]
    assert r["review"]["verdict"] == "downgrade"
    assert Band[r["confidence"]["band"]] < Band.high
    assert r["review_verdict"] == "downgrade"
    # input plan not mutated
    assert "review" not in plan["rotations"][0]
    assert plan["rotations"][0]["confidence"]["band"] == "high"


def test_rotation_causal_claim_is_downgraded() -> None:
    # A rotation rationale asserting the audience CAUSED the drop must not survive at a causal band:
    # fatigue is correlational, so a cause-claim from a decline alone is downgraded (confirm via A/B).
    plan = {
        "plan_type": "audience_rotation", "run_date": "2026-06-25",
        "rotations": [{
            "adset_id": "as1", "status": "approved",
            "evidence": {"metric_name": "blended_roas", "metric_value": 1.0,
                         "window": "2026-06-10..2026-06-24", "sample_purchases": 120.0,
                         "sample_spend": 2400.0, "entity_level": "adset", "entity_id": "as1"},
            "confidence": {"band": "high", "data_band": "high", "grounding_band": "medium",
                           "grounding_tier": "correlational", "factors": [], "would_raise": "",
                           "would_lower": "", "causal_flag": True},
        }],
    }
    reviewed = review_rotation_plan(plan)
    r = reviewed["rotations"][0]
    assert r["review"]["verdict"] == "downgrade"
    assert "causal" in r["review"]["failed_inputs"]
    assert r["confidence"]["band"] == "low"  # correlational causal cap


def test_advantage_disable_item_attaches_structural_abstain() -> None:
    # Turning Advantage Audience off is a safety op with NO performance metric — it must abstain with a
    # structural factor (no sample cited), and review must not refute it for "contradicting its metric".
    adsets = [_adset("as1", "Set 1", ["A"], ["B"], advantage=True)]
    plan = build_advantage_disable_plan(adsets, account_slug="demo", ad_account_id="act_1")
    item = plan["items"][0]
    assert item["confidence"]["band"] == "abstain"
    assert item["confidence"]["data_band"] == "abstain"
    assert item["evidence"]["sample_purchases"] is None and item["evidence"]["sample_spend"] is None
    # structural abstain (no cited sample) → the gate does not refute it
    assert item["review"]["verdict"] == "stands"


def test_rename_plan_passes_through_review_without_fabricated_band() -> None:
    # Renames are pure structural (name only) — exempt from grounding. review_rotation_plan must leave
    # them untouched: no confidence, no review block, no fabricated performance band.
    adsets = [{
        "id": "as1", "name": "Stills", "effective_status": "ACTIVE", "campaign_id": "c1",
        "targeting": {"custom_audiences": [{"id": "1", "name": "high-value-customers.csv"}]},
    }]
    plan = build_rename_plan(adsets, account_slug="demo", ad_account_id="act_1")
    reviewed = review_rotation_plan(plan)
    r = reviewed["renames"][0]
    assert "confidence" not in r and "review" not in r
    assert r["new_name"] == "High Value Customers"  # band-free, unchanged


def test_rotation_review_is_idempotent() -> None:
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    again = review_rotation_plan(plan)
    assert [r["confidence"]["band"] for r in again["rotations"]] == \
        [r["confidence"]["band"] for r in plan["rotations"]]
    assert again["rotations"][0]["review"] == plan["rotations"][0]["review"]


def test_rotation_high_confidence_still_blocks_on_live_targeting_drift() -> None:
    # Grounding/review runs at propose; the live-targeting drift check runs at execute. A confidently
    # grounded rotation is STILL blocked if the ad set's audience drifted since plan time.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["confidence"]["band"] == "medium"  # confidently grounded
    plan["rotations"][0]["status"] = "approved"
    adsets[0]["targeting"]["custom_audiences"] = [{"id": "Z"}]  # live drift after plan time
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert client.updates == []


def test_rotation_adset_with_no_window_row_cites_zero_sample_and_abstains() -> None:
    # Production-realistic: fetch_entity_metrics returns rows only for ad sets that delivered, so the
    # CLI may pass a metrics map that omits a proposed ad set entirely. That ad set must cite a ZERO
    # sample (not a structural no-metric abstain) → assess abstains → review marks it insufficient:
    # rotating on no evidence of fatigue is exactly what grounding prevents.
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id={}, goal="roas", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["sample_purchases"] == 0.0 and op["evidence"]["sample_spend"] == 0.0
    assert op["confidence"]["band"] == "abstain"
    assert op["review_verdict"] == "insufficient"


def _rotation_install_row(adset_id, name, *, app_installs, spend, purchases=0.0):
    """A fetch_entity_metrics-shaped row for an install-goal ad set: real install volume, ~0 purchases
    (the conversion an install account never produces)."""
    return {"id": adset_id, "name": name, "spend": float(spend), "roas": None,
            "purchases": float(purchases), "app_installs": float(app_installs),
            "cost_per_app_install": round(spend / app_installs, 2) if app_installs else None}


def test_rotation_install_goal_grounds_on_installs_clears_low() -> None:
    # The fix for the rotation write path: an install-goal account has purchases≈0 but real install
    # volume. The fatigue sample must be the install count (the conversion behind the goal-aware
    # cost-per-install metric), so the band clears low/abstain instead of being structurally pinned
    # there. Correlational tier still caps it at medium — never high.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_install_row(a["id"], a["name"], app_installs=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics,
                               goal="maximize_in_app_subscriptions", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["sample_purchases"] == 120.0  # installs ground the sample, not the 0 purchases
    assert Band[op["confidence"]["band"]] > Band.low  # 120 installs clears low/abstain
    assert op["confidence"]["band"] == "medium"  # correlational cap (never high) per ROTATION_EVIDENCE_TIER
    assert op["review"]["verdict"] == "stands"


def test_rotation_roas_goal_ignores_app_installs_decoy() -> None:
    # Parity guard: on a ROAS/default goal the fatigue sample stays purchases. An app_installs decoy in
    # the row must be ignored — byte-identical to a purchases-only fixture (band still medium for 120).
    adsets = _three_adset_partition()
    metrics = {a["id"]: {"id": a["id"], "name": a["name"], "spend": 2400.0, "roas": 1.0,
                         "purchases": 120.0, "app_installs": 999.0,  # decoy — must be ignored
                         "cost_per_app_install": 2.4}
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 120.0  # purchases, NOT the 999 install decoy
    assert op["confidence"]["band"] == "medium"


def test_rotation_install_goal_no_window_row_still_cites_zero_and_abstains() -> None:
    # The zero-sample branch (ad set absent from the metrics map) is goal-independent: the goal-aware
    # selector touches ONLY the present-row branch. An install-goal rotation with no row still cites a
    # ZERO sample → abstain, exactly as a ROAS-goal one does.
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id={},
                               goal="maximize_in_app_subscriptions", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["sample_purchases"] == 0.0 and op["evidence"]["sample_spend"] == 0.0
    assert op["confidence"]["band"] == "abstain"
    assert op["review_verdict"] == "insufficient"


def test_rotation_install_goal_structural_abstain_when_no_metrics() -> None:
    # The structural (metrics_by_id is None) branch is also goal-independent: no sample is cited at all
    # regardless of goal, so the goal-aware selector is never reached.
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=None,
                               goal="maximize_in_app_subscriptions", **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["sample_purchases"] is None  # structural — no cited sample
    assert op["confidence"]["band"] == "abstain"


def test_rotation_no_goal_installs_present_keeps_sample_on_purchases() -> None:
    # Intentional asymmetry (parity with control + actions._select_sample_conversions): with NO goal
    # set, _status_metric falls through to cost_per_app_install when installs are present, but the
    # significance sample stays on purchases — the selector keys ONLY on the explicit install-goal
    # string. Metric and sample can legitimately disagree here; this pins that it is NOT "fixed".
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_install_row(a["id"], a["name"], app_installs=120, spend=2400,
                                              purchases=30.0)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal=None, **_ROTATION_WINDOW)
    op = plan["rotations"][0]
    assert op["evidence"]["metric_name"] == "cost_per_app_install"  # no goal + installs present
    assert op["evidence"]["sample_purchases"] == 30.0  # but the sample stays on purchases


def test_apply_rotation_blocks_approved_thin_sample_at_execute() -> None:
    # An operator approves a rotation whose fatigue sample is below the significance floor (cited
    # abstain). The propose-time review already marked it insufficient; the apply-time grounding gate
    # is the hard backstop — it must block the write, the same way an ungrounded ops write is blocked.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=9, spend=40)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["confidence"]["band"] == "abstain"  # cited thin sample
    plan["rotations"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert "insufficient data" in results[0].reason
    assert client.updates == []


def test_apply_rotation_blocks_approved_zero_sample_at_execute() -> None:
    # An ad set that did not deliver in the window has no metrics row, so it cites a ZERO sample
    # (abstain WITH a sample). An approved rotation on such an ad set — rotating on no evidence of
    # fatigue — must be hard-blocked at apply, not silently executed.
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id={}, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["evidence"]["sample_purchases"] == 0.0
    assert plan["rotations"][0]["confidence"]["band"] == "abstain"
    plan["rotations"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert "insufficient data" in results[0].reason
    assert client.updates == []


def test_apply_rotation_drift_takes_precedence_over_grounding() -> None:
    # A thin-sample rotation (would be grounding-blocked) that is ALSO live-drifted reports the DRIFT
    # reason, not the grounding reason: the drift guard runs first because a stale plan must be
    # re-proposed regardless of band. Pins the drift-first ordering.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=9, spend=40)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["confidence"]["band"] == "abstain"  # would be grounding-blocked
    plan["rotations"][0]["status"] = "approved"
    adsets[0]["targeting"]["custom_audiences"] = [{"id": "Z"}]  # live drift after plan time
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert "Live included audiences changed" in results[0].reason  # drift reason, not grounding
    assert "insufficient data" not in (results[0].reason or "")
    assert client.updates == []


def test_apply_advantage_disable_structural_abstain_still_executes() -> None:
    # The Advantage-disable plan now sets requires_grounding, but each item is a STRUCTURAL abstain
    # (a safety toggle with no performance metric, no cited sample). The grounding gate must allow it:
    # an approved disable still executes and writes Advantage Audience off.
    adsets = [_adset("as1", "Set 1", ["A"], ["B", "C"], advantage=True)]
    plan = build_advantage_disable_plan(adsets, account_slug="demo", ad_account_id="act_1")
    assert plan["guardrails"]["requires_grounding"] is True
    assert plan["items"][0]["confidence"]["band"] == "abstain"  # structural abstain
    assert plan["items"][0]["evidence"]["sample_purchases"] is None
    plan["items"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_advantage_disable_plan(plan, client, execute=True)
    assert results[0].status == "executed"  # structural abstain is gate-allowed
    assert len(client.updates) == 1
    _adset_id, params, _validate_only = client.updates[0]
    assert params["targeting"]["targeting_automation"]["advantage_audience"] == 0


def test_apply_rotation_cited_above_floor_band_executes_through_gate() -> None:
    # The positive case the gate must NOT over-block: an approved rotation grounded on a real,
    # above-floor sample computes a non-abstain band, so op_grounding_gap returns None and the write
    # goes through. Without this, a gate bug that blocked every grounded rotation would slip by.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=120, spend=2400)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["confidence"]["band"] != "abstain"  # cited, above the floor
    plan["rotations"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    assert results[0].status == "executed"
    assert len(client.updates) == 1


def test_apply_rotation_blocks_approved_thin_sample_in_dry_run() -> None:
    # The grounding gate sits BEFORE the validate_only/dry-run branches (same placement as
    # apply_ops_plan), so a thin-sample approved rotation is blocked in a dry run too — it never even
    # reaches the would-write dry_run record. Pins gate-in-all-modes.
    adsets = _three_adset_partition()
    metrics = {a["id"]: _rotation_metric_row(a["id"], a["name"], purchases=9, spend=40)
               for a in adsets}
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=metrics, goal="roas", **_ROTATION_WINDOW)
    assert plan["rotations"][0]["confidence"]["band"] == "abstain"
    plan["rotations"][0]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=False)  # dry run, not execute
    assert results[0].status == "blocked"
    assert "insufficient data" in results[0].reason
    assert results[0].targeting is None  # blocked before compute_new_targeting
    assert client.updates == []


def test_apply_rotation_gate_isolates_per_item_blocked_does_not_stop_allowed() -> None:
    # A single plan mixing a structural-abstain rotation (no cited sample -> allowed) and a
    # cited-abstain rotation (thin sample -> blocked), both approved, in one apply call. The blocked
    # item must not abort the loop: the structural-abstain item still executes. Confirms the gate's
    # per-item continue isolates findings rather than failing the whole plan.
    adsets = _three_adset_partition()
    plan = build_rotation_plan(adsets, account_slug="demo", ad_account_id="act_1",
                               metrics_by_id=None, **_ROTATION_WINDOW)  # all structural abstains
    # Both start structural (allowed); promote the SECOND item to a cited thin sample (-> blocked).
    assert plan["rotations"][0]["evidence"]["sample_purchases"] is None  # structural
    plan["rotations"][1]["evidence"]["sample_purchases"] = 9.0
    plan["rotations"][1]["evidence"]["sample_spend"] = 40.0
    assert plan["rotations"][1]["confidence"]["band"] == "abstain"  # now cited abstain
    plan["rotations"][0]["status"] = "approved"
    plan["rotations"][1]["status"] = "approved"
    client = _FakeClient(adsets)
    results = apply_rotation_plan(plan, client, execute=True)
    by_id = {r.adset_id: r for r in results}
    assert by_id[plan["rotations"][0]["adset_id"]].status == "executed"  # structural -> allowed
    assert by_id[plan["rotations"][1]["adset_id"]].status == "blocked"   # cited thin -> blocked
    assert "insufficient data" in by_id[plan["rotations"][1]["adset_id"]].reason
    # Only the allowed item was written; the blocked one sent nothing.
    assert [u[0] for u in client.updates] == [plan["rotations"][0]["adset_id"]]


# --- Control layer (inspect + guarded ops + enable-ads) ----------------------

from meta_ads_analysis.control import (
    apply_ops_plan,
    build_account_snapshot,
    build_enable_ads_plan,
    validate_op,
)


class _ControlFakeClient:
    """Fake client for control-layer tests: campaigns/adsets/ads + updates."""

    def __init__(self, campaigns, adsets, ads, insights=None):
        self._campaigns = campaigns
        self._adsets = adsets
        self._ads = ads
        self._insights = insights or []
        self._by_id = {e["id"]: e for e in campaigns + adsets + ads}
        self.updates = []

    def list_campaigns(self, ad_account_id, *, fields, effective_status=None):
        return self._campaigns

    def list_adsets(self, ad_account_id, *, fields, effective_status=None):
        return self._adsets

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level, time_increment=1, breakdowns=None):
        return self._insights

    def iter_paginated(self, path, *, params=None):
        return list(self._ads)

    def get_ad(self, node_id, *, fields):
        return self._by_id[node_id]

    def get_adset(self, node_id, *, fields):
        return self._by_id[node_id]

    def get_campaign(self, node_id, *, fields):
        return self._by_id[node_id]

    def update_ad(self, node_id, *, params, validate_only=False):
        self.updates.append(("ad", node_id, params, validate_only))
        return {"id": node_id, "success": True}

    def update_adset(self, node_id, *, params, validate_only=False):
        self.updates.append(("adset", node_id, params, validate_only))
        return {"id": node_id, "success": True}

    def update_campaign(self, node_id, *, params, validate_only=False):
        self.updates.append(("campaign", node_id, params, validate_only))
        return {"id": node_id, "success": True}


def _control_fixture():
    campaigns = [{"id": "c1", "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE"}]
    adsets = [
        {
            "id": "as1", "name": "Set 1", "status": "ACTIVE", "effective_status": "ACTIVE",
            "campaign_id": "c1", "daily_budget": "10000",
            "targeting": {
                "custom_audiences": [{"id": "A", "name": "aud-A"}],
                "excluded_custom_audiences": [{"id": "B", "name": "aud-B"}],
                "targeting_automation": {"advantage_audience": 1},
            },
        }
    ]
    ads = [
        {"id": "ad1", "name": "Winner", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "as1", "issues_info": []},
        {
            "id": "ad2", "name": "Blocked", "status": "PAUSED", "effective_status": "WITH_ISSUES", "adset_id": "as1",
            "issues_info": [{"error_summary": "Ads creative post was created by an app that is in development mode"}],
        },
    ]
    return _ControlFakeClient(campaigns, adsets, ads)


def test_build_account_snapshot_assembles_tree_and_rollup() -> None:
    client = _control_fixture()
    snap = build_account_snapshot(client, "act_1")
    assert snap["rollup"] == {
        "campaigns": 1, "adsets": 1, "ads": 2, "active_ads": 1,
        "ads_with_issues": 1, "adsets_with_advantage_audience": 1,
    }
    adset = snap["campaigns"][0]["adsets"][0]
    assert adset["advantage_audience"] is True
    assert adset["included_audiences"] == ["aud-A"]
    assert len(adset["ads"]) == 2
    assert snap["ads_with_issues"][0]["ad_name"] == "Blocked"


def test_validate_op_enforces_guardrails() -> None:
    validate_op({"op_id": "x", "op": "set_status", "level": "ad", "id": "ad1", "params": {"status": "ACTIVE"}})
    # bad status
    try:
        validate_op({"op_id": "x", "op": "set_status", "level": "ad", "id": "ad1", "params": {"status": "DELETED"}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # AI param blocked
    try:
        validate_op({"op_id": "x", "op": "rename", "level": "adset", "id": "as1", "params": {"name": "advantage_plus_on"}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # budget at wrong level
    try:
        validate_op({"op_id": "x", "op": "set_daily_budget", "level": "ad", "id": "ad1", "params": {"daily_budget_cents": 100}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_apply_ops_enable_ad_and_budget_cap() -> None:
    client = _control_fixture()
    plan = {
        "ops": [
            {"op_id": "enable", "op": "set_status", "level": "ad", "id": "ad2", "params": {"status": "ACTIVE"}, "status": "approved"},
            {"op_id": "bump", "op": "set_daily_budget", "level": "adset", "id": "as1",
             "params": {"daily_budget_cents": 11000, "max_increase_percent": 20}, "status": "approved"},
            {"op_id": "overbump", "op": "set_daily_budget", "level": "adset", "id": "as1",
             "params": {"daily_budget_cents": 13000, "max_increase_percent": 20}, "status": "approved"},
            {"op_id": "notapproved", "op": "set_status", "level": "ad", "id": "ad1", "params": {"status": "PAUSED"}, "status": "proposed"},
        ]
    }
    results = apply_ops_plan(plan, client, execute=True)
    by_id = {r.op_id: r for r in results}
    assert by_id["enable"].status == "executed"
    assert by_id["bump"].status == "executed"  # within 20% (10000 -> 11000)
    assert by_id["overbump"].status == "blocked"  # 13000 > 12000 cap
    assert by_id["notapproved"].status == "skipped"
    # only the two valid approved writes hit the client
    assert ("ad", "ad2", {"status": "ACTIVE"}, False) in client.updates
    assert ("adset", "as1", {"daily_budget": "11000"}, False) in client.updates


def test_build_enable_ads_plan_targets_only_inactive_ads() -> None:
    client = _control_fixture()
    plan = build_enable_ads_plan(client, "act_1", account_slug="demo")
    assert plan["intent"] == "enable_ads"
    assert [op["id"] for op in plan["ops"]] == ["ad2"]  # only the inactive one
    assert plan["ops"][0]["params"] == {"status": "ACTIVE"}
    assert "development mode" in plan["ops"][0]["note"]


# --- Enable / set_status grounding (evidence + confidence + review on enable ops) ----


def _enable_client(insights):
    """_control_fixture (ad2 is the PAUSED/inactive ad) plus seeded ad-level insights rows."""
    base = _control_fixture()
    return _ControlFakeClient(base._campaigns, base._adsets, base._ads, insights=insights)


def test_enable_ads_paused_ad_with_strong_sample_carries_computed_band() -> None:
    # A high-spend ad that is currently paused, proposed for enable, carries the band the rubric
    # COMPUTES from its own window sample — never a free-typed number.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "500"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(_enable_client(insights), "act_1", policy={"primary_goal": "roas"})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 30.0
    # 25 <= 30 < 100 purchases, recent window, direct_observation → medium (matches confidence.assess).
    assert op["confidence"]["band"] == "medium"
    assert op["review"]["verdict"] == "stands"
    assert plan["guardrails"]["requires_grounding"] is True


def test_enable_ads_cold_ad_abstains_and_gate_blocks_turn_on() -> None:
    # A cold ad (no recent insights) cites a ZERO sample → abstains → review marks it insufficient,
    # and the apply-time grounding gate refuses to turn it on even when approved (keep observing).
    plan = build_enable_ads_plan(_enable_client([]), "act_1", policy={"primary_goal": "roas"})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["confidence"]["band"] == "abstain"
    assert op["confidence"]["data_band"] == "abstain"
    assert op["evidence"]["sample_spend"] == 0.0  # honest "zero recent delivery" — a cited sample
    assert op["review_verdict"] == "insufficient"
    # Operator approves anyway → the gate blocks the write.
    op["status"] = "approved"
    results = apply_ops_plan(plan, _enable_client([]), execute=False)
    blocked = next(r for r in results if r.op_id == op["op_id"])
    assert blocked.status == "blocked"
    assert "insufficient data" in (blocked.reason or "").lower()


def test_enable_ads_thin_new_ad_abstains_so_go_live_is_a_reviewed_step() -> None:
    # A freshly-authored (PAUSED) ad has thin data; enabling it is the go-live path and must be a
    # conscious, reviewed step — not an auto-confident enable. Below-floor sample → abstain.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "40",
        "action_values": [{"action_type": "purchase", "value": "60"}],
        "actions": [{"action_type": "purchase", "value": "3"}],
    }]
    plan = build_enable_ads_plan(_enable_client(insights), "act_1", policy={"primary_goal": "roas"})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["confidence"]["band"] == "abstain"  # $40 / 3 purchases below the significance floor
    assert op["review_verdict"] == "insufficient"


def test_enable_ads_install_goal_grounds_on_cost_per_install() -> None:
    # An install-goal account grounds the enable on cost-per-install (the goal metric, mirroring
    # actions._select_action_metric) AND grounds the significance sample on app_installs (the conversion
    # behind that metric) — so a real install volume backs the band instead of being pinned at low by a
    # purchases≈0 sample the account never produces (the confidence-install-goal-significance-ops fix).
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "actions": [{"action_type": "mobile_app_install", "value": "40"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "maximize_in_app_subscriptions"}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["metric_value"] == 12.5  # $500 / 40 installs
    assert op["evidence"]["sample_purchases"] == 40.0  # the install count grounds the sample, not purchases
    # 25 <= 40 < 100 installs, recent window, direct_observation → medium (pre-fix this read low because
    # the sample was pinned to ~0 purchases).
    assert op["confidence"]["band"] == "medium"
    assert op["review"]["verdict"] == "stands"


def test_review_ops_plan_demotes_overclaimed_enable() -> None:
    from meta_ads_analysis.review import review_ops_plan

    op = {
        "op_id": "enable_ad_x", "op": "set_status", "level": "ad", "id": "adx", "status": "approved",
        "params": {"status": "ACTIVE"},
        "evidence": {"metric_name": "blended_roas", "metric_value": 1.0,
                     "window": "2026-06-10..2026-06-24", "sample_purchases": 30.0,
                     "sample_spend": 500.0, "entity_level": "ad", "entity_id": "adx"},
        "confidence": {"band": "high", "data_band": "high", "grounding_band": "high",
                       "grounding_tier": "direct_observation", "factors": [], "would_raise": "",
                       "would_lower": "", "causal_flag": False},
    }
    plan = {"run_date": "2026-06-24", "ops": [op], "guardrails": {"requires_grounding": True}}
    reviewed = review_ops_plan(plan, spend_floor=100.0)
    r = reviewed["ops"][0]
    assert r["review"]["verdict"] == "downgrade"  # 30 purchases supports medium, not the claimed high
    assert Band[r["confidence"]["band"]] < Band.high
    assert r["review_verdict"] == "downgrade"
    assert "review" not in plan["ops"][0]  # input plan not mutated


def test_enable_ads_below_target_roas_strong_sample_is_refuted() -> None:
    # Enabling an ad is directionally a scale-up (0 -> live). A re-enable whose own cited ROAS (1.0)
    # sits below the account target (2.0) on a statistically STRONG sample is turning a known loser back
    # on — the gate REFUTES it (the same verdict a below-target budget scale-up gets) so it reaches the
    # operator named as a loser, not dressed up as a genuine performer. The refutation is a warning, not
    # a band-cap: the computed band is left intact (still medium for 30 purchases).
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "500"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_value"] == 1.0  # $500 revenue / $500 spend
    assert op["review"]["verdict"] == "refuted"
    assert "direction" in op["review"]["failed_inputs"]
    reason = " ".join(op["review"]["reasons"])
    assert "enabling" in reason and "1.00" in reason and "2 target" in reason
    assert op["review_verdict"] == "refuted"
    assert op["confidence"]["band"] == "medium"  # refuted is a warning, not a band-cap


def test_enable_ads_exactly_at_target_roas_stands() -> None:
    # Boundary: ROAS (2.0) sits EXACTLY at target (2.0). The enable branch uses a strict `<` (the
    # scale-up convention), so an at-target re-enable is NOT refuted — it stands. Pins the intentional
    # strict-`<` semantics so a future `<=` slip can't silently start refuting break-even re-enables.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "1000"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_value"] == 2.0  # $1000 / $500 — exactly at the 2.0 target
    assert op["review"]["verdict"] == "stands"  # strict `<`: at-target is not below-target
    assert "direction" not in op["review"]["failed_inputs"]


def test_enable_ads_above_target_roas_stands() -> None:
    # Same policy, but the cited ROAS (5.0) is comfortably above target — a genuine performer. The
    # direction rule does NOT fire; the band is computed from the sample and the call stands.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "2500"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_value"] == 5.0  # $2500 / $500
    assert op["review"]["verdict"] == "stands"
    assert op["confidence"]["band"] == "medium"


def test_enable_ads_roas_goal_without_target_does_not_refute() -> None:
    # Guard pin: with a ROAS goal but NO target_roas configured, the direction rule cannot fire — a
    # below-1.0 ROAS still stands. This is the guard that keeps the existing computed-band tests green;
    # pin it so a future refactor can't silently start refuting when no target is set.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "500"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(_enable_client(insights), "act_1", policy={"primary_goal": "roas"})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_value"] == 1.0
    assert op["review"]["verdict"] == "stands"


def test_enable_ads_install_goal_no_cost_target_not_direction_refuted() -> None:
    # Guard pin (NOT a blanket "ROAS-only" deferral): an install-goal enable is judged on
    # cost-per-install, but this policy carries only a target_roas — no
    # secondary_cost_per_app_install_target — so the cost-polarity direction check has no target to fire
    # against and the enable stands. The companion test below pins the refute path when a cost target IS
    # configured. (The band is medium here because the install sample now grounds significance — see
    # test_enable_ads_install_goal_grounds_on_cost_per_install — not refute-related.)
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "actions": [{"action_type": "mobile_app_install", "value": "40"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1",
        policy={"primary_goal": "maximize_in_app_subscriptions", "target_roas": 2.0},
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["review"]["verdict"] != "refuted"
    assert op["review"]["verdict"] == "stands"
    assert op["confidence"]["band"] == "medium"


def test_enable_ads_install_goal_zero_purchases_real_installs_clears_floor() -> None:
    # The core fix: an install-goal account almost never has purchases. With purchases=0 but real
    # install volume the enable's significance sample is the install count, so the band clears
    # `low`/abstain instead of being structurally pinned there. The 0-purchase decoy is ignored.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "0"}],
        "actions": [
            {"action_type": "purchase", "value": "0"},
            {"action_type": "mobile_app_install", "value": "120"},  # 120 >= 4 * 25 conversions floor
        ],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "maximize_in_app_subscriptions"}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["sample_purchases"] == 120.0  # installs ground the sample, not the 0 purchases
    assert Band[op["confidence"]["band"]] > Band.low  # 120 installs, recent window → clears low/abstain


def test_enable_ads_roas_goal_ignores_app_installs_decoy() -> None:
    # Parity guard: on a ROAS goal the significance sample stays purchases. An app_installs decoy in the
    # row must be ignored — byte-identical to a purchases-only fixture (band still medium for 30).
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "500"}],
        "actions": [
            {"action_type": "purchase", "value": "30"},
            {"action_type": "mobile_app_install", "value": "999"},  # decoy — must be ignored
        ],
    }]
    plan = build_enable_ads_plan(_enable_client(insights), "act_1", policy={"primary_goal": "roas"})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 30.0  # purchases, NOT the 999 install decoy
    assert op["confidence"]["band"] == "medium"


def test_enable_ads_install_goal_cold_ad_abstains_and_gate_blocks() -> None:
    # The cold-ad boundary holds for install goals too: no insights row routes through the cold branch
    # (untouched by the goal-aware selector) → cited ZERO sample → abstain → the apply-time gate refuses
    # to turn the ad on even when approved (you cannot confidently enable with no delivery evidence).
    plan = build_enable_ads_plan(
        _enable_client([]), "act_1", policy={"primary_goal": "maximize_in_app_subscriptions"}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["confidence"]["band"] == "abstain"
    assert op["evidence"]["sample_purchases"] == 0.0  # cited zero — the cold branch, not the helper
    assert op["evidence"]["sample_spend"] == 0.0
    op["status"] = "approved"
    results = apply_ops_plan(plan, _enable_client([]), execute=False)
    blocked = next(r for r in results if r.op_id == op["op_id"])
    assert blocked.status == "blocked"
    assert "insufficient data" in (blocked.reason or "").lower()


def test_enable_ads_no_goal_installs_present_keeps_sample_on_purchases() -> None:
    # Intentional asymmetry (parity with actions._select_sample_conversions): with NO goal set,
    # _status_metric falls through to cost_per_app_install when installs are present, but the
    # significance sample stays on purchases — the selector keys ONLY on the explicit install-goal
    # string. Metric and sample can legitimately disagree here; this pins that it is NOT "fixed".
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "actions": [
            {"action_type": "purchase", "value": "30"},
            {"action_type": "mobile_app_install", "value": "40"},
        ],
    }]
    plan = build_enable_ads_plan(_enable_client(insights), "act_1", policy={})
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_name"] == "cost_per_app_install"  # no goal + installs present
    assert op["evidence"]["sample_purchases"] == 30.0  # but the sample stays on purchases


def test_attach_status_grounding_none_row_ignores_sample_conversions() -> None:
    # Both `metrics_row is None` branches must be UNTOUCHED by the goal-aware sample selector: a
    # structural pause (cold_cites_zero=False) cites NO sample; a cold enable (cold_cites_zero=True)
    # cites zero — regardless of whatever sample_conversions value the call site passes.
    from meta_ads_analysis.control import _attach_status_grounding

    ad = {"id": "ad9", "name": "X"}
    for cites_zero, expected in [(False, None), (True, 0.0)]:
        samples = []
        for sc in (None, 0.0, 999.0):
            op: dict[str, object] = {}
            _attach_status_grounding(
                op, ad, None,
                metric_name="cost_per_app_install", metric_value=None,
                metric_display="cost/install n/a", sample_conversions=sc,
                account_slug="demo", date_from="2026-06-01", date_to="2026-06-24",
                recency_days=1, cold_cites_zero=cites_zero,
            )
            samples.append(op["evidence"]["sample_purchases"])
        assert samples == [expected, expected, expected]  # sample_conversions had no effect


def test_enable_ads_install_goal_above_cost_target_is_refuted() -> None:
    # The install-goal enable-op path with a cost target configured: re-enabling an ad whose computed
    # cost/install ($12.50 = $500 / 40 installs) sits ABOVE the $3 target is turning a known loser back
    # on (the cost-polarity mirror of a below-target ROAS re-enable). The gate REFUTES it with an
    # "enabling" reason. Refuted is a warning, not a band-cap — the computed band (medium, now that the
    # 40-install sample grounds significance) is left intact.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "actions": [{"action_type": "mobile_app_install", "value": "40"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1",
        policy={
            "primary_goal": "maximize_in_app_subscriptions",
            "secondary_cost_per_app_install_target": 3.0,
        },
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["metric_value"] == 12.5  # $500 / 40 installs
    assert op["review"]["verdict"] == "refuted"
    assert "direction" in op["review"]["failed_inputs"]
    reason = " ".join(op["review"]["reasons"])
    assert "enabling" in reason and "12.50" in reason and "$3 target" in reason
    assert op["review_verdict"] == "refuted"
    assert op["confidence"]["band"] == "medium"  # refuted is a warning, not a band-cap


def test_enable_ads_install_goal_at_target_cost_enable_stands() -> None:
    # Strict-boundary: cost/install == target ($120 / 40 installs = $3.00, target $3.00).
    # The branch uses cost > target (strict), so landing exactly on the threshold must stand.
    # Guards against a future > → >= slip that would silently start refuting cheap re-enables.
    insights = [{
        "ad_id": "ad2", "ad_name": "Exact", "spend": "120",
        "actions": [{"action_type": "mobile_app_install", "value": "40"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1",
        policy={
            "primary_goal": "maximize_in_app_subscriptions",
            "secondary_cost_per_app_install_target": 3.0,
        },
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["metric_value"] == 3.0  # $120 / 40 installs
    assert op["review"]["verdict"] != "refuted"
    assert op["review"]["verdict"] == "stands"
    assert "direction" not in op["review"]["failed_inputs"]


def test_enable_ads_install_goal_below_target_cost_enable_stands() -> None:
    # Clear winner: cost/install ($2.00 = $80 / 40 installs) is well below the $3.00 target.
    # A genuinely cheap re-enable must not be flagged — the direction gate must not fire.
    insights = [{
        "ad_id": "ad2", "ad_name": "Winner", "spend": "80",
        "actions": [{"action_type": "mobile_app_install", "value": "40"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1",
        policy={
            "primary_goal": "maximize_in_app_subscriptions",
            "secondary_cost_per_app_install_target": 3.0,
        },
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["metric_value"] == 2.0  # $80 / 40 installs
    assert op["review"]["verdict"] != "refuted"
    assert op["review"]["verdict"] == "stands"
    assert "direction" not in op["review"]["failed_inputs"]


def test_enable_ads_cold_ad_with_target_stays_insufficient_not_refuted() -> None:
    # A cold ad cites a ZERO sample → metric_value is None → the direction rule can't fire even with a
    # target configured. It must stay `insufficient` (keep observing), never flip to `refuted`, and the
    # apply gate still blocks the approved turn-on.
    plan = build_enable_ads_plan(
        _enable_client([]), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["action_type"] == "enable_ad"
    assert op["confidence"]["band"] == "abstain"
    assert op["review_verdict"] == "insufficient"
    op["status"] = "approved"
    results = apply_ops_plan(plan, _enable_client([]), execute=False)
    blocked = next(r for r in results if r.op_id == op["op_id"])
    assert blocked.status == "blocked"


def test_enable_ads_below_target_and_below_floor_is_insufficient_not_refuted() -> None:
    # Most-conservative-wins for enables: a below-target ROAS (1.0 < 2.0) on a BELOW-FLOOR sample yields
    # `insufficient` (rank 3) over `refuted` (rank 2). The apply gate still blocks the approved turn-on.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "40",
        "action_values": [{"action_type": "purchase", "value": "40"}],
        "actions": [{"action_type": "purchase", "value": "3"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["evidence"]["metric_value"] == 1.0  # below target, but the sample is below the floor
    assert op["review"]["verdict"] == "insufficient"
    assert op["review_verdict"] == "insufficient"
    op["status"] = "approved"
    results = apply_ops_plan(plan, _enable_client(insights), execute=False)
    blocked = next(r for r in results if r.op_id == op["op_id"])
    assert blocked.status == "blocked"


def test_enable_ads_refuted_can_still_be_operator_approved() -> None:
    # `refuted` is a loud warning, not a hard block for ops: the apply gate keys on grounding
    # (op_grounding_gap), not on review_verdict. An operator who genuinely wants the retest can set the
    # refuted op to `approved` and it is NOT blocked (its band is grounded/non-abstain) — proving the
    # refutation refuses to PRESENT a loser as a performer without trapping deliberate operator intent.
    insights = [{
        "ad_id": "ad2", "ad_name": "Blocked", "spend": "500",
        "action_values": [{"action_type": "purchase", "value": "500"}],
        "actions": [{"action_type": "purchase", "value": "30"}],
    }]
    plan = build_enable_ads_plan(
        _enable_client(insights), "act_1", policy={"primary_goal": "roas", "target_roas": 2.0}
    )
    op = next(o for o in plan["ops"] if o["id"] == "ad2")
    assert op["review_verdict"] == "refuted"
    assert op["confidence"]["band"] != "abstain"  # grounded — the gate has nothing to block on
    op["status"] = "approved"
    results = apply_ops_plan(plan, _enable_client(insights), execute=False)
    result = next(r for r in results if r.op_id == op["op_id"])
    assert result.status == "dry_run"  # NOT blocked — refusal is a warning, not a gate


def test_enable_ads_review_is_idempotent() -> None:
    from meta_ads_analysis.review import review_ops_plan

    plan = build_enable_ads_plan(_enable_client([]), "act_1", policy={"primary_goal": "roas"})
    again = review_ops_plan(plan)
    assert [o["confidence"]["band"] for o in again["ops"]] == [o["confidence"]["band"] for o in plan["ops"]]
    assert again["ops"][0]["review"] == plan["ops"][0]["review"]


# --- Metrics / diagnose / audiences / pause ---------------------------------

from meta_ads_analysis.control import (
    build_pause_plan,
    fetch_entity_metrics,
    list_account_audiences,
    scan_issues,
)


class _MetricsFakeClient:
    def __init__(self, insights=None, ads=None, audiences=None):
        self._insights = insights or []
        self._ads = ads or []
        self._audiences = audiences or []

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level, time_increment=1, breakdowns=None):
        return self._insights

    def iter_paginated(self, path, *, params=None):
        return list(self._ads)

    def list_custom_audiences(self, ad_account_id, *, fields):
        return self._audiences


def test_fetch_entity_metrics_computes_roas_and_sorts() -> None:
    insights = [
        {"adset_id": "as1", "adset_name": "Cheap", "spend": "100",
         "action_values": [{"action_type": "purchase", "value": "300"}],
         "actions": [{"action_type": "purchase", "value": "6"}], "impressions": "1000"},
        {"adset_id": "as2", "adset_name": "Big", "spend": "500",
         "action_values": [{"action_type": "purchase", "value": "750"}],
         "actions": [{"action_type": "purchase", "value": "5"}], "impressions": "9000"},
    ]
    client = _MetricsFakeClient(insights=insights)
    rows = fetch_entity_metrics(client, "act_1", level="adset", date_from="2026-06-01", date_to="2026-06-30")
    assert rows[0]["name"] == "Big"  # sorted by spend desc
    assert rows[0]["roas"] == 1.5
    assert rows[1]["name"] == "Cheap"
    assert rows[1]["roas"] == 3.0
    assert rows[1]["cost_per_purchase"] == round(100 / 6, 2)


def test_scan_issues_groups_by_summary() -> None:
    ads = [
        {"id": "1", "name": "A", "effective_status": "WITH_ISSUES",
         "issues_info": [{"error_summary": "dev mode"}]},
        {"id": "2", "name": "B", "effective_status": "WITH_ISSUES",
         "issues_info": [{"error_summary": "dev mode"}]},
        {"id": "3", "name": "C", "effective_status": "ACTIVE", "issues_info": []},
    ]
    scan = scan_issues(_MetricsFakeClient(ads=ads), "act_1")
    assert scan["ads_scanned"] == 3
    assert scan["ads_with_issues"] == 2
    assert scan["by_issue"]["dev mode"]["count"] == 2


def test_list_account_audiences_normalizes() -> None:
    auds = [{"id": "a1", "name": "HV", "subtype": "CUSTOM",
             "approximate_count_lower_bound": 1000, "approximate_count_upper_bound": 2000,
             "operation_status": {"code": 200, "description": "Normal"}}]
    out = list_account_audiences(_MetricsFakeClient(audiences=auds), "act_1")
    assert out[0]["name"] == "HV"
    assert out[0]["size_lower"] == 1000
    assert out[0]["status"] == "Normal"


def test_build_pause_plan_selects_underperformers_by_roas() -> None:
    ads = [
        {"id": "ad1", "name": "Loser", "effective_status": "ACTIVE", "adset_id": "as1", "issues_info": []},
        {"id": "ad2", "name": "Winner", "effective_status": "ACTIVE", "adset_id": "as1", "issues_info": []},
        {"id": "ad3", "name": "Paused already", "effective_status": "PAUSED", "adset_id": "as1", "issues_info": []},
    ]
    insights = [
        {"ad_id": "ad1", "ad_name": "Loser", "spend": "200",
         "action_values": [{"action_type": "purchase", "value": "200"}],
         "actions": [{"action_type": "purchase", "value": "4"}]},
        {"ad_id": "ad2", "ad_name": "Winner", "spend": "200",
         "action_values": [{"action_type": "purchase", "value": "800"}],
         "actions": [{"action_type": "purchase", "value": "10"}]},
    ]
    client = _MetricsFakeClient(ads=ads, insights=insights)
    plan = build_pause_plan(
        client, "act_1", roas_below=1.5, min_spend=100,
        date_from="2026-06-01", date_to="2026-06-30",
    )
    assert plan["intent"] == "pause_ads"
    ids = [op["id"] for op in plan["ops"]]
    assert ids == ["ad1"]  # only the active, below-floor, enough-spend ad
    assert plan["ops"][0]["params"] == {"status": "PAUSED"}


def test_pause_roas_below_carries_grounded_band() -> None:
    # A roas_below pause rests on ROAS by construction → the op cites that metric + a computed band.
    ads = [{"id": "ad1", "name": "Loser", "effective_status": "ACTIVE", "adset_id": "as1", "issues_info": []}]
    insights = [{"ad_id": "ad1", "ad_name": "Loser", "spend": "200",
                 "action_values": [{"action_type": "purchase", "value": "100"}],
                 "actions": [{"action_type": "purchase", "value": "4"}]}]
    client = _ControlFakeClient([], [], ads, insights=insights)
    plan = build_pause_plan(client, "act_1", roas_below=1.5, min_spend=100,
                            date_from="2026-06-01", date_to="2026-06-24", run_date="2026-06-25")
    op = next(o for o in plan["ops"] if o["id"] == "ad1")
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_spend"] == 200.0
    assert op["confidence"]["band"] != "abstain"  # spend cleared the floor → a real (low) band
    assert plan["guardrails"]["requires_grounding"] is True


def test_pause_structural_abstains_but_gate_allows_safety_pause() -> None:
    # A purely structural pause (no metric) cites NO sample → structural abstain → the apply-time gate
    # ALLOWS it (pausing is conservative; PAUSED-by-default safety writes must not be blocked).
    plan = build_pause_plan(_control_fixture(), "act_1")  # all ACTIVE ads, no perf rule
    op = next(o for o in plan["ops"] if o["id"] == "ad1")
    assert op["confidence"]["band"] == "abstain"
    assert op["evidence"]["sample_spend"] is None  # structural — nothing cited
    op["status"] = "approved"
    results = apply_ops_plan(plan, _control_fixture(), execute=False)
    res = next(r for r in results if r.op_id == op["op_id"])
    assert res.status == "dry_run"  # allowed, not blocked


def test_pause_roas_below_grounds_on_purchases_regardless_of_installs() -> None:
    # A roas_below pause was SELECTED by ROAS, so its cited sample stays purchases (agreeing with the
    # hardcoded blended_roas metric) — the goal-aware install selector must NOT switch this path to
    # installs. An app_installs decoy in the row is ignored.
    ads = [{"id": "ad1", "name": "Loser", "effective_status": "ACTIVE", "adset_id": "as1", "issues_info": []}]
    insights = [{"ad_id": "ad1", "ad_name": "Loser", "spend": "200",
                 "action_values": [{"action_type": "purchase", "value": "100"}],
                 "actions": [
                     {"action_type": "purchase", "value": "4"},
                     {"action_type": "mobile_app_install", "value": "999"},  # decoy
                 ]}]
    client = _ControlFakeClient([], [], ads, insights=insights)
    plan = build_pause_plan(client, "act_1", roas_below=1.5, min_spend=100,
                            date_from="2026-06-01", date_to="2026-06-24", run_date="2026-06-25")
    op = next(o for o in plan["ops"] if o["id"] == "ad1")
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 4.0  # purchases, NOT the 999 install decoy


# --- Authoring (create / duplicate / lookalike) + breakdowns + account-info --

from meta_ads_analysis.authoring import (
    apply_authoring_plan,
    build_duplicate_ad_plan,
    build_lookalike_plan,
    validate_authoring_op,
)
from meta_ads_analysis.control import account_info, fetch_breakdown_metrics


class _AuthoringFakeClient:
    def __init__(self, ad_creative_id="cr1", insights=None):
        self._creative_id = ad_creative_id
        self._insights = insights or []
        self.creates = []

    def get_ad(self, ad_id, *, fields):
        return {"id": ad_id, "name": "Source Ad", "creative": {"id": self._creative_id}}

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level="ad",
                       time_increment=1, breakdowns=None):
        # Source-ad metric read for the duplicate builder's grounding. Default: no delivery
        # (empty) → the duplicate cites a zero sample → abstains.
        return self._insights

    def create_campaign(self, ad_account_id, *, params, validate_only=False):
        self.creates.append(("campaign", params, validate_only))
        return {"id": "new_camp"}

    def create_adset(self, ad_account_id, *, params, validate_only=False):
        self.creates.append(("adset", params, validate_only))
        return {"id": "new_adset"}

    def create_ad(self, ad_account_id, *, params, validate_only=False):
        self.creates.append(("ad", params, validate_only))
        return {"id": "new_ad"}

    def create_custom_audience(self, ad_account_id, *, params, validate_only=False):
        self.creates.append(("audience", params, validate_only))
        return {"id": "new_lal"}


def test_validate_authoring_op_guards() -> None:
    validate_authoring_op({"kind": "create_campaign", "params": {"name": "C", "objective": "OUTCOME_SALES"}})
    for bad in [
        {"kind": "create_campaign", "params": {"name": "C"}},  # missing objective
        {"kind": "create_ad", "params": {"name": "A", "adset_id": "as1"}},  # missing creative
        {"kind": "create_lookalike", "params": {"name": "L", "origin_audience_id": "a1", "country": "US", "ratio": 0.5}},  # ratio out of range
        {"kind": "create_campaign", "params": {"name": "advantage_audience_on", "objective": "X"}},  # AI param
        {"kind": "create_widget", "params": {}},  # unknown kind
    ]:
        try:
            validate_authoring_op(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_apply_authoring_forces_paused_and_records_created_ids() -> None:
    client = _AuthoringFakeClient()
    plan = {
        "ad_account_id": "act_1",
        "ops": [
            {"op_id": "c", "kind": "create_campaign", "params": {"name": "C", "objective": "OUTCOME_SALES"}, "status": "approved"},
            {"op_id": "a", "kind": "create_ad", "params": {"name": "A", "adset_id": "as1", "creative": {"creative_id": "cr1"}}, "status": "approved"},
            {"op_id": "skip", "kind": "create_campaign", "params": {"name": "D", "objective": "X"}, "status": "proposed"},
        ],
    }
    results = apply_authoring_plan(plan, client, execute=True)
    by_id = {r.op_id: r for r in results}
    assert by_id["c"].status == "created" and by_id["c"].created_id == "new_camp"
    assert by_id["a"].status == "created"
    assert by_id["skip"].status == "skipped"
    # both creates forced PAUSED
    for kind, params, _vo in client.creates:
        assert params["status"] == "PAUSED"


def test_build_duplicate_ad_plan_reuses_source_creative() -> None:
    client = _AuthoringFakeClient(ad_creative_id="cr-99")
    plan = build_duplicate_ad_plan(client, "act_1", source_ad_id="ad1", target_adset_id="as2")
    op = plan["ops"][0]
    assert op["kind"] == "create_ad"
    assert op["params"]["creative"] == {"creative_id": "cr-99"}
    assert op["params"]["adset_id"] == "as2"


def test_build_lookalike_plan_shape() -> None:
    plan = build_lookalike_plan("act_1", name="LAL 2%", origin_audience_id="a1", country="US", ratio=0.02)
    op = plan["ops"][0]
    assert op["kind"] == "create_lookalike"
    assert op["params"]["ratio"] == 0.02


def test_fetch_breakdown_metrics_segments_and_roas() -> None:
    insights = [
        {"age": "25-34", "spend": "100", "action_values": [{"action_type": "purchase", "value": "400"}], "actions": [{"action_type": "purchase", "value": "8"}]},
        {"age": "35-44", "spend": "200", "action_values": [{"action_type": "purchase", "value": "200"}], "actions": [{"action_type": "purchase", "value": "3"}]},
    ]
    client = _MetricsFakeClient(insights=insights)
    rows = fetch_breakdown_metrics(client, "act_1", breakdown="age", date_from="2026-06-01", date_to="2026-06-30")
    assert rows[0]["segment"] == {"age": "35-44"}  # sorted by spend desc
    assert rows[1]["roas"] == 4.0


def test_account_info_maps_status_code() -> None:
    class C:
        def get_account(self, ad_account_id, *, fields):
            return {"name": "Acct", "account_status": 1, "currency": "USD",
                    "funding_source_details": {"display_string": "Visa ****1234"}}
    info = account_info(C(), "act_1")
    assert info["status"] == "ACTIVE"
    assert info["funding_source"] == "Visa ****1234"


# --- Targeting ops + estimate / interest search / pixels --------------------

from meta_ads_analysis.control import (
    estimate_adset_audience,
    list_account_pixels,
    search_interests,
    validate_op as _validate_op,
)


def test_targeting_ops_validation() -> None:
    _validate_op({"op_id": "x", "op": "set_age_range", "level": "adset", "id": "as1", "params": {"age_min": 25, "age_max": 45}})
    for bad in [
        {"op_id": "x", "op": "set_age_range", "level": "adset", "id": "as1", "params": {"age_min": 50, "age_max": 30}},
        {"op_id": "x", "op": "set_genders", "level": "adset", "id": "as1", "params": {"genders": [3]}},
        {"op_id": "x", "op": "set_geo_locations", "level": "adset", "id": "as1", "params": {"geo_locations": {}}},
        {"op_id": "x", "op": "set_placements", "level": "adset", "id": "as1", "params": {}},
        {"op_id": "x", "op": "set_age_range", "level": "campaign", "id": "c1", "params": {"age_min": 18, "age_max": 65}},
    ]:
        try:
            _validate_op(bad)
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_apply_targeting_ops_read_modify_write_preserves_other_fields() -> None:
    adsets = [_adset("as1", "Set 1", ["A"], ["B"], advantage=True)]  # has geo, age_min, automation
    plan = {
        "ops": [
            {"op_id": "age", "op": "set_age_range", "level": "adset", "id": "as1", "params": {"age_min": 30, "age_max": 50}, "status": "approved"},
            {"op_id": "place", "op": "set_placements", "level": "adset", "id": "as1", "params": {"automatic": True}, "status": "approved"},
        ]
    }
    client = _FakeClient(adsets)
    results = apply_ops_plan(plan, client, execute=True)
    assert {r.status for r in results} == {"executed"}
    sent = {adset_id: params for adset_id, params, _vo in client.updates}
    # both ops re-POST the full targeting object
    age_t = client.updates[0][1]["targeting"]
    assert age_t["age_min"] == 30 and age_t["age_max"] == 50
    assert age_t["geo_locations"] == {"countries": ["US"]}  # preserved
    assert age_t["custom_audiences"] == [{"id": "A", "name": "aud-A"}]  # untouched
    assert age_t["targeting_automation"] == {"advantage_audience": 1}  # never modified by targeting ops


def test_estimate_and_search_and_pixels_normalize() -> None:
    class C:
        def get_delivery_estimate(self, adset_id, *, fields):
            return {"data": [{"estimate_ready": True, "estimate_mau_lower_bound": 100000, "estimate_mau_upper_bound": 200000, "estimate_dau": 5000}]}

        def search_targeting(self, *, query, search_type="adinterest", limit=25):
            return [{"id": "6003", "name": "Jewelry", "audience_size_lower_bound": 1000, "audience_size_upper_bound": 2000, "topic": "Shopping"}]

        def list_pixels(self, ad_account_id, *, fields):
            return [{"id": "px1", "name": "Main Pixel", "last_fired_time": "2026-06-20", "is_unavailable": False}]

    c = C()
    est = estimate_adset_audience(c, "as1")
    assert est["mau_lower"] == 100000 and est["mau_upper"] == 200000
    interests = search_interests(c, "jewelry")
    assert interests[0]["name"] == "Jewelry" and interests[0]["audience_lower"] == 1000
    pixels = list_account_pixels(c, "act_1")
    assert pixels[0]["name"] == "Main Pixel"


# --- Video pipeline foundation (Phase 0 + 1) --------------------------------

from types import SimpleNamespace

from meta_ads_analysis.authoring import build_video_ad_plan, validate_authoring_op as _validate_auth
from meta_ads_analysis import video_intake


def test_create_video_ad_builds_object_story_spec_and_pauses() -> None:
    plan = build_video_ad_plan(
        "act_1", name="My Video Ad", adset_id="as1", video_id="vid123", page_id="page9",
        message="Buy our jewelry", link="https://shop.example/x", title="Shiny", description="Handmade",
        call_to_action_type="SHOP_NOW",
    )
    op = plan["ops"][0]
    assert op["kind"] == "create_video_ad"
    _validate_auth(op)  # passes validation
    # Net-new video ad: no prior performance → abstain → review marks it insufficient.
    assert op["confidence"]["band"] == "abstain"
    assert op["review"]["verdict"] == "insufficient"

    # Approving + executing under requires_grounding is BLOCKED (conscious override required); the
    # net-new create with no evidence is never auto-sent.
    blocked_client = _AuthoringFakeClient()
    plan["ops"][0]["status"] = "approved"
    blocked = apply_authoring_plan(plan, blocked_client, execute=True)
    assert blocked[0].status == "blocked"
    assert blocked_client.creates == []  # nothing created

    # Conscious override: drop requires_grounding → the create is sent, forced PAUSED, right shape.
    plan["guardrails"]["requires_grounding"] = False
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(plan, client, execute=True)
    assert results[0].status == "created"
    kind, params, _vo = client.creates[0]
    assert kind == "ad"  # video ad is created via create_ad
    assert params["status"] == "PAUSED"
    spec = params["creative"]["object_story_spec"]
    assert spec["page_id"] == "page9"
    assert spec["video_data"]["video_id"] == "vid123"
    assert spec["video_data"]["message"] == "Buy our jewelry"
    assert spec["video_data"]["call_to_action"]["value"]["link"] == "https://shop.example/x"
    assert spec["video_data"]["title"] == "Shiny"


def test_create_video_ad_requires_core_fields() -> None:
    try:
        _validate_auth({"kind": "create_video_ad", "params": {"name": "X", "adset_id": "as1"}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_frame_timestamps_even_spacing() -> None:
    assert video_intake.frame_timestamps(100.0, 4) == [20.0, 40.0, 60.0, 80.0]
    assert video_intake.frame_timestamps(0, 4) == []
    assert video_intake.frame_timestamps(100, 0) == []


def test_process_video_builds_brief_with_injected_runner_and_transcriber(tmp_path: Path, monkeypatch) -> None:
    # Avoid requiring ffmpeg binaries during the test.
    monkeypatch.setattr(video_intake, "_require_binary", lambda name: None)

    calls = []

    def fake_runner(cmd, capture_output=False, text=False, check=False):
        calls.append(cmd[0])
        # ffprobe duration query returns a number on stdout
        if cmd[0] == "ffprobe":
            return SimpleNamespace(stdout="42.0\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def fake_transcriber(audio_path):
        return {"text": "Handmade jewelry for everyday wear", "language": "en", "duration": 42.0,
                "segments": [{"start": 0.0, "end": 5.0, "text": "Handmade jewelry for everyday wear"}]}

    video = tmp_path / "promo.mp4"
    video.write_bytes(b"not a real video")
    brief = video_intake.process_video(
        video, account_slug="divine_designs", work_dir=tmp_path / "work",
        frame_count=3, runner=fake_runner, transcriber=fake_transcriber,
    )
    assert brief["transcript"] == "Handmade jewelry for everyday wear"
    assert brief["video"]["duration_seconds"] == 42.0
    assert len(brief["frames"]) == 3
    assert brief["copy_options"] == {"primary_texts": [], "headlines": [], "descriptions": []}
    assert (tmp_path / "work" / "creative_brief.json").exists()
    assert "ffprobe" in calls and "ffmpeg" in calls


# --- Winning-copy library ---------------------------------------------------

from meta_ads_analysis.control import (
    build_copy_library,
    extract_creative_copy,
    render_copy_library_md,
)


def test_extract_creative_copy_from_object_story_spec() -> None:
    creative = {
        "object_story_spec": {
            "link_data": {
                "message": "Handmade jewelry for everyday wear",
                "name": "Shop the collection",
                "description": "Free shipping over $50",
            }
        }
    }
    copy = extract_creative_copy(creative)
    assert copy["primary_text"] == "Handmade jewelry for everyday wear"
    assert copy["headline"] == "Shop the collection"
    assert copy["description"] == "Free shipping over $50"


class _CopyLibFakeClient:
    def __init__(self, insights, ads):
        self._insights = insights
        self._ads = ads

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level, time_increment=1, breakdowns=None):
        return self._insights

    def fetch_ads(self, ad_account_id, *, fields):
        return self._ads


def test_build_copy_library_ranks_by_roas_and_attaches_copy() -> None:
    insights = [
        {"ad_id": "1", "ad_name": "Winner", "spend": "200",
         "action_values": [{"action_type": "purchase", "value": "800"}], "actions": [{"action_type": "purchase", "value": "10"}]},
        {"ad_id": "2", "ad_name": "Mid", "spend": "200",
         "action_values": [{"action_type": "purchase", "value": "300"}], "actions": [{"action_type": "purchase", "value": "5"}]},
        {"ad_id": "3", "ad_name": "TinySpend", "spend": "10",
         "action_values": [{"action_type": "purchase", "value": "90"}], "actions": [{"action_type": "purchase", "value": "1"}]},
    ]
    ads = [
        {"id": "1", "name": "Winner", "creative": {"object_story_spec": {"link_data": {"message": "Best seller", "name": "Shop now"}}}},
        {"id": "2", "name": "Mid", "creative": {"object_story_spec": {"link_data": {"message": "Nice rings", "name": "See more"}}}},
        {"id": "3", "name": "TinySpend", "creative": {"object_story_spec": {"link_data": {"message": "x", "name": "y"}}}},
    ]
    client = _CopyLibFakeClient(insights, ads)
    rows = build_copy_library(client, "act_1", date_from="2026-06-01", date_to="2026-06-30", min_spend=50, top_n=10)
    # TinySpend filtered out (spend<50); Winner (ROAS 4.0) before Mid (1.5)
    assert [r["ad_name"] for r in rows] == ["Winner", "Mid"]
    assert rows[0]["primary_text"] == "Best seller"
    md = render_copy_library_md("demo", rows, date_from="2026-06-01", date_to="2026-06-30")
    assert "Winner" in md and "Primary text" in md


def test_set_creative_op_validates_and_builds_request() -> None:
    from meta_ads_analysis.control import apply_ops_plan as _apply, validate_op as _v

    _v({"op_id": "x", "op": "set_creative", "level": "ad", "id": "ad1", "params": {"creative_id": "cr9"}})
    try:
        _v({"op_id": "x", "op": "set_creative", "level": "ad", "id": "ad1", "params": {}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    # set_creative is ad-level only
    try:
        _v({"op_id": "x", "op": "set_creative", "level": "adset", "id": "as1", "params": {"creative_id": "cr9"}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    adsets = [_adset("as1", "Set 1", ["A"], ["B"])]
    client = _FakeClient(adsets)
    # _FakeClient only has adset methods; add an ad updater inline
    captured = {}
    client.update_ad = lambda node_id, *, params, validate_only=False: captured.update({"id": node_id, "params": params}) or {"id": node_id, "success": True}
    plan = {"ops": [{"op_id": "swap", "op": "set_creative", "level": "ad", "id": "ad1", "params": {"creative_id": "cr9"}, "status": "approved"}]}
    results = _apply(plan, client, execute=True)
    assert results[0].status == "executed"
    assert captured["params"] == {"creative": {"creative_id": "cr9"}}


def test_create_video_ad_multi_text_uses_asset_feed_spec_and_opts_out_enhancements() -> None:
    plan = build_video_ad_plan(
        "act_1", name="Mission Ad", adset_id="as1", video_id="vid1", page_id="page9",
        link="https://www.shopdivinedesigns.com",
        primary_texts=["t1", "t2", "t3", "t4", "t5"],
        headlines=["h1", "h2"], descriptions=["d1"], call_to_action_type="SHOP_NOW",
    )
    op = plan["ops"][0]
    from meta_ads_analysis.authoring import validate_authoring_op as _va
    _va(op)
    # Net-new → abstain → blocked at apply until a conscious override; verify the sent request shape
    # via that override path (drop requires_grounding), which still forces PAUSED.
    op["status"] = "approved"
    plan["guardrails"]["requires_grounding"] = False
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(plan, client, execute=True)
    assert results[0].status == "created"
    _kind, params, _vo = client.creates[0]
    assert params["status"] == "PAUSED"
    afs = params["creative"]["asset_feed_spec"]
    assert [b["text"] for b in afs["bodies"]] == ["t1", "t2", "t3", "t4", "t5"]
    assert [t["text"] for t in afs["titles"]] == ["h1", "h2"]
    assert afs["link_urls"][0]["website_url"] == "https://www.shopdivinedesigns.com"
    assert afs["videos"][0]["video_id"] == "vid1"
    # Must NOT include the deprecated standard_enhancements field (Meta rejects it).
    assert "degrees_of_freedom_spec" not in params["creative"]


def test_create_video_ad_rejects_more_than_five_primary_texts() -> None:
    from meta_ads_analysis.authoring import validate_authoring_op as _va
    op = {"kind": "create_video_ad", "params": {
        "name": "x", "adset_id": "as1", "video_id": "v", "page_id": "p", "link": "u",
        "primary_texts": ["1", "2", "3", "4", "5", "6"]}}
    try:
        _va(op)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# --- Follow-up task system --------------------------------------------------

from datetime import date as _date

from meta_ads_analysis import followups as _fu


def test_followups_add_due_and_done(tmp_path: Path) -> None:
    root = tmp_path / "followups"
    # one due-soon, one future
    _fu.add_followup(account="divine_designs", title="Evaluate Mission Call ad", due="2026-06-30",
                     note="Check ROAS vs Engaged baseline.", created="2026-06-23", root=root)
    _fu.add_followup(account="divine_designs", title="Quarterly audience refresh", due="2026-09-01",
                     note="", created="2026-06-23", root=root)

    # 'due' filters by date: only the 2026-06-30 one is due as of 2026-07-01
    due = _fu.due_followups("divine_designs", as_of=_date(2026, 7, 1), root=root)
    assert [f.title for f in due] == ["Evaluate Mission Call ad"]
    # nothing due earlier
    assert _fu.due_followups("divine_designs", as_of=_date(2026, 6, 1), root=root) == []
    # both are open
    assert len(_fu.iter_followups("divine_designs", root=root)) == 2

    # mark the due one done -> it leaves the open list and stops being 'due'
    tid = due[0].task_id
    _fu.mark_done(account="divine_designs", task_id=tid, completed="2026-07-01", root=root)
    assert _fu.due_followups("divine_designs", as_of=_date(2026, 7, 1), root=root) == []
    open_items = _fu.iter_followups("divine_designs", root=root)
    assert [f.title for f in open_items] == ["Quarterly audience refresh"]
    # done archive is readable with include_done
    all_items = _fu.iter_followups("divine_designs", root=root, include_done=True)
    assert any(f.status == "done" for f in all_items)


def test_set_creative_features_rebuilds_creative_with_enroll_status() -> None:
    from meta_ads_analysis.control import apply_ops_plan as _apply, validate_op as _v

    _v({"op_id": "x", "op": "set_creative_features", "level": "ad", "id": "ad1",
        "params": {"opt_in": ["enhance_cta"], "opt_out": ["text_optimizations"]}})
    try:  # empty lists rejected
        _v({"op_id": "x", "op": "set_creative_features", "level": "ad", "id": "ad1", "params": {}})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    class _C:
        def __init__(self):
            self.captured = {}

        def get_ad(self, node_id, *, fields):
            return {"id": node_id, "creative": {"object_story_spec": {"page_id": "p", "video_data": {"video_id": "v"}}}}

        def update_ad(self, node_id, *, params, validate_only=False):
            self.captured = {"id": node_id, "params": params}
            return {"id": node_id, "success": True}

    c = _C()
    plan = {"ops": [{"op_id": "cf", "op": "set_creative_features", "level": "ad", "id": "ad1",
                     "params": {"opt_in": ["enhance_cta", "image_brightness_and_contrast"],
                                "opt_out": ["text_optimizations"]}, "status": "approved"}]}
    res = _apply(plan, c, execute=True)
    assert res[0].status == "executed"
    creative = c.captured["params"]["creative"]
    assert creative["object_story_spec"]["page_id"] == "p"  # original content preserved
    feats = creative["degrees_of_freedom_spec"]["creative_features_spec"]
    assert feats["enhance_cta"]["enroll_status"] == "OPT_IN"
    assert feats["image_brightness_and_contrast"]["enroll_status"] == "OPT_IN"
    assert feats["text_optimizations"]["enroll_status"] == "OPT_OUT"


# --- Guarded-write grounding scaffold (evidence/confidence on op + authoring plans) ----

from meta_ads_analysis.authoring import (
    GROUNDING_REQUIRED_KINDS,
    apply_authoring_plan as _apply_authoring,
)
from meta_ads_analysis.control import (
    GROUNDING_REQUIRED_OPS,
    apply_ops_plan as _apply_ops_grounding,
    write_ops_results,
)
from meta_ads_analysis.review import review_authoring_plan, review_ops_plan
from meta_ads_analysis.write_grounding import attach_op_grounding, op_grounding_gap


def _grounded_op(*, op_id, op, level, node_id, status, evidence, tier, spend_floor=100.0,
                 conversions_floor=25.0, recency_days=1, params=None, kind=None):
    """A control/authoring op with evidence+confidence attached via the shared scaffold."""
    out = {"op_id": op_id, "op": op, "level": level, "id": node_id, "status": status,
           "params": params or {}}
    if kind is not None:
        out["kind"] = kind
    attach_op_grounding(out, evidence=evidence, tier=tier, spend_floor=spend_floor,
                        conversions_floor=conversions_floor, recency_days=recency_days)
    return out


def test_attach_op_grounding_computes_band_never_free_types() -> None:
    # A strong sample resolves to the SAME band confidence.assess computes — proving the band came
    # from the rubric, not a value the caller typed.
    strong = Evidence("blended_roas", 1.0, "ROAS 1.00", "2026-06-10..2026-06-24",
                       120.0, 2400.0, "adset", "as1", "Set 1", None)
    op = {"op_id": "bump", "op": "set_daily_budget", "level": "adset", "id": "as1", "status": "proposed"}
    attach_op_grounding(op, evidence=strong, tier=EvidenceTier.direct_observation,
                        spend_floor=100.0, conversions_floor=25.0, recency_days=1)
    expected = assess(evidence=strong, tier=EvidenceTier.direct_observation,
                      spend_floor=100.0, conversions_floor=25.0, recency_days=1)
    assert op["confidence"]["band"] == expected.band.name == "high"
    assert op["evidence"]["sample_purchases"] == 120.0
    assert op["evidence"]["window"] == "2026-06-10..2026-06-24"


def test_attach_op_grounding_abstains_when_evidence_absent() -> None:
    # No sample → abstain (the absence of a score), NEVER a defaulted low/medium.
    op = {"op_id": "pause", "op": "set_status", "level": "ad", "id": "ad1", "status": "proposed"}
    attach_op_grounding(op, evidence=None, tier=EvidenceTier.direct_observation,
                        spend_floor=100.0, conversions_floor=25.0, recency_days=1)
    assert op["confidence"]["band"] == "abstain"
    assert op["confidence"]["data_band"] == "abstain"
    assert op["evidence"]["sample_purchases"] is None and op["evidence"]["sample_spend"] is None


def test_attach_op_grounding_no_evidence_keeps_full_evidence_keyset() -> None:
    # The "no evidence" serialized shape must carry the SAME keys as a real evidence_to_dict, so a
    # downstream reader (the gate / a renderer) sees a stable schema whether or not a sample was
    # supplied. Pins write_grounding._empty_evidence_dict against drift if Evidence gains a field.
    op = {"op_id": "pause", "op": "set_status", "level": "ad", "id": "ad1", "status": "proposed"}
    attach_op_grounding(op, evidence=None, tier=EvidenceTier.direct_observation,
                        spend_floor=100.0, conversions_floor=25.0, recency_days=1)
    reference_keys = evidence_to_dict(
        Evidence("blended_roas", 1.0, "ROAS 1.00", "2026-06-10..2026-06-24",
                 120.0, 2400.0, "adset", "as1", "Set 1", None)
    ).keys()
    assert op["evidence"].keys() == reference_keys


def test_attach_op_grounding_below_floor_abstains_not_low() -> None:
    thin = Evidence("blended_roas", 2.0, "ROAS 2.00", "2026-06-19..2026-06-24",
                    9.0, 40.0, "ad", "ad9", "Thin", None)
    op = {"op_id": "bump", "op": "set_daily_budget", "level": "adset", "id": "as2", "status": "proposed"}
    attach_op_grounding(op, evidence=thin, tier=EvidenceTier.correlational,
                        spend_floor=75.0, conversions_floor=25.0, recency_days=1)
    assert op["confidence"]["band"] == "abstain"  # below floor, not a fabricated low


def test_review_ops_plan_demotes_overclaimed_band() -> None:
    # Hand-inflated 'high' band over a sample the rubric only supports at 'medium'.
    op = {
        "op_id": "oc", "op": "set_daily_budget", "level": "adset", "id": "as3", "status": "approved",
        "params": {"daily_budget_cents": 11000},
        "evidence": {"metric_name": "blended_roas", "metric_value": 1.0,
                     "window": "2026-06-10..2026-06-24", "sample_purchases": 30.0,
                     "sample_spend": 200.0, "entity_level": "adset", "entity_id": "as3"},
        "confidence": {"band": "high", "data_band": "high", "grounding_band": "high",
                       "grounding_tier": "direct_observation", "factors": [], "would_raise": "",
                       "would_lower": "", "causal_flag": False},
    }
    plan = {"run_date": "2026-06-24", "ops": [op]}
    reviewed = review_ops_plan(plan, spend_floor=100.0)
    r = reviewed["ops"][0]
    assert r["review"]["verdict"] == "downgrade"
    assert Band[r["confidence"]["band"]] < Band.high
    assert r["review_verdict"] == "downgrade"
    # input plan not mutated
    assert "review" not in plan["ops"][0]
    assert plan["ops"][0]["confidence"]["band"] == "high"


def test_review_ops_plan_skips_ops_without_confidence_block() -> None:
    plan = {"ops": [{"op_id": "info", "op": "rename", "level": "adset", "id": "as1",
                     "status": "approved", "params": {"name": "X"}}]}
    reviewed = review_ops_plan(plan)
    assert "review" not in reviewed["ops"][0]  # no confidence block → never reviewed
    assert reviewed["ops"][0] == plan["ops"][0]


def test_review_ops_plan_is_idempotent() -> None:
    op = _grounded_op(
        op_id="short", op="set_daily_budget", level="adset", node_id="as1", status="approved",
        params={"daily_budget_cents": 11000},
        evidence=Evidence("blended_roas", 1.1, "ROAS 1.10", "2026-06-21..2026-06-24",
                          30.0, 200.0, "adset", "as1", "S", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    plan = {"run_date": "2026-06-24", "ops": [op]}
    once = review_ops_plan(plan, spend_floor=100.0)
    twice = review_ops_plan(once, spend_floor=100.0)
    assert once["ops"][0]["review"]["verdict"] == "downgrade"  # a real correction happened
    assert twice == once


def test_review_ops_gate_only_demotes_never_promotes() -> None:
    # An approved op claiming 'high' over a thin (below-floor) sample: recompute → abstain →
    # insufficient → demoted out of approved. Never promoted, never band-raised.
    op = {
        "op_id": "thin", "op": "set_status", "level": "ad", "id": "ad9", "status": "approved",
        "params": {"status": "ACTIVE"},
        "evidence": {"metric_name": "blended_roas", "metric_value": 2.0,
                     "window": "2026-06-19..2026-06-24", "sample_purchases": 9.0,
                     "sample_spend": 40.0, "entity_level": "ad", "entity_id": "ad9"},
        "confidence": {"band": "high", "data_band": "high", "grounding_band": "high",
                       "grounding_tier": "direct_observation", "factors": [], "would_raise": "",
                       "would_lower": "", "causal_flag": False},
    }
    # A separate proposed clean op must never be promoted to approved.
    clean = _grounded_op(
        op_id="clean", op="set_daily_budget", level="adset", node_id="as1", status="proposed",
        params={"daily_budget_cents": 11000},
        evidence=Evidence("blended_roas", 1.0, "ROAS 1.00", "2026-06-10..2026-06-24",
                          120.0, 2400.0, "adset", "as1", "S", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    plan = {"run_date": "2026-06-24", "ops": [op, clean]}
    reviewed = review_ops_plan(plan, spend_floor=75.0)
    demoted, untouched = reviewed["ops"][0], reviewed["ops"][1]
    assert demoted["review"]["verdict"] == "insufficient"
    assert demoted["confidence"]["band"] == "abstain"
    assert demoted["status"] == "proposed"  # approved → proposed (demote only)
    assert untouched["status"] == "proposed"  # never promoted
    # band never raised for either op, no executable key injected (op vocabulary)
    for before, after in zip(plan["ops"], reviewed["ops"]):
        assert Band[after["confidence"]["band"]] <= Band[before["confidence"]["band"]]
        assert "executable" not in after


def test_apply_ops_blocks_approved_ungrounded_write() -> None:
    client = _control_fixture()
    plan = {
        "guardrails": {"requires_grounding": True},
        "ops": [
            # grounding-required, approved, NO confidence block → blocked
            {"op_id": "ungrounded", "op": "set_daily_budget", "level": "adset", "id": "as1",
             "params": {"daily_budget_cents": 11000}, "status": "approved"},
            # rename is exempt → executes even without grounding
            {"op_id": "rename_ok", "op": "rename", "level": "adset", "id": "as1",
             "params": {"name": "Renamed"}, "status": "approved"},
            # grounded approved write → executes
            _grounded_op(op_id="grounded", op="set_daily_budget", level="adset", node_id="as1",
                         status="approved", params={"daily_budget_cents": 11000},
                         evidence=Evidence("blended_roas", 1.0, "ROAS 1.00", "2026-06-10..2026-06-24",
                                           120.0, 2400.0, "adset", "as1", "S", None),
                         tier=EvidenceTier.direct_observation),
        ],
    }
    by_id = {r.op_id: r for r in _apply_ops_grounding(plan, client, execute=True)}
    assert by_id["ungrounded"].status == "blocked"
    assert "missing required evidence/confidence" in by_id["ungrounded"].reason
    assert by_id["rename_ok"].status == "executed"  # exemption holds
    assert by_id["grounded"].status == "executed"


def test_apply_ops_grounding_guard_inert_without_flag() -> None:
    # Legacy/ungrounded plans (no requires_grounding flag) keep working — no new capability gating.
    client = _control_fixture()
    plan = {"ops": [{"op_id": "leg", "op": "set_daily_budget", "level": "adset", "id": "as1",
                     "params": {"daily_budget_cents": 11000}, "status": "approved"}]}
    results = _apply_ops_grounding(plan, client, execute=True)
    assert results[0].status == "executed"


def test_apply_ops_blocks_thin_abstain_but_allows_structural_abstain() -> None:
    client = _control_fixture()
    thin = _grounded_op(  # cited sample below floor → abstain → blocked when grounding required
        op_id="thin", op="set_daily_budget", level="adset", node_id="as1", status="approved",
        params={"daily_budget_cents": 11000},
        evidence=Evidence("blended_roas", 2.0, "ROAS 2.00", "2026-06-19..2026-06-24",
                          9.0, 40.0, "adset", "as1", "S", None),
        tier=EvidenceTier.correlational, spend_floor=75.0,
    )
    structural = _grounded_op(  # no sample → structural abstain → allowed (PAUSED safety)
        op_id="structural", op="set_status", level="ad", node_id="ad2", status="approved",
        params={"status": "PAUSED"}, evidence=None, tier=EvidenceTier.direct_observation,
    )
    plan = {"guardrails": {"requires_grounding": True}, "ops": [thin, structural]}
    by_id = {r.op_id: r for r in _apply_ops_grounding(plan, client, execute=True)}
    assert by_id["thin"].status == "blocked"
    assert "insufficient data" in by_id["thin"].reason
    assert by_id["structural"].status == "executed"  # honest structural abstention is allowed


def test_apply_authoring_blocks_ungrounded_and_keeps_paused() -> None:
    client = _AuthoringFakeClient()
    grounded = _grounded_op(
        op_id="g", op="create_adset", level="adset", node_id="", status="approved", kind="create_adset",
        params={"name": "New Set", "campaign_id": "c1"},
        evidence=Evidence("blended_roas", 4.0, "ROAS 4.00", "2026-06-10..2026-06-24",
                          120.0, 2400.0, "campaign", "c1", "Camp", None),
        tier=EvidenceTier.correlational,
    )
    plan = {
        "ad_account_id": "act_1",
        "guardrails": {"requires_grounding": True},
        "ops": [
            grounded,
            {"op_id": "ung", "kind": "create_campaign",
             "params": {"name": "C", "objective": "OUTCOME_SALES"}, "status": "approved"},
        ],
    }
    by_id = {r.op_id: r for r in _apply_authoring(plan, client, execute=True)}
    assert by_id["ung"].status == "blocked"
    assert "missing required evidence/confidence" in by_id["ung"].reason
    assert by_id["g"].status == "created"
    # PAUSED-by-default is untouched by the grounding gate — the create that ran is still PAUSED.
    for _kind, params, _vo in client.creates:
        assert params["status"] == "PAUSED"


def test_review_authoring_plan_demote_only_and_paused_preserved() -> None:
    # Over-claimed authoring op is demoted; running the gate never un-pauses a create.
    op = _grounded_op(
        op_id="g", op="create_adset", level="adset", node_id="", status="approved", kind="create_adset",
        params={"name": "New Set", "campaign_id": "c1"},
        evidence=Evidence("blended_roas", 1.1, "ROAS 1.10", "2026-06-21..2026-06-24",
                          30.0, 200.0, "campaign", "c1", "Camp", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    plan = {"ad_account_id": "act_1", "run_date": "2026-06-24",
            "guardrails": {"requires_grounding": True}, "ops": [op]}
    reviewed = review_authoring_plan(plan, spend_floor=100.0)
    r = reviewed["ops"][0]
    assert r["review"]["verdict"] == "downgrade"  # short window
    assert Band[r["confidence"]["band"]] < Band.medium  # demoted, never raised
    # apply the reviewed plan: create still forced PAUSED
    client = _AuthoringFakeClient()
    r["status"] = "approved"  # whatever the band, the create itself stays PAUSED on send
    _apply_authoring(reviewed, client, execute=True)
    for _kind, params, _vo in client.creates:
        assert params["status"] == "PAUSED"


def test_build_duplicate_ad_plan_grounds_on_proven_winner() -> None:
    # Duplicating a proven winner: evidence is the SOURCE ad's own metric over the window → a real
    # computed band (not abstain) → executable → created PAUSED.
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Winner", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [
            {"ad_id": "ad1", "ad_name": "Winner", "spend": "1200",
             "action_values": [{"action_type": "purchase", "value": "5040"}],
             "actions": [{"action_type": "purchase", "value": "60"}]}
        ],
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["entity_id"] == "ad1"  # cites the SOURCE ad
    assert op["evidence"]["sample_purchases"] == 60.0
    assert Band[op["confidence"]["band"]] >= Band.medium  # computed from a real sample
    assert op["review"]["verdict"] == "stands"
    op["status"] = "approved"
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(plan, client, execute=True)
    assert results[0].status == "created"
    kind, params, _vo = client.creates[0]
    assert kind == "ad" and params["status"] == "PAUSED"
    assert params["creative"] == {"creative_id": "cr-1"}  # copies the source creative


def test_build_duplicate_ad_plan_abstains_when_source_undelivered() -> None:
    # Symmetric safety case to the proven-winner test: a source ad with NO delivery in the window has
    # no insights row → the duplicate cites a ZERO sample → abstain → review marks it insufficient →
    # the apply-time gate blocks an approved create (you cannot scale out an unproven source).
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Cold", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [],  # no delivery → no row for the source ad
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["entity_id"] == "ad1"  # still names the (undelivered) source
    assert op["evidence"]["sample_purchases"] == 0.0  # cited zero, not a fabricated band
    assert op["confidence"]["band"] == "abstain"
    assert op["review"]["verdict"] == "insufficient"
    op["status"] = "approved"
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(plan, client, execute=True)
    assert results[0].status == "blocked"
    assert "insufficient data" in results[0].reason
    assert client.creates == []  # nothing created from an unproven source


def test_build_duplicate_ad_plan_install_goal_grounds_on_installs() -> None:
    # The fix for the authoring write path: an install-goal source ad has purchases≈0 but real install
    # volume. The duplicate's significance sample must be the install count (the conversion behind the
    # goal-aware cost-per-install metric), so the band clears low/abstain instead of being pinned there
    # by the 0 purchases the account never produces.
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Installer", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [
            {"ad_id": "ad1", "ad_name": "Installer", "spend": "1200",
             "actions": [
                 {"action_type": "purchase", "value": "0"},  # ~0 purchases — must be ignored
                 {"action_type": "mobile_app_install", "value": "120"},
             ]}
        ],
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        policy={"primary_goal": "maximize_in_app_subscriptions"},
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["sample_purchases"] == 120.0  # installs ground the sample, not the 0 purchases
    assert Band[op["confidence"]["band"]] > Band.low  # 120 installs → clears low/abstain
    assert op["review"]["verdict"] == "stands"


def test_build_duplicate_ad_plan_roas_goal_ignores_app_installs_decoy() -> None:
    # Parity guard: on a ROAS/default goal the significance sample stays purchases. An app_installs
    # decoy in the source row must be ignored — byte-identical to a purchases-only fixture.
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Winner", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [
            {"ad_id": "ad1", "ad_name": "Winner", "spend": "1200",
             "action_values": [{"action_type": "purchase", "value": "5040"}],
             "actions": [
                 {"action_type": "purchase", "value": "60"},
                 {"action_type": "mobile_app_install", "value": "999"},  # decoy — must be ignored
             ]}
        ],
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        policy={"primary_goal": "roas"},
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 60.0  # purchases, NOT the 999 install decoy


def test_build_duplicate_ad_plan_install_goal_no_row_still_cites_zero_and_abstains() -> None:
    # The no-row (zero-sample) branch is goal-independent: the goal-aware selector touches ONLY the
    # present-row branch. An install-goal source with no delivery still cites a ZERO sample → abstain,
    # exactly as a ROAS-goal one does (see test_build_duplicate_ad_plan_abstains_when_source_undelivered).
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Cold", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [],  # no delivery → no row for the source ad
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        policy={"primary_goal": "maximize_in_app_subscriptions"},
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["sample_purchases"] == 0.0  # zero branch is goal-independent
    assert op["confidence"]["band"] == "abstain"
    assert op["review_verdict"] == "insufficient"


def test_build_duplicate_ad_plan_no_goal_installs_present_keeps_sample_on_purchases() -> None:
    # Intentional asymmetry (parity with control + actions._select_sample_conversions): with NO goal
    # set, _status_metric falls through to cost_per_app_install when installs are present, but the
    # significance sample stays on purchases — the selector keys ONLY on the explicit install-goal
    # string. Metric and sample can legitimately disagree here; this pins that it is NOT "fixed".
    from meta_ads_analysis.reader_provider import FakeMetaReader

    reader = FakeMetaReader(
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "name": "Mixed", "creative": {"id": "cr-1"}},
        fetch_insights=lambda *a, **k: [
            {"ad_id": "ad1", "ad_name": "Mixed", "spend": "1200",
             "actions": [
                 {"action_type": "purchase", "value": "30"},
                 {"action_type": "mobile_app_install", "value": "120"},
             ]}
        ],
    )
    plan = build_duplicate_ad_plan(
        reader, "act_1", source_ad_id="ad1", target_adset_id="as2",
        policy={},  # no primary_goal
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["evidence"]["metric_name"] == "cost_per_app_install"  # no goal + installs present
    assert op["evidence"]["sample_purchases"] == 30.0  # but the sample stays on purchases


def test_authoring_netnew_create_abstains_insufficient_and_non_executable() -> None:
    # The common brand-new-campaign case: no metric → a cited ZERO sample → abstain → review marks it
    # insufficient → the apply-time gate blocks an approved create (conscious override required).
    netnew = _grounded_op(
        op_id="newcamp", op="create_campaign", level="campaign", node_id="", status="approved",
        kind="create_campaign", params={"name": "New Launch", "objective": "OUTCOME_SALES"},
        evidence=Evidence("blended_roas", None, "ROAS n/a", "2026-06-01..2026-06-24",
                          0.0, 0.0, "campaign", None, None, None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    assert netnew["confidence"]["band"] == "abstain"  # cited zero sample, not a fabricated band
    plan = {"ad_account_id": "act_1", "run_date": "2026-06-24",
            "guardrails": {"requires_grounding": True}, "ops": [netnew]}
    reviewed = review_authoring_plan(plan, spend_floor=100.0)
    r = reviewed["ops"][0]
    assert r["review"]["verdict"] == "insufficient"  # net-new → insufficient (non-executable)
    r["status"] = "approved"
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(reviewed, client, execute=True)
    assert results[0].status == "blocked"
    assert "insufficient data" in results[0].reason
    assert client.creates == []  # nothing created on no evidence


def test_authoring_lookalike_structural_abstain_is_creatable() -> None:
    # A lookalike's basis is its seed's size/quality, not a ROAS/conversions metric → NO sample (a
    # structural abstain naming the seed). Audiences are inert (no status, not in PAUSED_KINDS), so a
    # structural abstain is gate-allowed and the audience is creatable.
    plan = build_lookalike_plan(
        "act_1", name="LAL 2%", origin_audience_id="a1", country="US", ratio=0.02,
        date_from="2026-05-26", date_to="2026-06-24", run_date="2026-06-24",
    )
    op = plan["ops"][0]
    assert op["confidence"]["band"] == "abstain"
    assert op["evidence"]["sample_purchases"] is None  # structural — no fabricated sample
    assert op["evidence"]["entity_id"] == "a1"  # names the seed audience
    assert op["review"]["verdict"] == "stands"  # structural abstain, NOT insufficient
    op["status"] = "approved"
    client = _AuthoringFakeClient()
    results = apply_authoring_plan(plan, client, execute=True)
    assert results[0].status == "created"


def test_review_authoring_plan_is_idempotent() -> None:
    # The builders return an already-reviewed plan; re-reviewing one (every op already carries a
    # `review` block) is a no-op.
    plan = build_lookalike_plan("act_1", name="LAL", origin_audience_id="a1", country="US", ratio=0.01)
    assert plan["ops"][0]["review"]  # builder already reviewed it
    assert review_authoring_plan(plan) == plan


def test_authoring_paused_invariant_holds_even_when_review_stands() -> None:
    # A high-confidence duplicate whose verdict STANDS must STILL create PAUSED — the gate is
    # demote-only and authoring hardcodes PAUSED. Pins the invariant against future drift.
    op = _grounded_op(
        op_id="dup", op="create_ad", level="ad", node_id="", status="approved", kind="create_ad",
        params={"name": "Copy", "adset_id": "as2", "creative": {"creative_id": "cr1"}},
        evidence=Evidence("blended_roas", 5.0, "ROAS 5.00", "2026-06-01..2026-06-24",
                          300.0, 5000.0, "ad", "ad1", "Winner", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    assert op["confidence"]["band"] == "high"
    plan = {"ad_account_id": "act_1", "run_date": "2026-06-24",
            "guardrails": {"requires_grounding": True}, "ops": [op]}
    reviewed = review_authoring_plan(plan, spend_floor=100.0)
    r = reviewed["ops"][0]
    assert r["review"]["verdict"] == "stands"  # band earned; nothing to refute
    client = _AuthoringFakeClient()
    apply_authoring_plan(reviewed, client, execute=True)
    kind, params, _vo = client.creates[0]
    assert params["status"] == "PAUSED"  # never ACTIVE, even on a stands


def test_authoring_grounded_create_still_blocks_advantage_param() -> None:
    # Even a well-grounded create is blocked if it carries a Meta-AI / Advantage+ param (the
    # FORBIDDEN_FRAGMENTS / _guard_params block is untouched by grounding).
    op = _grounded_op(
        op_id="ai", op="create_campaign", level="campaign", node_id="", status="approved",
        kind="create_campaign",
        params={"name": "C", "objective": "OUTCOME_SALES", "creative_enhancement": True},
        evidence=Evidence("blended_roas", 4.0, "ROAS 4.00", "2026-06-01..2026-06-24",
                          300.0, 5000.0, "campaign", "c1", "Camp", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    assert op["confidence"]["band"] == "high"  # grounding alone would pass
    plan = {"ad_account_id": "act_1", "guardrails": {"requires_grounding": True}, "ops": [op]}
    by_id = {r.op_id: r for r in apply_authoring_plan(plan, _AuthoringFakeClient(), execute=True)}
    assert by_id["ai"].status == "blocked"
    assert "Meta AI / Advantage+" in by_id["ai"].reason


def test_authoring_grounded_plan_is_json_serializable() -> None:
    # The added evidence/confidence/review keys serialize cleanly (plan + audit-log safety): the plan
    # round-trips, and write_authoring_results still logs only op_id/kind/status/created_id (extra
    # op-dict keys do not leak into the result log).
    import json

    from meta_ads_analysis.authoring import AuthoringResult, write_authoring_results

    plan = build_lookalike_plan("act_1", name="LAL", origin_audience_id="a1", country="US", ratio=0.01)
    json.dumps(plan)  # grounded plan round-trips
    out = write_authoring_results(
        plan=plan,
        results=[AuthoringResult("lookalike_a1_1", "create_lookalike", "created", created_id="lal1")],
        output_path=_TMP_OPS_RESULTS(), execute=True,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["results"][0]["op_id"] == "lookalike_a1_1"
    assert payload["results"][0]["created_id"] == "lal1"
    assert "confidence" not in payload["results"][0]  # grounding does not leak into the result log


def test_op_grounding_review_keys_are_audit_log_safe() -> None:
    op = _grounded_op(
        op_id="oc", op="set_daily_budget", level="adset", node_id="as3", status="approved",
        params={"daily_budget_cents": 11000},
        evidence=Evidence("blended_roas", 1.0, "ROAS 1.00", "2026-06-10..2026-06-24",
                          30.0, 200.0, "adset", "as3", "S", None),
        tier=EvidenceTier.direct_observation, spend_floor=100.0,
    )
    plan = {"run_date": "2026-06-24", "account_slug": "demo", "intent": "scale", "ops": [op]}
    reviewed = review_ops_plan(plan, spend_floor=100.0)
    # the reviewed plan (op carries evidence/confidence/review/review_verdict) is JSON-serializable
    import json
    json.dumps(reviewed)
    # write_ops_results ignores the extra op-dict keys (it serializes only OpResult fields)
    from meta_ads_analysis.control import OpResult
    results = [OpResult("oc", "dry_run", request={"daily_budget": "11000"})]
    out = write_ops_results(plan=reviewed, results=results, output_path=_TMP_OPS_RESULTS(), execute=False)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["results"][0]["op_id"] == "oc"
    assert "review_verdict" not in payload["results"][0]  # extra keys do not leak into the result log


def _TMP_OPS_RESULTS():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "ops_results.json"


# --- CBO-aware budget +/- (control ops + actions parity) --------------------

from meta_ads_analysis.control import (
    BUDGET_ADSET_LEVEL,
    BUDGET_BROKEN,
    BUDGET_CBO_ACTIVE,
    build_budget_plan,
    classify_adset_budget,
)


def _bud_insights(*, purchases="120", spend="2400", value="9600", ids=True):
    """One insights row carrying BOTH ad-set and campaign ids, so the same row serves an ad-set-level
    and a campaign-level metric lookup. Default sample (120 purchases / $2400, value $9600) clears the
    floor with ROAS 4.0. Override ``value`` to move ROAS (1200→0.5, 2400→1.0, 12000→5.0)."""
    row = {"spend": spend,
           "action_values": [{"action_type": "purchase", "value": value}],
           "actions": [{"action_type": "purchase", "value": purchases}]}
    if ids:
        row.update({"adset_id": "as1", "adset_name": "Set 1", "campaign_id": "c1", "campaign_name": "Camp"})
    return [row]


def _adset_level_client(adset_budget="10000", insights=None):
    campaigns = [{"id": "c1", "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE"}]
    adsets = [{"id": "as1", "name": "Set 1", "status": "ACTIVE", "effective_status": "ACTIVE",
               "campaign_id": "c1", "daily_budget": adset_budget}]
    return _ControlFakeClient(campaigns, adsets, [],
                              insights=_bud_insights() if insights is None else insights)


def _cbo_client(campaign_daily="5000", campaign_lifetime=None, insights=None):
    """Ad set as1 has NO daily budget (CBO); the parent campaign c1 holds the budget."""
    campaign = {"id": "c1", "name": "Camp", "status": "ACTIVE", "effective_status": "ACTIVE"}
    if campaign_daily is not None:
        campaign["daily_budget"] = campaign_daily
    if campaign_lifetime is not None:
        campaign["lifetime_budget"] = campaign_lifetime
    adsets = [{"id": "as1", "name": "Set 1", "status": "ACTIVE", "effective_status": "ACTIVE",
               "campaign_id": "c1"}]  # no daily_budget
    return _ControlFakeClient([campaign], adsets, [],
                              insights=_bud_insights() if insights is None else insights)


def test_classify_adset_budget_levels() -> None:
    adset = classify_adset_budget(_adset_level_client(), "as1")
    assert adset["classification"] == BUDGET_ADSET_LEVEL
    assert adset["adset_daily_budget"] == 10000.0

    cbo_daily = classify_adset_budget(_cbo_client(), "as1")
    assert cbo_daily["classification"] == BUDGET_CBO_ACTIVE
    assert cbo_daily["campaign_id"] == "c1"
    assert cbo_daily["campaign_daily_budget"] == 5000.0

    cbo_lifetime = classify_adset_budget(_cbo_client(campaign_daily=None, campaign_lifetime="70000"), "as1")
    assert cbo_lifetime["classification"] == BUDGET_CBO_ACTIVE  # lifetime ALSO means "budget at campaign"
    assert cbo_lifetime["campaign_lifetime_budget"] == 70000.0

    broken = classify_adset_budget(_cbo_client(campaign_daily=None, campaign_lifetime=None), "as1")
    assert broken["classification"] == BUDGET_BROKEN


def test_build_budget_plan_cbo_redirects_to_campaign_op() -> None:
    # ROAS 4.0 (>= 3.0 target) so the scale-up is legit and the campaign op stands (isolates the
    # redirect mechanics from the direction refutation, which other tests cover).
    plan = build_budget_plan(
        _cbo_client(insights=_bud_insights(value="9600")), "act_1", new_daily_budget_cents=6000,
        adset_id="as1", policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    pointer = next(o for o in plan["ops"] if o["level"] == "adset")
    campaign_op = next(o for o in plan["ops"] if o["level"] == "campaign")
    # ad-set op is the non-executable pointer carrying the CBO classification
    assert pointer["cbo_detected"] is True
    assert pointer["status"] == "proposed"
    assert "CBO active" in pointer["note"]
    assert pointer["live_campaign_state"]["classification"] == "cbo_active"
    # campaign op is actionable, carries its OWN campaign-level evidence (not a copy of the ad set's)
    assert campaign_op["id"] == "c1"
    assert campaign_op["evidence"]["entity_level"] == "campaign"
    assert campaign_op["cbo_redirect_from_adset_id"] == "as1"
    assert campaign_op["action_type"] == "increase_campaign_budget"  # 6000 > campaign 5000
    assert campaign_op["review"]["verdict"] == "stands"


def test_apply_ops_cbo_active_adset_blocked_at_execute() -> None:
    # Re-read drift: an ad-set budget op that finds CBO at execute time is blocked, not mis-applied.
    client = _cbo_client()
    plan = {"ops": [{"op_id": "bump", "op": "set_daily_budget", "level": "adset", "id": "as1",
                     "params": {"daily_budget_cents": 6000}, "status": "approved"}]}
    res = apply_ops_plan(plan, client, execute=True)[0]
    assert res.status == "blocked"
    assert "CBO active" in res.reason
    assert client.updates == []


def test_apply_ops_budget_broken_blocked() -> None:
    client = _cbo_client(campaign_daily=None, campaign_lifetime=None)
    plan = {"ops": [{"op_id": "x", "op": "set_daily_budget", "level": "adset", "id": "as1",
                     "params": {"daily_budget_cents": 6000}, "status": "approved"}]}
    res = apply_ops_plan(plan, client, execute=True)[0]
    assert res.status == "blocked"
    assert "neither" in res.reason
    assert client.updates == []


def test_apply_ops_campaign_lifetime_budget_blocked() -> None:
    client = _cbo_client(campaign_daily=None, campaign_lifetime="70000")
    plan = {"ops": [{"op_id": "x", "op": "set_daily_budget", "level": "campaign", "id": "c1",
                     "params": {"daily_budget_cents": 6000}, "status": "approved"}]}
    res = apply_ops_plan(plan, client, execute=True)[0]
    assert res.status == "blocked"
    assert "lifetime" in res.reason
    assert client.updates == []


def test_apply_ops_campaign_daily_budget_executes() -> None:
    # The CBO-redirect deliverable's WRITE path: an approved campaign-level daily-budget op (the op
    # build_budget_plan emits for a CBO ad set) executes through to update_campaign. Covers both an
    # increase (within the 20% cap over the 5000-cent campaign budget) and a decrease (within the 50%
    # decrease cap and above the 100-cent floor).
    inc = _cbo_client()  # campaign c1 daily 5000, ad set as1 no budget
    inc_res = apply_ops_plan(
        {"ops": [{"op_id": "up", "op": "set_daily_budget", "level": "campaign", "id": "c1",
                  "params": {"daily_budget_cents": 5500}, "status": "approved"}]},
        inc, execute=True,
    )[0]
    assert inc_res.status == "executed"
    assert ("campaign", "c1", {"daily_budget": "5500"}, False) in inc.updates

    dec = _cbo_client()
    dec_res = apply_ops_plan(
        {"ops": [{"op_id": "down", "op": "set_daily_budget", "level": "campaign", "id": "c1",
                  "params": {"daily_budget_cents": 4000}, "status": "approved"}]},
        dec, execute=True,
    )[0]
    assert dec_res.status == "executed"
    assert ("campaign", "c1", {"daily_budget": "4000"}, False) in dec.updates


def test_apply_ops_budget_decrease_paths_and_caps() -> None:
    client = _adset_level_client()  # ad set as1 has a $100/day (10000-cent) budget
    plan = {"ops": [
        # 20% decrease: within the 50% cap and above the 100-cent floor → ok
        {"op_id": "dec_ok", "op": "set_daily_budget", "level": "adset", "id": "as1",
         "params": {"daily_budget_cents": 8000}, "status": "approved"},
        # 60% decrease: exceeds the default 50% decrease cap → blocked
        {"op_id": "dec_overcap", "op": "set_daily_budget", "level": "adset", "id": "as1",
         "params": {"daily_budget_cents": 4000}, "status": "approved"},
        # below the absolute floor (cap lifted to 99.9% so the FLOOR is what blocks, isolating it)
        {"op_id": "dec_floor", "op": "set_daily_budget", "level": "adset", "id": "as1",
         "params": {"daily_budget_cents": 50, "max_decrease_percent": 99.9}, "status": "approved"},
    ]}
    by_id = {r.op_id: r for r in apply_ops_plan(plan, client, execute=True)}
    assert by_id["dec_ok"].status == "executed"
    assert by_id["dec_overcap"].status == "blocked" and "max decrease" in by_id["dec_overcap"].reason
    assert by_id["dec_floor"].status == "blocked" and "floor" in by_id["dec_floor"].reason
    assert ("adset", "as1", {"daily_budget": "8000"}, False) in client.updates


def test_build_budget_plan_adset_level_increase_grounded() -> None:
    plan = build_budget_plan(
        _adset_level_client(insights=_bud_insights(value="9600")), "act_1",
        new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    assert len(plan["ops"]) == 1  # ad-set level → no campaign redirect
    op = plan["ops"][0]
    assert op["level"] == "adset" and op["action_type"] == "increase_adset_budget"
    assert op["evidence"]["entity_level"] == "adset"
    assert op["confidence"]["band"] in {"high", "medium"}
    assert op["review"]["verdict"] == "stands"


def test_build_budget_plan_thin_sample_abstains_and_is_blocked() -> None:
    thin = _bud_insights(purchases="9", spend="40", value="80")
    plan = build_budget_plan(
        _adset_level_client(insights=thin), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-19", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["confidence"]["band"] == "abstain"  # below floor — never a fabricated low
    assert op["review"]["verdict"] == "insufficient"
    # Approving it anyway is blocked at the apply-time grounding gate (cited sample + abstain).
    op["status"] = "approved"
    res = apply_ops_plan(plan, _adset_level_client(insights=thin), execute=True)[0]
    assert res.status == "blocked"
    assert "insufficient data" in res.reason


def test_build_budget_plan_review_refutes_scale_up_below_target() -> None:
    below = _bud_insights(value="2400")  # ROAS 1.0, sample 120/$2400 clears the floor
    plan = build_budget_plan(
        _adset_level_client(insights=below), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["review"]["verdict"] == "refuted"  # scaling up an entity below the ROAS target
    assert op["review_verdict"] == "refuted"


def test_build_budget_plan_review_refutes_cutting_a_clear_winner() -> None:
    winner = _bud_insights(value="12000")  # ROAS 5.0 ≥ 3.0 * 1.5 winner margin
    plan = build_budget_plan(
        _adset_level_client(insights=winner), "act_1", new_daily_budget_cents=8000, adset_id="as1",
        policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["action_type"] == "decrease_adset_budget"  # 8000 < current 10000
    assert op["review"]["verdict"] == "refuted"  # cutting the budget of a clear winner


def _bud_install_insights(installs: str, spend: str = "2400"):
    """An install-goal insights row carrying ad-set + campaign ids and app-install actions, so
    ``fetch_entity_metrics`` computes cost/install = spend / installs. No purchase actions, but on an
    install goal the significance sample is the install count (_status_sample_conversions), which clears
    the floor, and $2400 spend clears MIN_SCALING_SPEND too — so the sample-floor abstain does not fire
    and the direction refutation surfaces as the verdict."""
    return [{"spend": spend,
             "actions": [{"action_type": "mobile_app_install", "value": installs}],
             "adset_id": "as1", "adset_name": "Set 1", "campaign_id": "c1", "campaign_name": "Camp"}]


def test_build_budget_plan_install_goal_refutes_scale_up_above_cost_target() -> None:
    # End-to-end on the control BUDGET-op surface for an install goal (the composition no other test
    # drives together: _budget_op's action_type + _status_metric's cost_per_app_install + the gate's
    # install branch). Scaling up an entity whose cited cost/install ($4 = $2400 / 600) sits ABOVE the
    # $3 target is scaling a loser — the cost-polarity mirror of the ROAS scale-up-below-target test.
    loser = _bud_install_insights("600")  # 2400 / 600 = $4.00 cost/install, above $3 target
    plan = build_budget_plan(
        _adset_level_client(insights=loser), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "maximize_in_app_subscriptions",
                "secondary_cost_per_app_install_target": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["action_type"] == "increase_adset_budget"  # 11000 > current 10000
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["metric_value"] == 4.0
    assert op["review"]["verdict"] == "refuted"
    assert op["review_verdict"] == "refuted"
    assert "direction" in op["review"]["failed_inputs"]


def test_build_budget_plan_install_goal_refutes_cutting_a_clear_winner() -> None:
    # The inverted-polarity mirror on the budget surface: cutting the budget of an entity whose cited
    # cost/install ($1.50 = $2400 / 1600) is comfortably below the $3 target (<= 3/1.5 = $2) is cutting
    # a winner. Refuted.
    winner = _bud_install_insights("1600")  # 2400 / 1600 = $1.50 cost/install, below the $2 margin
    plan = build_budget_plan(
        _adset_level_client(insights=winner), "act_1", new_daily_budget_cents=8000, adset_id="as1",
        policy={"primary_goal": "maximize_in_app_subscriptions",
                "secondary_cost_per_app_install_target": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["action_type"] == "decrease_adset_budget"  # 8000 < current 10000
    assert op["evidence"]["metric_value"] == 1.5
    assert op["review"]["verdict"] == "refuted"  # cutting the budget of a cost-per-install winner


def test_build_budget_plan_install_goal_grounds_sample_on_installs() -> None:
    # The core fix on the budget surface: an install-goal entity with NO purchases but real install
    # volume grounds the significance sample on installs, so the move clears `low`/abstain instead of
    # being structurally pinned there. No direction target is configured, so it stands — the band is the
    # point. Pre-fix the sample was ~0 purchases and this abstained.
    backed = _bud_install_insights("120")  # 120 installs >= 4 * 25 floor; $2400 / 120 = $20 cost/install
    plan = build_budget_plan(
        _adset_level_client(insights=backed), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "maximize_in_app_subscriptions"},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["evidence"]["metric_name"] == "cost_per_app_install"
    assert op["evidence"]["sample_purchases"] == 120.0  # installs ground the sample, not purchases
    assert Band[op["confidence"]["band"]] > Band.low
    assert op["review"]["verdict"] == "stands"


def test_build_budget_plan_roas_goal_ignores_app_installs_decoy() -> None:
    # Parity guard on the budget surface: a ROAS-goal move keeps the sample on purchases; an
    # app_installs decoy is ignored (byte-identical to a purchases-only fixture).
    row = _bud_insights(value="9600")  # 120 purchases / $2400, ROAS 4.0
    row[0]["actions"].append({"action_type": "mobile_app_install", "value": "999"})  # decoy
    plan = build_budget_plan(
        _adset_level_client(insights=row), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["evidence"]["metric_name"] == "blended_roas"
    assert op["evidence"]["sample_purchases"] == 120.0  # purchases, NOT the 999 install decoy


def test_build_budget_plan_install_goal_no_installs_thin_row_abstains_and_blocks() -> None:
    # Present row but app_installs absent on an install goal → sample is None while the row IS present.
    # sample_spend is always numeric, so the sample stays CITED; with the conversions floor uncleared
    # and spend below MIN_SCALING_SPEND too, the band abstains WITH a cited sample → the gate BLOCKS.
    # Same shape as today's purchases-None present-row case — only which conversion count fills it.
    thin = [{"spend": "40",  # below MIN_SCALING_SPEND (75); no app-install or purchase actions
             "adset_id": "as1", "adset_name": "Set 1", "campaign_id": "c1", "campaign_name": "Camp"}]
    plan = build_budget_plan(
        _adset_level_client(insights=thin), "act_1", new_daily_budget_cents=11000, adset_id="as1",
        policy={"primary_goal": "maximize_in_app_subscriptions"},
        date_from="2026-06-19", date_to="2026-06-24", run_date="2026-06-25",
    )
    op = plan["ops"][0]
    assert op["evidence"]["sample_purchases"] is None  # app_installs absent → None, but row IS present
    assert op["evidence"]["sample_spend"] == 40.0      # still a cited sample
    assert op["confidence"]["band"] == "abstain"
    op["status"] = "approved"
    res = apply_ops_plan(plan, _adset_level_client(insights=thin), execute=True)[0]
    assert res.status == "blocked"
    assert "insufficient data" in res.reason


def test_actions_ops_cbo_classification_parity() -> None:
    # The ops path (classify_adset_budget) and the action path
    # (_populate_budget_params_from_live_state) must classify an identical fixture identically.
    from meta_ads_analysis.actions import _populate_budget_params_from_live_state

    def _reader():
        return FakeMetaReader(
            get_adset=lambda adset_id, *, fields: {"id": "as1", "campaign_id": "c1"},  # no daily_budget
            get_campaign=lambda campaign_id, *, fields: {"id": "c1", "daily_budget": "5000"},
        )

    ops_state = classify_adset_budget(_reader(), "as1")
    assert ops_state["classification"] == BUDGET_CBO_ACTIVE

    action = {
        "action_type": "increase_adset_budget",
        "target": {"type": "adset", "id": "as1"},
        "params": {},
        "live_adset_state": {"adset_id": "as1", "campaign_id": "c1", "daily_budget": None},
        "rationale": "scale candidate",
    }
    _populate_budget_params_from_live_state(action, _reader())
    assert action["cbo_detected"] is True
    assert action["executable"] is False
    assert action["live_campaign_state"]["classification"] == ops_state["classification"]


def test_build_budget_plan_direct_campaign_target() -> None:
    plan = build_budget_plan(
        _cbo_client(insights=_bud_insights(value="9600")), "act_1", new_daily_budget_cents=5500,
        campaign_id="c1", policy={"primary_goal": "roas", "target_roas": 3.0},
        date_from="2026-06-10", date_to="2026-06-24", run_date="2026-06-25",
    )
    assert len(plan["ops"]) == 1
    op = plan["ops"][0]
    assert op["level"] == "campaign" and op["id"] == "c1"
    assert op["action_type"] == "increase_campaign_budget"  # 5500 > campaign 5000
    assert "cbo_redirect_from_adset_id" not in op  # direct target, not a redirect
    assert op["evidence"]["entity_level"] == "campaign"


def test_build_budget_plan_requires_exactly_one_target() -> None:
    client = _adset_level_client()
    for kwargs in ({}, {"adset_id": "as1", "campaign_id": "c1"}):
        try:
            build_budget_plan(client, "act_1", new_daily_budget_cents=11000, **kwargs)
            raise AssertionError("expected ValueError for ambiguous/missing target")
        except ValueError:
            pass


def test_actions_adset_level_budget_populates_current() -> None:
    # Non-CBO ad set with its own budget: current is populated and no CBO redirect happens.
    from meta_ads_analysis.actions import _populate_budget_params_from_live_state

    action = {
        "action_type": "increase_adset_budget",
        "target": {"type": "adset", "id": "as1"},
        "params": {},
        "live_adset_state": {"adset_id": "as1", "campaign_id": "c1", "daily_budget": "10000"},
    }
    _populate_budget_params_from_live_state(action, FakeMetaReader())  # reader never consulted
    assert action["params"]["current_daily_budget_cents"] == 10000
    assert "cbo_detected" not in action


# --- Runaway / outlier watch scanner ----------------------------------------

from datetime import date as _d

from meta_ads_analysis.monitor import build_watch_report, classify_ad


def _cls(**kw):
    base = dict(spend=300, roas=0.5, results=0, days_since_change=30, accelerating=False,
                min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    base.update(kw)
    return classify_ad(**base)["classification"]


def test_classify_ad_buckets_and_protection() -> None:
    assert _cls(spend=50) == "insufficient"                  # below significance floor
    assert _cls(days_since_change=2) == "watch"              # young -> protected, never urgent
    assert _cls(roas=0.5, days_since_change=30) == "urgent"  # mature + below floor
    assert _cls(roas=2.0, days_since_change=30) == "underperforming"  # floor<roas<target
    assert _cls(roas=3.5, days_since_change=30) == "ok"      # at/above target
    # $ at risk scales with how far below target
    v = classify_ad(spend=200, roas=0.0, results=0, days_since_change=30, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["dollars_at_risk"] == 200.0


class _WatchFakeClient:
    def __init__(self, insights, ads_meta):
        self._insights = insights
        self._ads = ads_meta

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level, time_increment=1, breakdowns=None):
        return self._insights

    def iter_paginated(self, path, *, params=None):
        return list(self._ads)


def test_build_watch_report_protects_young_flags_mature() -> None:
    insights = [
        {"ad_id": "m1", "ad_name": "Mature Loser", "spend": "300",
         "action_values": [{"action_type": "purchase", "value": "150"}], "actions": [{"action_type": "purchase", "value": "3"}]},
        {"ad_id": "y1", "ad_name": "Young Loser", "spend": "300",
         "action_values": [{"action_type": "purchase", "value": "150"}], "actions": [{"action_type": "purchase", "value": "3"}]},
        {"ad_id": "ok1", "ad_name": "Winner", "spend": "300",
         "action_values": [{"action_type": "purchase", "value": "1200"}], "actions": [{"action_type": "purchase", "value": "20"}]},
    ]
    ads_meta = [
        {"id": "m1", "name": "Mature Loser", "effective_status": "ACTIVE", "adset_id": "as1", "updated_time": "2026-06-01T00:00:00+0000"},
        {"id": "y1", "name": "Young Loser", "effective_status": "ACTIVE", "adset_id": "as1", "updated_time": "2026-06-22T00:00:00+0000"},
        {"id": "ok1", "name": "Winner", "effective_status": "ACTIVE", "adset_id": "as1", "updated_time": "2026-06-01T00:00:00+0000"},
    ]
    client = _WatchFakeClient(insights, ads_meta)
    report = build_watch_report(client, "act_1", account_slug="demo", as_of=_d(2026, 6, 24),
                                roas_floor=1.5, roas_target=3.0, min_spend=100, grace_days=5)
    by_id = {r["ad_id"]: r for r in report["rows"]}
    assert by_id["m1"]["classification"] == "urgent"
    assert by_id["y1"]["classification"] == "watch"      # young -> protected
    assert "ok1" not in by_id                            # winner not flagged
    # only flaggable (urgent/underperforming) go on the watchlist
    assert "m1" in report["watchlist"]["ads"] and "y1" not in report["watchlist"]["ads"]
    assert report["watchlist"]["ads"]["m1"]["times_flagged"] == 1


def test_classify_ad_below_min_spend_confidence_abstains_not_low() -> None:
    # An ad below the significance floor abstains (⚪ "insufficient data"), it is NOT scored low.
    v = classify_ad(spend=50, roas=0.5, results=0, days_since_change=30, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["classification"] == "insufficient"
    assert v["confidence"]["band"] == "abstain"
    assert v["confidence"]["band"] != "low"


def test_classify_ad_young_ad_stays_watch_confidence_abstains_never_urgent() -> None:
    # A young ad below the ROAS floor stays `watch` (grace wins) and its confidence reflects
    # "too young to judge" — abstain, never a high-confidence pause.
    v = classify_ad(spend=300, roas=0.5, results=0, days_since_change=2, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["classification"] == "watch"            # grace beats pause
    assert v["classification"] != "urgent"
    assert v["confidence"]["band"] == "abstain"      # not "low", not "high"
    assert v["confidence"]["causal_flag"] is False


def test_classify_ad_urgent_confidence_is_direct_observation_non_causal() -> None:
    # A mature below-floor ad gets a direct-observation data band; descriptive reasons are not
    # causal language, so causal_flag stays False (asserted, per the ticket).
    v = classify_ad(spend=300, roas=0.5, results=3, days_since_change=30, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["classification"] == "urgent"
    assert v["confidence"]["grounding_tier"] == "direct_observation"
    assert v["confidence"]["causal_flag"] is False
    # 3 purchases is below the 25 conversion floor (spend cleared) → thin-on-conversions → low.
    assert v["confidence"]["band"] == "low"


def test_classify_ad_young_ad_with_large_sample_still_abstains() -> None:
    # The grace abstention is NOT a below-floor sample: a young ad can be heavily funded with many
    # conversions, yet we still decline to judge it. This is exactly why grace routes through the
    # abstain factory rather than the sample-size rubric in assess.
    v = classify_ad(spend=5000, roas=0.5, results=400, days_since_change=2, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["classification"] == "watch"
    assert v["confidence"]["band"] == "abstain"        # not high, despite 400 purchases / $5k spend
    assert v["confidence"]["data_band"] == "abstain"


def test_classify_ad_underperforming_carries_direct_observation_confidence() -> None:
    # floor < roas < target on a mature ad with enough conversions → a real (non-abstain) data band.
    v = classify_ad(spend=300, roas=2.0, results=30, days_since_change=30, accelerating=False,
                    min_spend=100, grace_days=5, roas_floor=1.5, roas_target=3.0)
    assert v["classification"] == "underperforming"
    assert v["confidence"]["grounding_tier"] == "direct_observation"
    assert v["confidence"]["band"] == "medium"         # 30 purchases ≥ 25 floor, < 100 high-knee
    assert v["confidence"]["causal_flag"] is False


def test_build_watch_report_rows_carry_confidence_and_reproducible_evidence() -> None:
    insights = [
        {"ad_id": "m1", "ad_name": "Mature Loser", "spend": "300",
         "action_values": [{"action_type": "purchase", "value": "150"}], "actions": [{"action_type": "purchase", "value": "3"}]},
    ]
    ads_meta = [
        {"id": "m1", "name": "Mature Loser", "effective_status": "ACTIVE", "adset_id": "as1", "updated_time": "2026-06-01T00:00:00+0000"},
    ]
    report = build_watch_report(_WatchFakeClient(insights, ads_meta), "act_1", account_slug="demo",
                                as_of=_d(2026, 6, 24), roas_floor=1.5, roas_target=3.0, min_spend=100, grace_days=5)
    row = {r["ad_id"]: r for r in report["rows"]}["m1"]
    assert row["classification"] == "urgent"
    assert row["confidence"]["grounding_tier"] == "direct_observation"
    assert row["confidence"]["causal_flag"] is False
    ev = row["evidence"]
    assert ev["entity_level"] == "ad" and ev["entity_id"] == "m1"
    assert ev["window"] == "2026-06-18..2026-06-24"
    # A flagged ad traces back to a reproducible account_metrics command.
    assert ev["regenerating_query"] == (
        "account_metrics --account demo --level ad --date-from 2026-06-18 --date-to 2026-06-24"
    )


# --- A/B experiment harness ----------------------------------------------------

from datetime import date as _date_e

from meta_ads_analysis import experiment as _exp


class _ExpFakeClient:
    def __init__(self, insights):
        self._insights = insights

    def fetch_insights(self, ad_account_id, *, fields, date_from, date_to, level, time_increment=1, breakdowns=None):
        return self._insights


def test_two_proportion_pvalue_detects_clear_difference() -> None:
    # 100/10000 vs 300/10000 — a large, obviously-significant gap.
    p = _exp.two_proportion_pvalue(300, 10000, 100, 10000)
    assert p is not None and p < 0.001


def test_two_proportion_pvalue_identical_rates_not_significant() -> None:
    p = _exp.two_proportion_pvalue(150, 10000, 150, 10000)
    assert p == 1.0


def test_two_proportion_pvalue_guards_degenerate_inputs() -> None:
    assert _exp.two_proportion_pvalue(0, 0, 1, 10) is None   # empty arm
    assert _exp.two_proportion_pvalue(0, 100, 0, 100) is None  # pooled rate 0


def test_define_load_list_experiment_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_exp, "EXPERIMENTS_ROOT", tmp_path)
    path = _exp.define_experiment(
        account="Demo Account", exp_id="enh-cta", hypothesis="enhance_cta lifts ROAS",
        variable="enhance_cta on vs off", level="ad", control_ids=["c1"], variant_ids=["v1"],
        start_date="2026-06-01", planned_days=14, notes="", created="2026-06-01",
    )
    assert path.exists()
    loaded = _exp.load_experiment("demo_account", "enh-cta")
    assert loaded.variable == "enhance_cta on vs off"
    assert loaded.control_ids == ["c1"] and loaded.variant_ids == ["v1"]
    items = _exp.list_experiments("demo_account")
    assert [e.id for e in items] == ["enh-cta"]


def test_define_experiment_rejects_bad_inputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_exp, "EXPERIMENTS_ROOT", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        _exp.define_experiment(account="d", exp_id="x", hypothesis="h", variable="v",
                               level="bogus", control_ids=["c"], variant_ids=["v"],
                               start_date="2026-06-01", created="2026-06-01")
    with pytest.raises(ValueError):
        _exp.define_experiment(account="d", exp_id="x", hypothesis="h", variable="v",
                               level="ad", control_ids=[], variant_ids=["v"],
                               start_date="2026-06-01", created="2026-06-01")


def _exp_obj(**over):
    base = dict(id="t", account="demo", hypothesis="h", variable="enh on/off", level="ad",
                control_ids=["c1"], variant_ids=["v1"], metric="roas",
                start_date="2026-06-01", planned_days=14, status="active", notes="", created="2026-06-01")
    base.update(over)
    return _exp.Experiment(**base)


def test_read_experiment_insufficient_data_gate() -> None:
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "100",
         "action_values": [{"action_type": "purchase", "value": "300"}],
         "actions": [{"action_type": "purchase", "value": "5"}], "impressions": "1000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "100",
         "action_values": [{"action_type": "purchase", "value": "400"}],
         "actions": [{"action_type": "purchase", "value": "6"}], "impressions": "1000"},
    ]
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24))
    assert r["control"]["roas"] == 3.0 and r["variant"]["roas"] == 4.0
    assert r["roas_lift_pct"] == round((4.0 / 3.0 - 1) * 100, 1)
    assert "INSUFFICIENT DATA" in r["verdict"]   # only 5/6 purchases, below default 25


def test_read_experiment_calls_significant_winner() -> None:
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "2000"}],
         "actions": [{"action_type": "purchase", "value": "100"}], "impressions": "100000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "6000"}],
         "actions": [{"action_type": "purchase", "value": "300"}], "impressions": "100000"},
    ]
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24),
                             min_conversions=25)
    assert r["conversion_rate_pvalue"] is not None and r["conversion_rate_pvalue"] < 0.05
    assert "SIGNIFICANT" in r["verdict"] and "variant" in r["verdict"]
    assert r["variant"]["roas"] == 6.0 and r["control"]["roas"] == 2.0


def test_read_experiment_significant_reads_high_confidence_ab_experiment() -> None:
    # A clean, well-powered, significant A/B reads 🟢 High — the experiment is the TOP grounding tier,
    # so it is NOT capped the way a correlational claim is. (The same claim read Medium as a
    # correlational action in the actions ticket; grounding improved, so confidence rises.)
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "2000"}],
         "actions": [{"action_type": "purchase", "value": "100"}], "impressions": "100000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "6000"}],
         "actions": [{"action_type": "purchase", "value": "300"}], "impressions": "100000"},
    ]
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24),
                             min_conversions=25)
    assert r["confidence"]["band"] == "high"
    assert r["confidence"]["grounding_tier"] == "ab_experiment"
    assert r["confidence"]["causal_flag"] is False          # the A/B IS the causal instrument
    ev = r["evidence"]
    assert ev["entity_level"] == "ad" and ev["sample_purchases"] == 100   # weaker arm governs
    assert ev["regenerating_query"] == (
        "account_metrics --account demo --level ad --date-from 2026-06-01 --date-to 2026-06-24"
    )


def test_read_experiment_below_min_conversions_abstains_keeps_verdict_string() -> None:
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "100",
         "action_values": [{"action_type": "purchase", "value": "300"}],
         "actions": [{"action_type": "purchase", "value": "5"}], "impressions": "1000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "100",
         "action_values": [{"action_type": "purchase", "value": "400"}],
         "actions": [{"action_type": "purchase", "value": "6"}], "impressions": "1000"},
    ]
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24))
    assert r["confidence"]["band"] == "abstain"
    assert "INSUFFICIENT DATA" in r["verdict"]   # existing human verdict string preserved


def test_read_experiment_no_significant_difference_caps_confidence_at_medium() -> None:
    # Enough data per arm, but identical conversion rates (p>=0.05) → no proven effect → the data band
    # is capped at medium even though the sample is large. Verdict string is unchanged.
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "2000"}],
         "actions": [{"action_type": "purchase", "value": "100"}], "impressions": "100000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "1000",
         "action_values": [{"action_type": "purchase", "value": "2000"}],
         "actions": [{"action_type": "purchase", "value": "100"}], "impressions": "100000"},
    ]
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24),
                             min_conversions=25)
    assert r["conversion_rate_pvalue"] is not None and r["conversion_rate_pvalue"] >= 0.05
    assert r["confidence"]["grounding_tier"] == "ab_experiment"
    assert r["confidence"]["band"] == "medium"   # NOT high — no proven difference yet
    assert "NO significant difference" in r["verdict"]


def test_readout_json_output_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_exp, "EXPERIMENTS_ROOT", tmp_path)
    insights = [
        {"ad_id": "c1", "ad_name": "Control", "spend": "500",
         "action_values": [{"action_type": "purchase", "value": "1000"}],
         "actions": [{"action_type": "purchase", "value": "50"}], "impressions": "50000"},
        {"ad_id": "v1", "ad_name": "Variant", "spend": "500",
         "action_values": [{"action_type": "purchase", "value": "1500"}],
         "actions": [{"action_type": "purchase", "value": "75"}], "impressions": "50000"},
    ]
    out = tmp_path / "output" / "readout.json"
    r = _exp.read_experiment(_ExpFakeClient(insights), "act_1", _exp_obj(), as_of=_date_e(2026, 6, 24), min_conversions=25)
    from meta_ads_analysis.utils import ensure_dir, write_json
    ensure_dir(out.parent)
    write_json(out, r)
    import json
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["verdict"] == r["verdict"]
    assert "control" in loaded and "variant" in loaded
    assert loaded["control"]["roas"] == 2.0
    assert loaded["variant"]["roas"] == 3.0


_EXP_CLI_INSIGHTS = [
    {"ad_id": "c1", "ad_name": "Control", "spend": "500",
     "action_values": [{"action_type": "purchase", "value": "1000"}],
     "actions": [{"action_type": "purchase", "value": "50"}], "impressions": "50000"},
    {"ad_id": "v1", "ad_name": "Variant", "spend": "500",
     "action_values": [{"action_type": "purchase", "value": "1500"}],
     "actions": [{"action_type": "purchase", "value": "75"}], "impressions": "50000"},
]


def _setup_exp_cli(tmp_path, monkeypatch):
    """Define an experiment in a temp root and stub the Meta-touching deps so
    `experiment_main()` can run the readout branch offline. Returns the slug/id."""
    from meta_ads_analysis import cli as _cli
    from meta_ads_analysis import meta_api as _meta_api

    monkeypatch.setattr(_exp, "EXPERIMENTS_ROOT", tmp_path)
    _exp.define_experiment(
        account="demo", exp_id="enh-cta", hypothesis="enhance_cta lifts ROAS",
        variable="enhance_cta on vs off", level="ad", control_ids=["c1"], variant_ids=["v1"],
        start_date="2026-06-01", planned_days=14, notes="", created="2026-06-01",
    )
    monkeypatch.setattr(_cli, "resolve_ad_account_id", lambda slug: "act_1")
    monkeypatch.setattr(_meta_api, "client_from_env", lambda api_version=None: _ExpFakeClient(_EXP_CLI_INSIGHTS))
    return "demo", "enh-cta"


def test_experiment_readout_cli_writes_json(tmp_path, monkeypatch, capsys) -> None:
    from meta_ads_analysis.cli import experiment_main

    account, exp_id = _setup_exp_cli(tmp_path, monkeypatch)
    out = tmp_path / "new-dir" / "readout.json"   # parent does not exist yet
    monkeypatch.setattr(sys, "argv", [
        "experiment", "readout", "--account", account, "--id", exp_id,
        "--as-of", "2026-06-24", "--json-output-path", str(out),
    ])
    experiment_main()

    assert out.exists()
    assert f"Wrote readout JSON: {out}" in capsys.readouterr().out
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert "SIGNIFICANT" in loaded["verdict"] and "variant" in loaded["verdict"]
    assert loaded["control"]["roas"] == 2.0 and loaded["variant"]["roas"] == 3.0
    for key in ("roas_lift_pct", "conversion_rate_pvalue", "generated_at"):
        assert key in loaded


def test_experiment_readout_cli_no_json_path_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    from meta_ads_analysis.cli import experiment_main

    account, exp_id = _setup_exp_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", [
        "experiment", "readout", "--account", account, "--id", exp_id, "--as-of", "2026-06-24",
    ])
    experiment_main()

    captured = capsys.readouterr().out
    assert "VERDICT:" in captured                      # table still printed
    assert "Wrote readout JSON" not in captured        # no file confirmation
    # only the experiment-definition JSON exists; no readout file was written
    assert not any(p.name == "readout.json" for p in tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# Confidence engine (src/meta_ads_analysis/confidence.py)
# ---------------------------------------------------------------------------


def _recent_evidence(*, purchases: float | None, spend: float | None) -> Evidence:
    """A reusable, fully-populated Evidence with a recent window for confidence tests."""
    return Evidence(
        metric_name="blended_roas",
        metric_value=1.20,
        metric_display="ROAS 1.20",
        window="2026-06-10..2026-06-24",
        sample_purchases=purchases,
        sample_spend=spend,
        entity_level="ad",
        entity_id="123",
        entity_name="Scale Winner",
        regenerating_query=build_regenerating_query("divine_designs", "ad", "2026-06-10", "2026-06-24"),
    )


def test_combine_bands_returns_the_weaker_axis() -> None:
    assert combine_bands(Band.high, Band.medium) == Band.medium
    assert combine_bands(Band.high, Band.low) == Band.low
    assert combine_bands(Band.abstain, Band.high) == Band.abstain
    assert combine_bands(Band.high, Band.abstain) == Band.abstain
    assert combine_bands(Band.high, Band.high) == Band.high


def test_grounding_caps_a_large_correlational_causal_sample_at_low() -> None:
    # Big, recent, well-converted sample — strong on the DATA axis...
    evidence = _recent_evidence(purchases=500.0, spend=50_000.0)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.correlational,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
        causal_text="scaled because the new audience converts",
    )
    # ...but the grounding cap (correlational ceiling medium, downgraded one for the causal claim)
    # governs: the combined band can be at most low. Sample size must NOT average the cap away.
    assert conf.data_band == Band.high
    assert conf.grounding_band == Band.low
    assert conf.band == Band.low
    assert conf.causal_flag is True
    assert "correlational — confirm via A/B" in conf.factors


def test_ab_experiment_with_significance_reads_high_despite_causal_language() -> None:
    evidence = _recent_evidence(purchases=500.0, spend=50_000.0)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.ab_experiment,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
        pvalue=0.01,
        causal_text="the new audience drives ROAS",
    )
    # Experiment IS the causal evidence: grounding no longer caps and the causal guard does not
    # downgrade an experiment-backed claim.
    assert conf.data_band == Band.high
    assert conf.grounding_band == Band.high
    assert conf.band == Band.high
    assert conf.causal_flag is True


def test_below_floor_inputs_abstain_not_low() -> None:
    evidence = _recent_evidence(purchases=3.0, spend=40.0)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    assert conf.data_band == Band.abstain
    assert conf.band == Band.abstain  # NOT low — abstain is a first-class verdict
    assert any("floor" in factor for factor in conf.factors)


def test_clearing_only_spend_floor_is_thin_on_conversions() -> None:
    # Spend cleared ($500 > $100) but conversions did not (2 < 25): weak data on the outcome → low.
    band, factors = data_strength(
        sample_purchases=2.0,
        sample_spend=500.0,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    assert band == Band.low
    assert any("thin on conversions" in factor for factor in factors)


def test_missing_sample_drives_abstain_no_model_typed_score() -> None:
    # A caller that cannot supply sample data passes None — which drives abstain, never a guess.
    evidence = _recent_evidence(purchases=None, spend=None)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.ab_experiment,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    assert conf.band == Band.abstain


def test_assess_exposes_no_pre_baked_band_parameter() -> None:
    import inspect

    params = set(inspect.signature(assess).parameters)
    # The ONLY path to a band is through deterministic inputs; no caller-set band/score knob.
    assert not (params & {"band", "score", "confidence", "data_band", "grounding_band"})


def test_abstain_confidence_factory_pins_data_axis_and_keeps_grounding_ceiling() -> None:
    # A caller whose own domain gate refuses to score (e.g. a well-funded but too-young ad whose
    # sample WOULD clear the floor) gets abstain via the sanctioned factory — the data axis is pinned
    # to the floor, NOT a number, while grounding still reports the tier's honest ceiling.
    conf = abstain_confidence(
        tier=EvidenceTier.direct_observation,
        factors=["too young to judge — abstain, keep running"],
        would_raise="a matured (post-learning) window",
    )
    assert conf.data_band is Band.abstain
    assert conf.grounding_band is Band.high          # direct_observation ceiling, honestly reported
    assert conf.band is Band.abstain                 # weaker axis governs
    assert conf.grounding_tier == "direct_observation"
    assert conf.causal_flag is False
    # Round-trips through the shared serializer like any other verdict.
    assert confidence_to_dict(conf)["band"] == "abstain"


def test_abstain_confidence_factory_exposes_no_band_knob() -> None:
    import inspect

    # The factory is an explicit *refusal* to score, not a back door to a caller-chosen band.
    params = set(inspect.signature(abstain_confidence).parameters)
    assert not (params & {"band", "score", "data_band", "grounding_band", "confidence"})


def test_detect_causal_language() -> None:
    assert detect_causal_language("scaled because the new audience converts") is True
    assert detect_causal_language("the new creative drives ROAS") is True
    assert detect_causal_language("paused due to fatigue") is True
    assert detect_causal_language("the change leads to more purchases") is True
    # Descriptive, no causal verb:
    assert detect_causal_language("ROAS is 1.2 over 14 days") is False
    assert detect_causal_language("") is False
    assert detect_causal_language(None) is False


def test_build_regenerating_query_exact_string_and_none_on_missing() -> None:
    assert build_regenerating_query("divine_designs", "ad", "2026-06-10", "2026-06-24") == (
        "account_metrics --account divine_designs --level ad "
        "--date-from 2026-06-10 --date-to 2026-06-24"
    )
    assert build_regenerating_query("divine_designs", None, "2026-06-10", "2026-06-24") is None
    assert build_regenerating_query("divine_designs", "ad", "2026-06-10", None) is None
    assert build_regenerating_query(None, "ad", "2026-06-10", "2026-06-24") is None


def test_stale_window_rounds_data_band_down_vs_recent() -> None:
    kwargs = dict(
        sample_purchases=500.0,
        sample_spend=50_000.0,
        spend_floor=100.0,
        conversions_floor=25.0,
    )
    recent_band, _ = data_strength(recency_days=1, **kwargs)
    stale_band, stale_factors = data_strength(recency_days=CONFIDENCE_RECENCY_STALE_DAYS + 40, **kwargs)
    assert recent_band == Band.high
    assert stale_band == Band.medium  # rounded down exactly one level
    assert stale_band < recent_band
    assert any("stale window" in factor for factor in stale_factors)


def test_unknown_recency_rounds_down() -> None:
    band, factors = data_strength(
        sample_purchases=500.0,
        sample_spend=50_000.0,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=None,
    )
    assert band == Band.medium  # high base, rounded down because recency is unknown
    assert any("recency unknown" in factor for factor in factors)


def test_non_significant_pvalue_caps_data_at_medium() -> None:
    band, factors = data_strength(
        sample_purchases=500.0,
        sample_spend=50_000.0,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
        pvalue=0.20,
    )
    assert band == Band.medium
    assert any("not significant" in factor for factor in factors)


def test_grounding_tier_ceilings_and_causal_guard() -> None:
    assert grounding_strength(EvidenceTier.ab_experiment, causal_claim=False)[0] == Band.high
    assert grounding_strength(EvidenceTier.direct_observation, causal_claim=False)[0] == Band.high
    assert grounding_strength(EvidenceTier.correlational, causal_claim=False)[0] == Band.medium
    assert grounding_strength(EvidenceTier.external, causal_claim=False)[0] == Band.low
    assert grounding_strength(EvidenceTier.model_inference, causal_claim=False)[0] == Band.low
    # Causal guard downgrades non-experimental claims one band (floored at low)...
    assert grounding_strength(EvidenceTier.correlational, causal_claim=True)[0] == Band.low
    assert grounding_strength(EvidenceTier.external, causal_claim=True)[0] == Band.low
    # ...but never an A/B experiment (the experiment is the causal evidence).
    assert grounding_strength(EvidenceTier.ab_experiment, causal_claim=True)[0] == Band.high
    # Strings coerce to the same tiers.
    assert grounding_strength("correlational", causal_claim=False)[0] == Band.medium


def test_band_presentation_matches_knowledge_vocabulary_exactly() -> None:
    # Pin the one vocabulary so knowledge/README.md and confidence.py cannot drift into two scales.
    assert BAND_PRESENTATION[Band.high] == {"emoji": "🟢", "label": "High", "range": "~80–100%"}
    assert BAND_PRESENTATION[Band.medium] == {"emoji": "🟡", "label": "Medium", "range": "~50–80%"}
    assert BAND_PRESENTATION[Band.low] == {"emoji": "🔴", "label": "Low", "range": "<50%"}
    assert BAND_PRESENTATION[Band.abstain] == {
        "emoji": "⚪",
        "label": "Insufficient data — abstain",
        "range": "—",
    }


def test_band_vocabulary_actually_appears_in_knowledge_readme() -> None:
    # The pin above guards the code constants; this one closes the loop the implement handoff
    # claimed but did not enforce — that the SAME emoji+label live in knowledge/README.md, so the
    # human rubric and the computed rubric genuinely cannot drift into two scales. If someone edits
    # the README's emoji/label (or the code's), one of these assertions fails.
    from meta_ads_analysis.config import PROJECT_ROOT

    readme = (PROJECT_ROOT / "knowledge" / "README.md").read_text(encoding="utf-8")
    for band in (Band.high, Band.medium, Band.low, Band.abstain):
        pres = BAND_PRESENTATION[band]
        assert pres["emoji"] in readme, f"{band.name} emoji {pres['emoji']!r} missing from README"
        assert pres["label"] in readme, f"{band.name} label {pres['label']!r} missing from README"


def test_grounding_tier_ceilings_match_knowledge_readme() -> None:
    # Sibling to the band-vocabulary pin: the README "Grounding tiers" table documents each
    # EvidenceTier's ceiling band, and that mapping IS the code's _TIER_CEILING. Without this pin the
    # table could silently drift (e.g. someone raises external to Medium in prose but not in code, or
    # vice versa). Assert that the row naming each tier carries that tier's true ceiling emoji+label.
    from meta_ads_analysis.config import PROJECT_ROOT
    from meta_ads_analysis.confidence import _TIER_CEILING

    readme = (PROJECT_ROOT / "knowledge" / "README.md").read_text(encoding="utf-8")
    lines = readme.splitlines()
    for tier, ceiling in _TIER_CEILING.items():
        pres = BAND_PRESENTATION[ceiling]
        # The table wraps each tier name in backticks; find the row that names it.
        rows = [ln for ln in lines if f"`{tier.name}`" in ln]
        assert rows, f"EvidenceTier {tier.name!r} missing from README grounding-tier table"
        assert any(pres["emoji"] in ln and pres["label"] in ln for ln in rows), (
            f"{tier.name} should document ceiling {pres['emoji']} {pres['label']} in its README row"
        )


def test_review_verdict_taxonomy_appears_in_docs() -> None:
    # Sibling to the vocabulary/tier pins above. The AGENTS.md "Adversarial-review rule" and the
    # knowledge/README.md two-layer-review subsection both name the four verdicts that review.py
    # emits. Pin the SOURCE-OF-TRUTH verdict strings (review.py's VERDICT_* constants) to both docs
    # so a rename in code (or a drifted doc) fails here instead of silently desyncing the prose from
    # the gate. (The six per-check names are intentionally NOT pinned — the docs spell them as prose,
    # e.g. "causal-cap"/"external-cap", which deliberately differ from the code's failed_input
    # identifiers "causal"/"external", so a verbatim pin would be fragile in both directions.)
    from meta_ads_analysis.config import PROJECT_ROOT
    from meta_ads_analysis.review import (
        VERDICT_DOWNGRADE,
        VERDICT_INSUFFICIENT,
        VERDICT_REFUTED,
        VERDICT_STANDS,
    )

    verdicts = (VERDICT_STANDS, VERDICT_DOWNGRADE, VERDICT_REFUTED, VERDICT_INSUFFICIENT)
    for rel in ("AGENTS.md", "knowledge/README.md"):
        doc = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for verdict in verdicts:
            assert verdict in doc, f"verdict {verdict!r} missing from {rel}"


def test_render_helpers_produce_compact_lines() -> None:
    evidence = _recent_evidence(purchases=42.0, spend=1250.0)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.correlational,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    conf_line = render_confidence_line(conf)
    assert BAND_PRESENTATION[conf.band]["emoji"] in conf_line
    assert BAND_PRESENTATION[conf.band]["label"] in conf_line

    ev_line = render_evidence_line(evidence)
    assert "ROAS 1.20" in ev_line
    assert "2026-06-10..2026-06-24" in ev_line
    assert "42 conversions" in ev_line
    assert "account_metrics --account divine_designs" in ev_line


def test_band_ordering_is_abstain_low_medium_high() -> None:
    assert Band.abstain < Band.low < Band.medium < Band.high


def test_evidence_and_confidence_dicts_round_trip() -> None:
    evidence = _recent_evidence(purchases=120.0, spend=2400.0)
    conf = assess(
        evidence=evidence,
        tier=EvidenceTier.direct_observation,
        spend_floor=100.0,
        conversions_floor=25.0,
        recency_days=1,
    )
    # to_dict stores bands as their lowercase NAME, never a number.
    conf_dict = confidence_to_dict(conf)
    assert conf_dict["band"] == "high"
    assert conf_dict["grounding_tier"] == "direct_observation"
    assert evidence_to_dict(evidence)["regenerating_query"].startswith("account_metrics --account")
    # Round-trip is lossless for the fields the downstream brief renderer needs.
    assert confidence_from_dict(conf_dict).band is Band.high
    rebuilt = evidence_from_dict(evidence_to_dict(evidence))
    assert rebuilt.metric_display == "ROAS 1.20"
    assert rebuilt.sample_purchases == 120.0


# ---------------------------------------------------------------------------
# Action plan: evidence + confidence + abstention (confidence-actions-analyze)
# ---------------------------------------------------------------------------


def _use_account_policy(tmp_path: Path, monkeypatch, slug: str, policy: dict[str, Any]) -> None:
    accounts_path = tmp_path / "meta_ads_accounts.json"
    accounts_path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account_slug": slug,
                        "account_name": slug.replace("_", " ").title(),
                        "ad_account_id": "act_test",
                        "action_policy": policy,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "meta_ads_analysis.account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH", accounts_path
    )


def _pause_ad_payload(
    *,
    ad_overrides: dict[str, Any],
    run_date: str = "2026-06-24",
    account_slug: str = "divine_designs",
) -> dict[str, Any]:
    ad = {
        "ad_id": "123",
        "ad_name": "Cody - Copy",
        "campaign_name": "Campaign",
        "adset_name": "Ad Set",
        "total_results": 0.0,
        "total_app_installs": 0.0,
        "waste_score": 90.0,
        "waste_status": "high",
        "waste_reasons": ["spent without proportional value"],
        "tracking_confidence": "high",
    }
    ad.update(ad_overrides)
    return {
        "account_slug": account_slug,
        "run_date": run_date,
        "budget_waste": [ad],
        "fatigue_findings": [],
        "scaling_candidates": [],
        "tracking_concerns": [],
    }


def test_action_plan_pause_carries_high_confidence_and_direct_observation(tmp_path, monkeypatch) -> None:
    _use_account_policy(tmp_path, monkeypatch, "divine_designs", {"primary_goal": "roas"})
    payload = _pause_ad_payload(
        ad_overrides={
            "blended_roas": 1.2,
            "total_purchase_count": 120.0,
            "total_spend": 2400.0,
            "first_seen": "2026-06-10",
            "last_seen": "2026-06-24",
        }
    )

    plan = build_action_plan(payload)

    pause = next(action for action in plan["actions"] if action["action_type"] == "pause_ad")
    # The headline use case: a well-sampled, recent, directly-observed pause reads High.
    assert pause["confidence"]["band"] == "high"
    assert pause["confidence"]["grounding_tier"] == "direct_observation"
    assert pause["confidence"]["causal_flag"] is False
    # Still a confident, executable pause — the evidence/confidence shape is additive.
    assert pause["executable"] is True
    assert pause["status"] == "proposed"
    assert "verdict" not in pause
    # Evidence carries the four facts + a real (non-null) regenerating query.
    evidence = pause["evidence"]
    assert evidence["metric_name"] == "blended_roas"
    assert evidence["metric_display"] == "ROAS 1.20"
    assert evidence["window"] == "2026-06-10..2026-06-24"
    assert evidence["sample_purchases"] == 120.0
    assert evidence["sample_spend"] == 2400.0
    assert evidence["entity_level"] == "ad"
    assert evidence["entity_id"] == "123"
    assert evidence["entity_name"] == "Cody - Copy"
    assert evidence["regenerating_query"] == (
        "account_metrics --account divine_designs --level ad "
        "--date-from 2026-06-10 --date-to 2026-06-24"
    )


def test_action_plan_pause_with_43_purchases_reads_medium_under_calibrated_knee(
    tmp_path, monkeypatch
) -> None:
    # NOTE: the ticket's headline example calls 43 purchases "🟢 High ~85%", but the SHIPPED
    # confidence-core rubric only reaches `high` at >= 4x the conversions floor (>= 100 purchases);
    # 43 clears the floor but lands at `medium`. This test pins the real computed band so the
    # discrepancy is visible (confidence-core was reviewed/accepted and is out of scope here).
    _use_account_policy(tmp_path, monkeypatch, "divine_designs", {"primary_goal": "roas"})
    payload = _pause_ad_payload(
        ad_overrides={
            "blended_roas": 1.2,
            "total_purchase_count": 43.0,
            "total_spend": 880.0,
            "first_seen": "2026-06-10",
            "last_seen": "2026-06-24",
        }
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    assert pause["confidence"]["band"] == "medium"
    assert pause["confidence"]["grounding_tier"] == "direct_observation"
    assert pause["executable"] is True
    assert pause["evidence"]["sample_purchases"] == 43.0
    assert pause["evidence"]["regenerating_query"] is not None


def test_action_plan_pause_below_floor_abstains_as_keep_running(tmp_path, monkeypatch) -> None:
    _use_account_policy(tmp_path, monkeypatch, "divine_designs", {"primary_goal": "roas"})
    payload = _pause_ad_payload(
        ad_overrides={
            "blended_roas": 1.2,
            "total_purchase_count": 3.0,
            "total_spend": 40.0,
            "first_seen": "2026-06-20",
            "last_seen": "2026-06-23",
        }
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    # Below the significance floor: a non-executable "insufficient data — keep running" rec, NOT a
    # confident pause. Cannot be approved into a write (approval_required is False).
    assert pause["confidence"]["band"] == "abstain"
    assert pause["verdict"] == "insufficient_data"
    assert pause["executable"] is False
    assert pause["approval_required"] is False
    assert pause["status"] == "proposed"
    rationale = pause["rationale"].lower()
    assert "keep running" in rationale
    assert "winner" not in rationale
    assert "loser" not in rationale


def test_action_plan_zero_sample_ad_abstains_never_fabricates_pause(tmp_path, monkeypatch) -> None:
    _use_account_policy(tmp_path, monkeypatch, "divine_designs", {"primary_goal": "roas"})
    payload = _pause_ad_payload(
        ad_overrides={"total_purchase_count": 0.0, "total_spend": 0.0}
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    assert pause["confidence"]["band"] == "abstain"
    assert pause["verdict"] == "insufficient_data"
    assert pause["executable"] is False
    assert pause["approval_required"] is False


def test_action_plan_install_goal_grounds_significance_on_app_installs(tmp_path, monkeypatch) -> None:
    # The headline fix: an install-goal account reports 0 purchases, so before this change its sample
    # was 0 and the band was stuck at `low` (spend cleared, "thin on conversions"). Now significance is
    # grounded on the conversion type that fits the goal — app installs when no subscription results —
    # so a pause backed by real install volume reads above `low`.
    _use_account_policy(
        tmp_path, monkeypatch, "pollen_sense", {"primary_goal": "maximize_in_app_subscriptions"}
    )
    payload = _pause_ad_payload(
        account_slug="pollen_sense",
        ad_overrides={
            "total_purchase_count": 0.0,   # install accounts rarely have purchases
            "total_results": 0.0,          # no in-app subscriptions yet
            "total_app_installs": 100.0,   # but real install volume (>= 4 * floor of 25)
            "cost_per_app_install": 2.50,
            "total_spend": 250.0,
            "first_seen": "2026-06-10",
            "last_seen": "2026-06-24",
        },
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    # 100 installs >= 4 * 25 floor + recent + direct_observation ceiling → high (definitely not low).
    assert pause["confidence"]["band"] == "high"
    assert pause["confidence"]["band"] not in {"low", "abstain"}
    # Significance is grounded on the installs that actually back the call, not the (zero) purchases.
    evidence = pause["evidence"]
    assert evidence["sample_purchases"] == 100.0
    assert evidence["metric_name"] == "cost_per_app_install"
    assert pause["executable"] is True


def test_action_plan_install_goal_grounds_on_subscriptions_not_installs(tmp_path, monkeypatch) -> None:
    # Decision-1 tradeoff, pinned so it is a choice and not an accident: when in-app subscription
    # results are present at all, significance grounds on THOSE (the account's real commercial signal),
    # NOT on a richer app-install count. So a handful of subscriptions honestly stays thin/`low` even
    # though the ad has plenty of installs — the installs fallback is only for "no subscriptions yet".
    _use_account_policy(
        tmp_path, monkeypatch, "pollen_sense", {"primary_goal": "maximize_in_app_subscriptions"}
    )
    payload = _pause_ad_payload(
        account_slug="pollen_sense",
        ad_overrides={
            "total_purchase_count": 0.0,
            "total_results": 3.0,          # a few subscriptions present → these are the signal
            "total_app_installs": 80.0,    # many installs, but NOT used because results > 0
            "cost_per_app_install": 3.10,
            "total_spend": 250.0,
            "first_seen": "2026-06-10",
            "last_seen": "2026-06-24",
        },
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    # Grounds on the 3 subscriptions, NOT the 80 installs.
    assert pause["evidence"]["sample_purchases"] == 3.0
    # 3 conversions is below the 25 floor (spend cleared) → thin-on-conversions → low. Conservative,
    # and exactly the intended behavior: subscriptions are thin, so the call stays thin.
    assert pause["confidence"]["band"] == "low"


def test_action_plan_install_goal_abstains_when_both_signals_and_spend_are_thin(
    tmp_path, monkeypatch
) -> None:
    # Install ad with no subscriptions, few installs, AND spend below the floor → both axes fail →
    # abstain. For the guarded pause path that flips to a non-executable "keep running" recommendation.
    _use_account_policy(
        tmp_path, monkeypatch, "pollen_sense", {"primary_goal": "maximize_in_app_subscriptions"}
    )
    payload = _pause_ad_payload(
        account_slug="pollen_sense",
        ad_overrides={
            "total_purchase_count": 0.0,
            "total_results": 0.0,
            "total_app_installs": 5.0,     # below the 25 conversions floor
            "cost_per_app_install": 8.0,
            "total_spend": 40.0,           # below the MIN_WASTE_SPEND ($100) spend floor
            "first_seen": "2026-06-20",
            "last_seen": "2026-06-23",
        },
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    assert pause["confidence"]["band"] == "abstain"
    assert pause["verdict"] == "insufficient_data"
    assert pause["executable"] is False
    assert pause["approval_required"] is False
    # The sample grounded on installs (5), and the operator-facing rationale says "conversions".
    assert pause["evidence"]["sample_purchases"] == 5.0
    assert "conversions" in pause["rationale"]


def test_action_plan_roas_goal_still_grounds_on_purchase_count(tmp_path, monkeypatch) -> None:
    # Non-install goals are byte-identical to before: a ROAS account grounds significance on
    # total_purchase_count and never leaks the install/subscription fallback, even if those fields
    # happen to be populated.
    _use_account_policy(tmp_path, monkeypatch, "divine_designs", {"primary_goal": "roas"})
    payload = _pause_ad_payload(
        ad_overrides={
            "blended_roas": 1.2,
            "total_purchase_count": 120.0,
            "total_results": 999.0,        # present, but must be ignored for a ROAS account
            "total_app_installs": 999.0,   # ditto
            "total_spend": 2400.0,
            "first_seen": "2026-06-10",
            "last_seen": "2026-06-24",
        },
    )

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    assert pause["evidence"]["sample_purchases"] == 120.0  # the purchase count, not 999
    assert pause["evidence"]["metric_name"] == "blended_roas"


def test_evaluate_action_confidence_flags_causal_correlational_and_caps_band() -> None:
    ad = {
        "ad_id": "scale-1",
        "ad_name": "New Audience",
        "blended_roas": 4.0,
        "total_purchase_count": 300.0,
        "total_spend": 9000.0,
        "first_seen": "2026-06-10",
        "last_seen": "2026-06-24",
    }
    _evidence, confidence = evaluate_action_confidence(
        ad,
        action_type="consider_scale_budget",
        policy={"primary_goal": "roas"},
        account_slug="divine_designs",
        run_date="2026-06-24",
        rationale="Scale because the new audience converts",
    )
    # Trajectory/scale-candidate calls lean on a cross-sectional comparison → correlational.
    assert confidence["grounding_tier"] == "correlational"
    assert confidence["causal_flag"] is True
    # Grounding caps the large, recent sample: correlational ceiling medium, downgraded one for the
    # causal claim → low. Sample size must NOT average the cap away.
    assert confidence["data_band"] == "high"
    assert confidence["band"] == "low"


def test_action_plan_pause_keeps_rationale_and_params_backward_compatible() -> None:
    # The executable pause path's behavior is unchanged; confidence/evidence are purely additive.
    payload = {
        "account_slug": "pollen_sense",
        "run_date": "2026-05-04",
        "budget_waste": [
            {
                "ad_id": "123",
                "ad_name": "Waste Ad",
                "total_spend": 250.0,
                "total_results": 0.0,
                "total_app_installs": 1.0,
                "waste_score": 82.0,
                "waste_status": "high",
                "waste_reasons": ["spent without results"],
                "tracking_confidence": "medium_roas_unavailable",
            }
        ],
        "fatigue_findings": [],
        "scaling_candidates": [],
        "tracking_concerns": [],
    }

    pause = next(a for a in build_action_plan(payload)["actions"] if a["action_type"] == "pause_ad")
    assert pause["executable"] is True
    assert pause["approval_required"] is True
    assert pause["params"] == {"status": "paused"}
    assert pause["rationale"].startswith("High waste risk")
    assert "confidence" in pause
    assert pause["evidence"]["entity_id"] == "123"


def test_recommendations_prose_carries_metric_window_sample_facts() -> None:
    rows = [
        {
            "report_date": date(2026, 6, 1) + timedelta(days=offset),
            "campaign_id": "campaign-1",
            "campaign_name": "Waste Campaign",
            "adset_id": "adset-1",
            "adset_name": "Waste Set",
            "ad_id": "waste-ad",
            "ad_name": "Waste Ad",
            "creative_type": "Image",
            "spend": 60.0,
            "purchase_value": 72.0,
            "purchase_count": 3.0,
            "results": 3.0,
            "result_label": "Website purchases",
            "app_installs": 0.0,
            "impressions": 5000,
            "outbound_clicks": 50,
            "frequency": 2.0,
            "video_3s_plays": 0,
            "thruplays": 0,
            "has_video_metrics": False,
            "tracking_confidence": "high",
        }
        for offset in range(6)
    ]

    report = build_report_payload(rows, "2026-06-16")
    waste_line = next(
        (line for line in report["next_7_day_actions"] if line.startswith("Reduce or pause budget")),
        None,
    )
    assert waste_line is not None
    # Metric, window, sample, and spend facts are inline so the prose is grounded.
    assert "ROAS" in waste_line
    assert "purchases" in waste_line
    assert "spend" in waste_line
    assert "over" in waste_line and "d," in waste_line


# ---------------------------------------------------------------------------
# Knowledge-vault provenance format + lint-vault
# (src/meta_ads_analysis/knowledge_provenance.py)
# ---------------------------------------------------------------------------

_CLEAN_VAULT = """# Durable learnings

Intro prose that is not an entry and must be ignored.

## Meta platform & API behavior

### Dev-mode app blocker is a platform mechanic
**Confidence:** 🟢 High →  ·  **Domain:** platform
**Rot:** evergreen  ·  **Verified:** 2026-01-01
- ➕ 2026-01-01 — validate-only POST rejected on all 3 ad sets; matches documented behavior.
  This evidence wraps across **two** physical lines and the tag closes here. _(src: direct_observation · acct: divine_designs)_
- ➖ 2026-01-02 — one counter-observation that lowers nothing yet. _(src: correlational · acct: divine_designs)_
**Apply:** check issues_info first.
**Would raise / lower:** a second account reproducing it.

### Engaged audience holds higher ROAS
**Confidence:** 🔴 Low ↑  ·  **Domain:** strategy
**Rot:** fast  ·  **Verified:** 2026-01-10
- ➕ 2026-01-10 — engaged ad set 3.74 ROAS vs low-value 2.04, confounded by creative mix.
  `verify: account_metrics --account divine_designs --level adset --date-from 2025-12-11 --date-to 2026-01-10`
  _(src: correlational · acct: divine_designs · metric: engaged_roas=3.74)_
**Apply:** treat as a hunch only.

## Tooling capabilities (factual reference — not a probabilistic claim)

- `sync-api` — a plain tooling bullet that is NOT an entry and must be ignored.
"""


def _entry(*body_lines: str, header: str = "X claim", confidence: str = "🟢 High →",
           rot: str = "evergreen", verified: str = "2026-01-01") -> str:
    head = [f"### {header}", f"**Confidence:** {confidence}  ·  **Domain:** platform"]
    if rot is not None and verified is not None:
        head.append(f"**Rot:** {rot}  ·  **Verified:** {verified}")
    elif rot is not None:
        head.append(f"**Rot:** {rot}")
    elif verified is not None:
        head.append(f"**Verified:** {verified}")
    return "\n".join(["## Section", "", *head, *body_lines, ""])


def _codes(findings, severity=None) -> set:
    return {f.code for f in findings if severity is None or f.severity == severity}


def test_parse_learnings_extracts_structured_fields() -> None:
    entries = parse_learnings(_CLEAN_VAULT)
    # Two `###` entries; the intro prose and the tooling bullet are ignored.
    assert len(entries) == 2

    dev = entries[0]
    assert dev.claim == "Dev-mode app blocker is a platform mechanic"
    assert dev.band_emoji == "🟢"
    assert dev.domain == "platform"
    assert dev.rot == "evergreen"
    assert dev.verified == "2026-01-01"
    # lineno points at the `### ` header line (1-indexed).
    assert _CLEAN_VAULT.splitlines()[dev.lineno - 1].startswith("### ")
    assert len(dev.evidence) == 2

    ev0 = dev.evidence[0]
    assert ev0.sign == "+" and ev0.date == "2026-01-01"
    assert ev0.tier == "direct_observation" and ev0.account == "divine_designs"
    assert ev0.metric_name is None and ev0.metric_value is None
    assert ev0.verify_query is None and ev0.url is None and ev0.has_tag is True
    # The multi-physical-line evidence was rejoined: text from both lines is present, tag stripped.
    assert "validate-only POST" in ev0.text
    assert "wraps across" in ev0.text
    assert "src:" not in ev0.text

    assert dev.evidence[1].sign == "-" and dev.evidence[1].tier == "correlational"

    eng = entries[1]
    assert eng.rot == "fast" and eng.verified == "2026-01-10"
    ev = eng.evidence[0]
    assert ev.tier == "correlational" and ev.account == "divine_designs"
    assert ev.metric_name == "engaged_roas" and ev.metric_value == 3.74
    assert ev.verify_query is not None and ev.verify_query.startswith("account_metrics --account divine_designs")
    assert ev.metric_selector is None  # no `select:` in the tag → None (token-heuristic fallback)


def test_parse_evidence_selector_field() -> None:
    # A `select:` field parses into a {key: value} dict alongside metric:/src:/acct:.
    one = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account --breakdown publisher_platform` "
        "_(src: correlational · acct: d · metric: ig_roas=3.63 · select: publisher_platform=instagram)_",
        header="single-key select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert one.metric_selector == {"publisher_platform": "instagram"}

    # Multi-key: comma-separated pairs; the commas inside the value don't collide with the `·`
    # field separator, so both pairs survive.
    multi = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account` "
        "_(src: correlational · acct: d · metric: r=4.5 · select: publisher_platform=instagram,platform_position=stories)_",
        header="multi-key select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert multi.metric_selector == {"publisher_platform": "instagram", "platform_position": "stories"}

    # Incidental whitespace around commas / pairs is tolerated — a space after the comma must NOT
    # silently drop the trailing pair (it would otherwise resolve a coarser slice → wrong blend).
    spaced = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account` "
        "_(src: correlational · acct: d · metric: r=4.5 · select: publisher_platform=instagram, platform_position=stories)_",
        header="spaced select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert spaced.metric_selector == {"publisher_platform": "instagram", "platform_position": "stories"}

    # A `select:` placed BEFORE another tag field stops at the `·` separator (does not swallow acct).
    mid = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account` "
        "_(src: correlational · metric: r=4.5 · select: publisher_platform=instagram · acct: d)_",
        header="mid select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert mid.metric_selector == {"publisher_platform": "instagram"} and mid.account == "d"

    # Malformed (a bare key with no `=`) → None → falls back to the token heuristic (no crash).
    bad = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account` "
        "_(src: correlational · acct: d · metric: r=4.5 · select: publisher_platform)_",
        header="malformed select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert bad.metric_selector is None

    # Absent → None.
    none = parse_learnings(_entry(
        "- ➕ 2026-01-01 — x. `verify: account_metrics --account d --level account` "
        "_(src: correlational · acct: d · metric: r=4.5)_",
        header="no select", rot="fast", verified="2026-01-01",
    ))[0].evidence[0]
    assert none.metric_selector is None


def test_parse_evidence_selector_does_not_bleed_across_sibling_lines() -> None:
    # Two evidence bullets in ONE entry parse independently — a `select:` on one must not leak onto
    # the other (each is its own EvidenceLine).
    evs = parse_learnings(_entry(
        "- ➕ 2026-01-01 — a. `verify: account_metrics --account d --level account --breakdown publisher_platform,platform_position` "
        "_(src: correlational · acct: d · metric: ig_roas=3.63 · select: publisher_platform=instagram)_",
        "- ➕ 2026-01-01 — b. `verify: account_metrics --account d --level account --breakdown publisher_platform` "
        "_(src: correlational · acct: d · metric: fb_roas=2.55)_",
        header="two bullets", rot="fast", verified="2026-01-01",
    ))[0].evidence
    assert evs[0].metric_selector == {"publisher_platform": "instagram"}
    assert evs[1].metric_selector is None


def test_lint_clean_vault_has_no_findings() -> None:
    entries = parse_learnings(_CLEAN_VAULT)
    findings = lint(entries, today=date(2026, 1, 15))  # 5d after the fast entry's Verified
    assert findings == []


def test_lint_errors_untagged_evidence_line() -> None:
    text = _entry("- ➕ 2026-01-01 — an evidence line with no provenance tag at all.")
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    assert "missing_tag" in _codes(findings, "error")


def test_tag_on_field_label_continuation_fails_loudly_not_silently() -> None:
    # Documented parser-boundary tradeoff: a bold *field label* (`**Note:**`) at the start of a
    # continuation line ends the evidence block, so a `_( … )_` tag stranded on such a line does NOT
    # join. The point of this guard is the FAILURE DIRECTION — the orphaned-tag evidence must surface
    # a loud `missing_tag` error, never silently pass as tagged. (Inline emphasis like `**not**` —
    # no colon — does NOT end a block; that path is exercised by _CLEAN_VAULT's wrapped evidence.)
    text = _entry(
        "- ➕ 2026-01-01 — first physical line of the evidence",
        "  **Note:** continuation that mistakenly carries the tag "
        "_(src: direct_observation · acct: divine_designs)_",
    )
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    assert "missing_tag" in _codes(findings, "error")


def test_lint_errors_invalid_src_tier() -> None:
    text = _entry("- ➕ 2026-01-01 — bad tier. _(src: bogus_tier · acct: divine_designs)_")
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    assert "invalid_src" in _codes(findings, "error")


def test_lint_errors_missing_rot_and_verified() -> None:
    text = _entry(
        "- ➕ 2026-01-01 — fine evidence. _(src: direct_observation · acct: divine_designs)_",
        rot=None,
        verified=None,
    )
    codes = _codes(lint(parse_learnings(text), today=date(2026, 1, 2)), "error")
    assert "missing_rot" in codes and "missing_verified" in codes


def test_lint_errors_metric_without_verify_command() -> None:
    text = _entry(
        "- ➕ 2026-01-01 — cites a number. _(src: correlational · acct: divine_designs · metric: roas=3.0)_"
    )
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    assert "metric_without_verify" in _codes(findings, "error")


def test_lint_errors_external_without_url() -> None:
    text = _entry("- ➕ 2026-01-01 — practitioner says X. _(src: external · acct: —)_")
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    assert "external_without_url" in _codes(findings, "error")
    # A URL anywhere on the line satisfies the rule.
    ok = _entry(
        "- ➕ 2026-01-01 — practitioner says X, see https://example.com/post . _(src: external · acct: —)_"
    )
    assert "external_without_url" not in _codes(lint(parse_learnings(ok), today=date(2026, 1, 2)), "error")


def test_lint_warns_select_recommended_for_multi_breakdown_metric_without_selector() -> None:
    # A metric sliced by ≥2 breakdowns with no `select:` is the exact class audit-vault's token
    # heuristic can't resolve → one `select_recommended` WARN (never an error).
    text = _entry(
        "- ➕ 2026-01-01 — IG 3.63 vs FB. "
        "`verify: account_metrics --account divine_designs --level account "
        "--date-from 2026-02-23 --date-to 2026-06-23 --breakdown publisher_platform,platform_position` "
        "_(src: correlational · acct: divine_designs · metric: ig_roas=3.63)_",
        header="two-dim no selector", rot="fast", verified="2026-01-01",
    )
    findings = lint(parse_learnings(text), today=date(2026, 1, 2))
    warns = [f for f in findings if f.code == "select_recommended"]
    assert len(warns) == 1 and warns[0].severity == "warn"
    assert _codes(findings, "error") == set()  # warn-not-error keeps the entry lint-clean


def test_lint_no_select_warn_when_selector_present_or_single_breakdown() -> None:
    # With a selector the two-dim metric is resolvable → no nudge.
    with_sel = _entry(
        "- ➕ 2026-01-01 — IG 3.63. "
        "`verify: account_metrics --account divine_designs --level account "
        "--breakdown publisher_platform,platform_position` "
        "_(src: correlational · acct: divine_designs · metric: ig_roas=3.63 · select: publisher_platform=instagram)_",
        header="two-dim with selector", rot="fast", verified="2026-01-01",
    )
    assert "select_recommended" not in _codes(lint(parse_learnings(with_sel), today=date(2026, 1, 2)))
    # A SINGLE-breakdown metric without a selector is fine (the token heuristic resolves it) → no nudge.
    single = _entry(
        "- ➕ 2026-01-01 — IG 2.79. "
        "`verify: account_metrics --account divine_designs --level account --breakdown publisher_platform` "
        "_(src: correlational · acct: divine_designs · metric: ig_roas=2.79)_",
        header="single-dim no selector", rot="fast", verified="2026-01-01",
    )
    assert "select_recommended" not in _codes(lint(parse_learnings(single), today=date(2026, 1, 2)))


def test_lint_staleness_flags_fast_but_never_evergreen() -> None:
    text = (
        _entry(
            "- ➕ 2026-01-01 — fast fact. _(src: correlational · acct: divine_designs)_",
            header="fast claim",
            rot="fast",
            verified="2026-01-01",
        )
        + _entry(
            "- ➕ 2025-06-01 — durable platform mechanic. _(src: direct_observation · acct: —)_",
            header="evergreen claim",
            rot="evergreen",
            verified="2025-06-01",
        )
    )
    entries = parse_learnings(text)
    # today is 50d after the fast entry's Verified (> default 42) and ~264d after the evergreen one.
    findings = lint(entries, today=date(2026, 2, 20))
    warns = [f for f in findings if f.severity == "warn"]
    assert len(warns) == 1
    assert warns[0].code == "reverify" and warns[0].claim == "fast claim"
    # No errors, and the 200+ day-old evergreen entry is NOT age-flagged.
    assert _codes(findings, "error") == set()
    assert all(f.claim != "evergreen claim" for f in findings)


def test_render_report_strict_turns_warnings_into_failure() -> None:
    text = _entry(
        "- ➕ 2026-01-01 — fast fact. _(src: correlational · acct: divine_designs)_",
        rot="fast",
        verified="2026-01-01",
    )
    findings = lint(parse_learnings(text), today=date(2026, 3, 1))  # well past 42 days
    assert any(f.severity == "warn" for f in findings)
    _, code_lenient = render_report(findings, entries_count=1, strict=False)
    _, code_strict = render_report(findings, entries_count=1, strict=True)
    assert code_lenient == 0  # warnings alone do not fail by default
    assert code_strict == 1   # --strict makes them fail


def _run_lint_vault_cli(tmp_path, monkeypatch, text, *, today="2026-01-15", strict=False) -> int:
    import pytest

    from meta_ads_analysis.cli import lint_vault_main

    learnings = tmp_path / "learnings.md"
    learnings.write_text(text, encoding="utf-8")
    argv = [
        "lint-vault",
        "--path", str(learnings),
        "--profile", str(tmp_path / "no-such-profile.md"),  # skipped (does not exist)
        "--today", today,
    ]
    if strict:
        argv.append("--strict")
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        lint_vault_main()
    code = exc.value.code
    return code if isinstance(code, int) else 1


def test_lint_vault_main_exits_zero_when_clean(tmp_path, monkeypatch, capsys) -> None:
    code = _run_lint_vault_cli(tmp_path, monkeypatch, _CLEAN_VAULT, today="2026-01-15")
    assert code == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_lint_vault_main_exits_nonzero_on_format_error(tmp_path, monkeypatch, capsys) -> None:
    bad = _entry("- ➕ 2026-01-01 — bad. _(src: bogus_tier · acct: divine_designs)_")
    code = _run_lint_vault_cli(tmp_path, monkeypatch, bad, today="2026-01-15")
    assert code == 1
    assert "ERROR" in capsys.readouterr().out


def test_lint_vault_main_strict_fails_on_stale_fast(tmp_path, monkeypatch) -> None:
    stale = _entry(
        "- ➕ 2026-01-01 — fast fact. _(src: correlational · acct: divine_designs)_",
        rot="fast",
        verified="2026-01-01",
    )
    # Without --strict the stale warning does not fail; with --strict it does.
    assert _run_lint_vault_cli(tmp_path, monkeypatch, stale, today="2026-06-01") == 0
    assert _run_lint_vault_cli(tmp_path, monkeypatch, stale, today="2026-06-01", strict=True) == 1


def test_provenance_tier_names_are_exactly_confidence_evidence_tier() -> None:
    # ONE vocabulary: the provenance `src` tiers must equal confidence.EvidenceTier so the vault
    # checker and the live engine cannot drift into two scales.
    assert TIER_NAMES == frozenset(t.name for t in EvidenceTier)


def test_provenance_band_emojis_match_confidence_presentation() -> None:
    assert BAND_EMOJIS == frozenset(
        BAND_PRESENTATION[b]["emoji"] for b in (Band.high, Band.medium, Band.low)
    )


def test_real_learnings_md_lints_with_zero_errors() -> None:
    # Meta-test: the committed knowledge/learnings.md, after the provenance retrofit, must lint
    # clean (errors). Deterministic because `today` is pinned. Warnings (re-verify) are allowed.
    from meta_ads_analysis.config import PROJECT_ROOT

    text = (PROJECT_ROOT / "knowledge" / "learnings.md").read_text(encoding="utf-8")
    entries = parse_learnings(text)
    assert entries, "expected the real learnings.md to contain entries"
    findings = lint(entries, today=date(2026, 6, 25))
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], f"real learnings.md has lint errors: {[(f.code, f.message) for f in errors]}"
    # Every entry has a rot class and a verified date after the retrofit.
    assert all(e.rot in {"fast", "evergreen"} and e.verified for e in entries)


def test_real_profile_baseline_header_is_present_and_fresh() -> None:
    from meta_ads_analysis.config import PROJECT_ROOT

    text = (PROJECT_ROOT / "knowledge" / "accounts" / "divine_designs" / "profile.md").read_text(
        encoding="utf-8"
    )
    # Fresh relative to the baseline date → no warning; far in the future → ⏳ re-verify warning.
    assert lint_profile_baseline(text, today=date(2026, 6, 25)) == []
    stale = lint_profile_baseline(text, today=date(2027, 1, 1))
    assert len(stale) == 1 and stale[0].code == "reverify"


# ---------------------------------------------------------------------------
# audit-vault — drift re-check (pure verdict + markdown mutation in
# knowledge_provenance.py; the Meta-touching orchestration in cli.py is exercised
# with a FAKE metrics provider — never live Meta).
# ---------------------------------------------------------------------------

# A single account-level, data-backed `fast` claim — the clean case `resolve_fresh_metric` can
# aggregate without segment matching. Stored window is 30 days (2025-12-12..2026-01-10).
_AUDIT_VAULT = """# Durable learnings

## Strategy

### Divine Designs blended ROAS sits comfortably above target
**Confidence:** 🟢 High →  ·  **Domain:** strategy
**Rot:** fast  ·  **Verified:** 2026-01-10
- ➕ 2026-01-10 — 30-day blended ROAS 3.74 on a healthy sample.
  `verify: account_metrics --account divine_designs --level account --date-from 2025-12-12 --date-to 2026-01-10`
  _(src: direct_observation · acct: divine_designs · metric: blended_roas=3.74)_
**Apply:** keep scaling.
"""


def _account_rows(*, roas: float | None, spend: float = 2000.0, purchases: float = 60.0):
    """One account-level metrics row. ``purchase_value`` is derived so the aggregate ROAS resolves to
    ``roas``; a ``None`` roas models a window with spend but no resolvable value."""
    value = round(roas * spend, 2) if roas is not None else None
    return [{"id": "act", "name": "account", "spend": spend, "purchase_value": value,
             "roas": roas, "purchases": purchases}]


def _fixed_fetch(rows):
    def fetch(level, breakdowns, date_from, date_to):
        return rows
    return fetch


def _audit(text, fetch, *, apply, as_of=date(2026, 6, 25)):
    from meta_ads_analysis.cli import run_vault_audit

    return run_vault_audit(
        text=text,
        account_slug="divine_designs",
        as_of=as_of,
        target_roas=3.0,
        pause_roas_floor=1.5,
        fetch_metrics=fetch,
        apply=apply,
    )


# --- pure verdict logic (classify_drift) -----------------------------------


def test_classify_drift_confirmed_when_fresh_matches_stored() -> None:
    # Stored 3.74, fresh 3.70 (≈1% drift, both above target) → confirmed.
    verdict, crossed, _ = classify_drift(
        stored_value=3.74, fresh=FreshSample(3.70, 60, 2000, "w"),
        target_roas=3.0, pause_roas_floor=1.5,
    )
    assert verdict == AUDIT_CONFIRMED and crossed is None


def test_classify_drift_refuted_on_policy_threshold_cross() -> None:
    # Stored 3.74 (above target 3.0), fresh 2.10 (below) → decision flip → refuted, regardless of %.
    verdict, crossed, _ = classify_drift(
        stored_value=3.74, fresh=FreshSample(2.10, 60, 2000, "w"),
        target_roas=3.0, pause_roas_floor=1.5,
    )
    assert verdict == AUDIT_REFUTED and crossed == "target_roas"


def test_classify_drift_contradicted_on_magnitude_without_threshold_cross() -> None:
    # Stored 10.0, fresh 6.0 (40% drift) but BOTH above target → contradicted, not refuted.
    verdict, crossed, _ = classify_drift(
        stored_value=10.0, fresh=FreshSample(6.0, 60, 4000, "w"),
        target_roas=3.0, pause_roas_floor=1.5,
    )
    assert verdict == AUDIT_CONTRADICTED and crossed is None


def test_classify_drift_insufficient_fresh_data_abstains() -> None:
    # A noisy fresh window (2 purchases / $30) is below the significance floor → abstain, NOT a
    # refutation — even though 2.5 < target 3.0 would otherwise flip the decision.
    verdict, crossed, _ = classify_drift(
        stored_value=3.74, fresh=FreshSample(2.5, 2, 30, "w"),
        target_roas=3.0, pause_roas_floor=1.5,
    )
    assert verdict == AUDIT_INSUFFICIENT and crossed is None


def test_classify_drift_could_not_audit_when_fresh_value_unresolved() -> None:
    # Entity vanished / value missing for the window → could_not_audit, never scored as 0 ROAS.
    verdict, _, _ = classify_drift(
        stored_value=3.74, fresh=FreshSample(None, None, None, "w"),
        target_roas=3.0, pause_roas_floor=1.5,
    )
    assert verdict == AUDIT_COULD_NOT


def test_lower_band_emoji_walks_confidence_band_ordering() -> None:
    # The decrement uses confidence.Band ordering (not a local emoji ladder), floored at Low.
    assert lower_band_emoji(BAND_PRESENTATION[Band.high]["emoji"]) == BAND_PRESENTATION[Band.medium]["emoji"]
    assert lower_band_emoji(BAND_PRESENTATION[Band.medium]["emoji"]) == BAND_PRESENTATION[Band.low]["emoji"]
    assert lower_band_emoji(BAND_PRESENTATION[Band.low]["emoji"]) == BAND_PRESENTATION[Band.low]["emoji"]
    assert lower_band_emoji(None) is None


# --- selection -------------------------------------------------------------


def test_select_auditable_skips_evergreen_no_metric_and_other_accounts() -> None:
    text = (
        # evergreen + metric: skipped (platform mechanics don't rot on numbers)
        _entry(
            "- ➕ 2026-01-01 — x. `verify: account_metrics --account divine_designs --level account` "
            "_(src: direct_observation · acct: divine_designs · metric: roas=2.0)_",
            header="evergreen with metric", rot="evergreen", verified="2026-01-01",
        )
        # fast, no metric: skipped (nothing to re-pull)
        + _entry(
            "- ➕ 2026-01-01 — qualitative. _(src: direct_observation · acct: divine_designs)_",
            header="fast no metric", rot="fast", verified="2026-01-01",
        )
        # fast + metric for ANOTHER account: skipped for divine_designs
        + _entry(
            "- ➕ 2026-01-01 — y. `verify: account_metrics --account pollen_sense --level account` "
            "_(src: direct_observation · acct: pollen_sense · metric: roas=2.0)_",
            header="other account", rot="fast", verified="2026-01-01",
        )
        # fast + metric + divine_designs: the ONLY selectable claim
        + _entry(
            "- ➕ 2026-01-01 — z. `verify: account_metrics --account divine_designs --level account` "
            "_(src: direct_observation · acct: divine_designs · metric: blended_roas=3.0)_",
            header="auditable target", rot="fast", verified="2026-01-01",
        )
    )
    pairs = select_auditable(parse_learnings(text), account_slug="divine_designs")
    assert [e.claim for e, _ in pairs] == ["auditable target"]


# --- metric resolution out of fresh rows (resolve_fresh_metric) ------------


def test_resolve_fresh_metric_aggregates_account_level() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    rows = _account_rows(roas=3.21, spend=1000, purchases=40)
    value, purchases, spend = resolve_fresh_metric(rows, level="account", breakdowns=[], metric_name="blended_roas")
    assert value == 3.21 and purchases == 40 and spend == 1000


def test_resolve_fresh_metric_matches_a_breakdown_segment_by_name() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    rows = [
        {"segment": {"publisher_platform": "facebook"}, "spend": 500, "purchase_value": 970, "roas": 1.94, "purchases": 20},
        {"segment": {"publisher_platform": "instagram"}, "spend": 800, "purchase_value": 2232, "roas": 2.79, "purchases": 50},
    ]
    # `ig_roas` → identifier token {instagram} → the instagram row.
    value, _, _ = resolve_fresh_metric(rows, level="account", breakdowns=["publisher_platform"], metric_name="ig_roas")
    assert value == 2.79


def test_resolve_fresh_metric_returns_none_when_segment_is_ambiguous() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    rows = [
        {"segment": {"publisher_platform": "facebook"}, "spend": 500, "purchase_value": 970, "roas": 1.94, "purchases": 20},
        {"segment": {"publisher_platform": "instagram"}, "spend": 800, "purchase_value": 2232, "roas": 2.79, "purchases": 50},
    ]
    # A metric name with no token matching any segment → unresolved (→ could_not_audit, never a guess).
    assert resolve_fresh_metric(rows, level="account", breakdowns=["publisher_platform"], metric_name="tiktok_roas") == (None, None, None)


def test_resolve_fresh_metric_value_missing_is_unresolved_not_zero() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # Spend but no purchase_value for the window → ROAS unresolved, NOT a fabricated 0 ROAS.
    rows = _account_rows(roas=None, spend=300, purchases=0)
    value, _, spend = resolve_fresh_metric(rows, level="account", breakdowns=[], metric_name="blended_roas")
    assert value is None and spend == 300


# --- explicit `select:` slice resolution (resolve_fresh_metric) ------------
# A two-dimension `publisher_platform,platform_position` breakdown: many IG cells share the
# {instagram} name-token, so the token heuristic abstains (ambiguous). An explicit selector resolves
# the slice exactly — blending several cells, or pinning one — and abstains only on zero matches.

def _two_dim_ig_fb_rows():
    """IG (feed/stories/reels) + FB (feed) cells. IG blends to 3.63 ROAS (8160/2250); IG Stories
    alone is 4.50 (2250/500)."""
    return [
        {"segment": {"publisher_platform": "instagram", "platform_position": "feed"},
         "spend": 1000, "purchase_value": 3250, "roas": 3.25, "purchases": 80},
        {"segment": {"publisher_platform": "instagram", "platform_position": "stories"},
         "spend": 500, "purchase_value": 2250, "roas": 4.50, "purchases": 40},
        {"segment": {"publisher_platform": "instagram", "platform_position": "reels"},
         "spend": 750, "purchase_value": 2660, "roas": 3.55, "purchases": 55},
        {"segment": {"publisher_platform": "facebook", "platform_position": "feed"},
         "spend": 900, "purchase_value": 2295, "roas": 2.55, "purchases": 35},
    ]


def test_resolve_fresh_metric_selector_blends_subset_under_finer_breakdown() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # `select: publisher_platform=instagram` under a two-dim breakdown → several IG cells → the
    # author-specified platform-level blend (8160 value / 2250 spend = 3.63), NOT an abstain.
    value, purchases, spend = resolve_fresh_metric(
        _two_dim_ig_fb_rows(), level="account", breakdowns=["publisher_platform", "platform_position"],
        metric_name="ig_roas", selector={"publisher_platform": "instagram"},
    )
    assert value == 3.63 and spend == 2250 and purchases == 175


def test_resolve_fresh_metric_selector_pins_a_single_cell() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # A two-key selector names exactly one cell → that row via _row_value (single-cell ROAS 4.50).
    value, _, spend = resolve_fresh_metric(
        _two_dim_ig_fb_rows(), level="account", breakdowns=["publisher_platform", "platform_position"],
        metric_name="ig_stories_roas",
        selector={"publisher_platform": "instagram", "platform_position": "stories"},
    )
    assert value == 4.50 and spend == 500


def test_resolve_fresh_metric_selector_zero_match_abstains() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # A vanished/renamed segment value → zero matches → (None, None, None) → could_not_audit.
    assert resolve_fresh_metric(
        _two_dim_ig_fb_rows(), level="account", breakdowns=["publisher_platform", "platform_position"],
        metric_name="threads_roas", selector={"publisher_platform": "threads"},
    ) == (None, None, None)


def test_resolve_fresh_metric_selector_is_case_insensitive_full_value_only() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    rows = [
        {"segment": {"publisher_platform": "instagram"}, "spend": 800, "purchase_value": 2232, "roas": 2.79, "purchases": 50},
        {"segment": {"publisher_platform": "facebook"}, "spend": 500, "purchase_value": 970, "roas": 1.94, "purchases": 20},
    ]
    # Case-insensitive full-value match: `Instagram` selector resolves the `instagram` row.
    value, _, _ = resolve_fresh_metric(
        rows, level="account", breakdowns=["publisher_platform"],
        metric_name="ig_roas", selector={"publisher_platform": "Instagram"},
    )
    assert value == 2.79
    # …but a substring must NOT match (full value only, unlike the token path) → abstain.
    assert resolve_fresh_metric(
        rows, level="account", breakdowns=["publisher_platform"],
        metric_name="ig_roas", selector={"publisher_platform": "insta"},
    ) == (None, None, None)


def test_resolve_fresh_metric_selector_missing_key_does_not_match() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # A selector key absent from the row's segment dict → no match (no crash, no partial match).
    rows = [{"segment": {"publisher_platform": "instagram"}, "spend": 800, "purchase_value": 2232,
             "roas": 2.79, "purchases": 50}]
    assert resolve_fresh_metric(
        rows, level="account", breakdowns=["publisher_platform"],
        metric_name="ig_roas", selector={"platform_position": "stories"},
    ) == (None, None, None)


def test_resolve_fresh_metric_none_selector_uses_token_heuristic_unchanged() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    rows = [
        {"segment": {"publisher_platform": "facebook"}, "spend": 500, "purchase_value": 970, "roas": 1.94, "purchases": 20},
        {"segment": {"publisher_platform": "instagram"}, "spend": 800, "purchase_value": 2232, "roas": 2.79, "purchases": 50},
    ]
    # selector=None (the default) falls through to the name-token path: ig_roas → {instagram} → 2.79.
    assert resolve_fresh_metric(
        rows, level="account", breakdowns=["publisher_platform"], metric_name="ig_roas",
    )[0] == 2.79


def test_resolve_fresh_metric_account_level_rows_have_no_segment_so_selector_abstains() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # Account-level rows carry no `segment` dict → _row_matches_selector is False for all → abstain.
    assert resolve_fresh_metric(
        _account_rows(roas=3.21), level="account", breakdowns=[], metric_name="blended_roas",
        selector={"publisher_platform": "instagram"},
    ) == (None, None, None)


# --- end-to-end orchestration with a fake metrics provider -----------------


def test_audit_confirmed_refreshes_verified_only_band_unchanged() -> None:
    report, new_text, counts = _audit(_AUDIT_VAULT, _fixed_fetch(_account_rows(roas=3.70)), apply=True)
    assert counts[AUDIT_CONFIRMED] == 1
    assert new_text is not None
    assert "🟢 High" in new_text  # band untouched (re-confirming the same window is not corroboration)
    assert "**Verified:** 2026-06-25" in new_text  # refreshed → clears a lint-vault ⏳ re-verify flag
    assert "➖" not in new_text  # no contradiction logged


def test_audit_refuted_lowers_band_logs_dated_minus_and_keeps_claim() -> None:
    report, new_text, counts = _audit(_AUDIT_VAULT, _fixed_fetch(_account_rows(roas=2.10)), apply=True)
    assert counts[AUDIT_REFUTED] == 1
    assert new_text is not None
    # Refute → 🔴 Low + (contested); never edits the claim text; never deletes the entry.
    assert "🔴 Low (contested)" in new_text
    assert "Divine Designs blended ROAS sits comfortably above target" in new_text
    # A dated ➖ carrying the fresh metric and a reproduce-the-fresh-value verify command.
    assert "➖ 2026-06-25 — vault audit: blended_roas now 2.10 vs stored 3.74" in new_text
    assert "verify: account_metrics --account divine_designs --level account" in new_text
    assert "metric: blended_roas=2.10" in new_text
    assert "**Verified:** 2026-06-25" in new_text
    # The contradiction is called out loudly in the always-printed report.
    assert "⚠️" in report and "CONTRADICTION" in report


def test_audit_insufficient_fresh_data_changes_nothing() -> None:
    # A below-floor fresh pull must not refute a real fact: no band change, no ➖, Verified unmoved.
    report, new_text, counts = _audit(
        _AUDIT_VAULT, _fixed_fetch(_account_rows(roas=2.5, spend=30, purchases=2)), apply=True
    )
    assert counts[AUDIT_INSUFFICIENT] == 1
    assert new_text == _AUDIT_VAULT


def test_audit_could_not_audit_when_entity_vanished() -> None:
    # Empty fresh rows → could_not_audit; reported, never silently counted as confirmed, no edits.
    report, new_text, counts = _audit(_AUDIT_VAULT, _fixed_fetch([]), apply=True)
    assert counts[AUDIT_COULD_NOT] == 1
    assert new_text == _AUDIT_VAULT


def test_audit_apply_is_idempotent_on_same_as_of() -> None:
    fetch = _fixed_fetch(_account_rows(roas=2.10))
    _, t1, _ = _audit(_AUDIT_VAULT, fetch, apply=True)
    _, t2, _ = _audit(t1, fetch, apply=True)
    assert t2 == t1  # second --apply on the same --as-of is a no-op
    assert t1.count("➖ 2026-06-25") == 1  # exactly one drift line, not two


def test_audit_report_only_makes_no_text_changes() -> None:
    report, new_text, counts = _audit(_AUDIT_VAULT, _fixed_fetch(_account_rows(roas=2.10)), apply=False)
    assert new_text is None  # report-only ⇒ nothing to write
    assert counts[AUDIT_REFUTED] == 1  # …but drift is still detected and reported


# A two-dimension `publisher_platform,platform_position` claim with an explicit `select:` slice —
# mirrors the real divine_designs `ig_roas=3.63` entry. The token heuristic alone would abstain
# (every IG cell matches {instagram}); the selector blends the IG cells so the claim re-verifies.
_AUDIT_SELECT_VAULT = """# Durable learnings

## Strategy

### Instagram outperforms Facebook across placements
**Confidence:** 🟢 High →  ·  **Domain:** strategy
**Rot:** fast  ·  **Verified:** 2026-01-10
- ➕ 2026-01-10 — 120d split. `verify: account_metrics --account divine_designs --level account --date-from 2025-09-12 --date-to 2026-01-10 --breakdown publisher_platform,platform_position` _(src: correlational · acct: divine_designs · metric: ig_roas=3.63 · select: publisher_platform=instagram)_
**Apply:** lean Instagram.
"""


def _two_dim_blend_rows(*, ig_roas: float):
    """Two IG cells (feed+stories) + one FB cell. The IG cells blend to ``ig_roas`` so the selector
    `publisher_platform=instagram` resolves to it; FB is a decoy the selector must exclude."""
    half = round(ig_roas * 1000, 2)
    return [
        {"segment": {"publisher_platform": "instagram", "platform_position": "feed"},
         "spend": 1000, "purchase_value": half, "roas": ig_roas, "purchases": 80},
        {"segment": {"publisher_platform": "instagram", "platform_position": "stories"},
         "spend": 1000, "purchase_value": half, "roas": ig_roas, "purchases": 80},
        {"segment": {"publisher_platform": "facebook", "platform_position": "feed"},
         "spend": 1000, "purchase_value": 2000, "roas": 2.0, "purchases": 40},
    ]


def test_audit_selector_resolves_two_dim_claim_end_to_end_and_is_idempotent() -> None:
    # Fresh IG blend 5.00 drifts 38% from stored 3.63 (both above target 3.0 → no policy cross →
    # contradicted, not refuted). The selector blends only the IG cells; without it the audit would
    # abstain (could_not_audit). Band drops one level 🟢→🟡, a dated ➖ is logged.
    fetch = _fixed_fetch(_two_dim_blend_rows(ig_roas=5.00))
    _, t1, counts = _audit(_AUDIT_SELECT_VAULT, fetch, apply=True)
    assert counts[AUDIT_CONTRADICTED] == 1 and counts[AUDIT_COULD_NOT] == 0
    assert "🟡 Medium" in t1 and "(contested)" not in t1
    assert "➖ 2026-06-25 — vault audit: ig_roas now 5.00 vs stored 3.63" in t1
    assert "**Verified:** 2026-06-25" in t1
    # The logged ➖ carries metric: but no select: — and is_audit_line skips it, so a second --apply
    # on the same --as-of stays byte-identical even for a selector-resolved claim.
    _, t2, _ = _audit(t1, fetch, apply=True)
    assert t2 == t1


def test_audit_selector_abstains_when_segment_vanished_band_unchanged() -> None:
    # The IG cells are gone from the fresh pull (only FB remains) → selector matches zero rows →
    # could_not_audit. The safe-direction invariant: band untouched, no ➖, Verified unmoved.
    fetch = _fixed_fetch([
        {"segment": {"publisher_platform": "facebook", "platform_position": "feed"},
         "spend": 1000, "purchase_value": 2000, "roas": 2.0, "purchases": 40},
    ])
    _, new_text, counts = _audit(_AUDIT_SELECT_VAULT, fetch, apply=True)
    assert counts[AUDIT_COULD_NOT] == 1
    assert new_text == _AUDIT_SELECT_VAULT  # zero file changes — a vanished segment must not refute


# --- CLI: file I/O path (report-only never writes; --apply writes) ---------


def _run_audit_cli(tmp_path, monkeypatch, text, rows, *, apply: bool, as_of="2026-06-25"):
    from meta_ads_analysis import cli
    import meta_ads_analysis.meta_api as meta_api

    learnings = tmp_path / "learnings.md"
    learnings.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli, "resolve_ad_account_id", lambda slug: "act_test")
    monkeypatch.setattr(cli, "fetch_entity_metrics", lambda *a, **k: rows)
    monkeypatch.setattr(cli, "fetch_breakdown_metrics", lambda *a, **k: rows)
    monkeypatch.setattr(meta_api, "client_from_env", lambda version=None: Mock())
    argv = ["audit-vault", "--account", "divine_designs", "--as-of", as_of, "--path", str(learnings)]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    cli.audit_vault_main()
    return learnings.read_text(encoding="utf-8")


def test_audit_vault_cli_report_only_leaves_file_byte_for_byte(tmp_path, monkeypatch, capsys) -> None:
    out = _run_audit_cli(tmp_path, monkeypatch, _AUDIT_VAULT, _account_rows(roas=2.10), apply=False)
    assert out == _AUDIT_VAULT  # zero file changes in report-only mode
    printed = capsys.readouterr().out
    assert "CONTRADICTION" in printed  # contradiction surfaced loudly even without --apply


def test_audit_vault_cli_apply_writes_drift_to_file(tmp_path, monkeypatch) -> None:
    out = _run_audit_cli(tmp_path, monkeypatch, _AUDIT_VAULT, _account_rows(roas=2.10), apply=True)
    assert "🔴 Low (contested)" in out
    assert "➖ 2026-06-25 — vault audit: blended_roas now 2.10 vs stored 3.74" in out
    assert "**Verified:** 2026-06-25" in out


# --- review additions: gaps the implementer's tests left uncovered ---------


def test_audit_contradicted_lowers_band_one_level_in_text() -> None:
    # Magnitude drift (3.74 → 5.00 ≈ 34%) with NO policy-threshold cross (both above target 3.0):
    # contradicted, NOT refuted. On --apply the band drops exactly one level (🟢 High → 🟡 Medium),
    # is NOT marked (contested) (that is refute-only), and a dated ➖ is logged.
    report, new_text, counts = _audit(_AUDIT_VAULT, _fixed_fetch(_account_rows(roas=5.00)), apply=True)
    assert counts[AUDIT_CONTRADICTED] == 1 and counts[AUDIT_REFUTED] == 0
    assert new_text is not None
    assert "🟡 Medium" in new_text and "🟢 High" not in new_text
    assert "(contested)" not in new_text  # one-level drop, not a refute
    assert "➖ 2026-06-25 — vault audit: blended_roas now 5.00 vs stored 3.74" in new_text
    assert "**Verified:** 2026-06-25" in new_text


def test_resolve_fresh_metric_matches_entity_name_at_adset_level() -> None:
    from meta_ads_analysis.cli import resolve_fresh_metric

    # `engaged_adset_roas` → identifier token {engaged} → the one ad set whose NAME contains it.
    rows = [
        {"id": "1", "name": "Engaged - 365d", "spend": 1200, "purchase_value": 4488, "roas": 3.74, "purchases": 90},
        {"id": "2", "name": "Broad prospecting", "spend": 900, "purchase_value": 1800, "roas": 2.0, "purchases": 30},
    ]
    value, _, spend = resolve_fresh_metric(rows, level="adset", breakdowns=[], metric_name="engaged_adset_roas")
    assert value == 3.74 and spend == 1200


# Two data-backed metric: lines in ONE entry (mirrors the real divine_designs Instagram entry,
# which carries two `ig_roas` claims). Both must be audited, both ➖ logged, and a re-run must stay
# byte-identical even though the two share the same metric NAME.
_AUDIT_TWO_METRICS = """# Durable learnings

## Strategy

### Two windows back the same Instagram-wins claim
**Confidence:** 🟢 High →  ·  **Domain:** strategy
**Rot:** fast  ·  **Verified:** 2026-01-10
- ➕ 2026-01-10 — 30d split. `verify: account_metrics --account divine_designs --level account --date-from 2025-12-12 --date-to 2026-01-10 --breakdown publisher_platform` _(src: correlational · acct: divine_designs · metric: ig_roas=2.79)_
- ➕ 2026-01-10 — 30d split, different cut. `verify: account_metrics --account divine_designs --level account --date-from 2025-12-12 --date-to 2026-01-10 --breakdown publisher_platform` _(src: correlational · acct: divine_designs · metric: fb_roas=1.94)_
**Apply:** lean Instagram.
"""


def test_audit_logs_each_drifted_metric_in_a_multi_metric_entry_and_is_idempotent() -> None:
    # ig_roas → instagram segment 1.50 (drifts from 2.79, AND crosses pause_roas_floor 1.5? no — 1.5
    # is not < 1.5; use a clear refute). fb_roas → facebook segment 1.00 (drifts from 1.94, crosses
    # pause_roas_floor 1.5 → refuted). Both segments resolvable in one breakdown pull.
    rows = [
        {"segment": {"publisher_platform": "instagram"}, "spend": 1000, "purchase_value": 1200, "roas": 1.20, "purchases": 50},
        {"segment": {"publisher_platform": "facebook"}, "spend": 1000, "purchase_value": 1000, "roas": 1.00, "purchases": 40},
    ]
    _, t1, counts = _audit(_AUDIT_TWO_METRICS, _fixed_fetch(rows), apply=True)
    # Both metrics drifted; each gets its own dated ➖ even though they live in one entry.
    assert t1.count("➖ 2026-06-25 — vault audit:") == 2
    assert "vault audit: ig_roas now 1.20" in t1
    assert "vault audit: fb_roas now 1.00" in t1
    # A second --apply on the same --as-of adds nothing (idempotent across both metric names).
    _, t2, _ = _audit(t1, _fixed_fetch(rows), apply=True)
    assert t2 == t1


# --- Reader provider seam (MOCKS ONLY: no test here makes a live Meta call) ---

import inspect

from meta_ads_analysis.reader_provider import (
    READ_METHODS,
    DirectMetaReader,
    FakeMetaReader,
    MetaReaderProvider,
    as_reader,
)

# Representative (args, kwargs) per read method — also pins the call shape each one takes.
_READER_CALL_SPECS = {
    "fetch_insights": (("act_1",), {"fields": ["spend"], "date_from": "2026-06-01", "date_to": "2026-06-30"}),
    "fetch_ads": (("act_1",), {"fields": ["id"]}),
    "list_campaigns": (("act_1",), {"fields": ["id"]}),
    "get_campaign": (("c1",), {"fields": ["id"]}),
    "list_adsets": (("act_1",), {"fields": ["id"]}),
    "get_adset": (("as1",), {"fields": ["id"]}),
    "get_ad": (("ad1",), {"fields": ["id"]}),
    "list_custom_audiences": (("act_1",), {"fields": ["id"]}),
    "get_account": (("act_1",), {"fields": ["name"]}),
    "get_delivery_estimate": (("as1",), {"fields": ["estimate_dau"]}),
    "search_targeting": ((), {"query": "jewelry"}),
    "list_pixels": (("act_1",), {"fields": ["id"]}),
    "list_custom_conversions": (("act_1",), {"fields": ["id"]}),
    "iter_paginated": (("/act_1/ads",), {"params": {"limit": 1}}),
}


class _RecordingClient:
    """Records every call and returns a per-method sentinel — to prove DirectMetaReader delegates 1:1.

    MOCKS ONLY: stands in for MetaMarketingApiClient; never touches the network.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"<{name}>"

        return _method


def _sig_params(sig: inspect.Signature) -> list[tuple]:
    return [(p.name, p.kind, p.default) for p in sig.parameters.values()]


def test_reader_call_specs_cover_every_read_method() -> None:
    # Guard: the delegation test is only meaningful if it exercises the full read surface.
    assert set(_READER_CALL_SPECS) == set(READ_METHODS)


def test_direct_meta_reader_delegates_each_read_method_one_to_one() -> None:
    # Every reader method forwards to the same-named client method and returns its result verbatim.
    for name, (args, kwargs) in _READER_CALL_SPECS.items():
        recorder = _RecordingClient()
        reader = DirectMetaReader(recorder)
        result = getattr(reader, name)(*args, **kwargs)
        assert [c[0] for c in recorder.calls] == [name], f"{name} did not delegate 1:1"
        assert result == f"<{name}>", f"{name} did not return the wrapped client's result"


def test_reader_signatures_match_client_exactly() -> None:
    # Keyword-only splits and defaults must match MetaMarketingApiClient so a call-site swap is a
    # pure rename; drift here surfaces as a TypeError only at some distant call site otherwise.
    for name in READ_METHODS:
        client_params = _sig_params(inspect.signature(getattr(MetaMarketingApiClient, name)))
        for cls in (MetaReaderProvider, DirectMetaReader, FakeMetaReader):
            reader_params = _sig_params(inspect.signature(getattr(cls, name)))
            assert reader_params == client_params, (
                f"{cls.__name__}.{name} signature drifted from MetaMarketingApiClient"
            )


def test_direct_meta_reader_iter_paginated_preserves_lazy_iterator() -> None:
    # iter_paginated must return an iterator (not a list), preserving the client's laziness.
    def _gen(path, *, params=None):
        yield {"id": "1"}
        yield {"id": "2"}

    class _Client:
        iter_paginated = staticmethod(_gen)

    reader = DirectMetaReader(_Client())
    out = reader.iter_paginated("/act_1/ads", params={"limit": 1})
    assert iter(out) is out  # a real iterator, returned unchanged
    assert list(out) == [{"id": "1"}, {"id": "2"}]


def test_fake_meta_reader_returns_canned_values_and_records_calls() -> None:
    reader = FakeMetaReader(
        get_account={"name": "Acme"},
        list_campaigns=[{"id": "c1"}],
        get_ad=lambda ad_id, *, fields: {"id": ad_id, "fields": fields},
    )
    assert reader.get_account("act_1", fields=["name"]) == {"name": "Acme"}
    assert reader.list_campaigns("act_1", fields=["id"]) == [{"id": "c1"}]
    # A callable stub receives the actual call args.
    assert reader.get_ad("ad9", fields=["id"]) == {"id": "ad9", "fields": ["id"]}
    assert ("get_account", ("act_1",), {"fields": ["name"]}) in reader.calls


def test_fake_meta_reader_raises_on_unstubbed_method() -> None:
    reader = FakeMetaReader(get_account={"name": "Acme"})
    try:
        reader.get_adset("as1", fields=["id"])
    except NotImplementedError as exc:
        assert "get_adset" in str(exc)
    else:
        raise AssertionError("expected NotImplementedError for an unstubbed read method")


def test_fake_meta_reader_iter_paginated_is_reiterable_per_call() -> None:
    reader = FakeMetaReader(iter_paginated=[{"id": "1"}, {"id": "2"}])
    # Each call yields the full seeded list, so list()/iterate-twice behave like the real client.
    assert list(reader.iter_paginated("/act_1/ads")) == [{"id": "1"}, {"id": "2"}]
    assert list(reader.iter_paginated("/act_1/ads")) == [{"id": "1"}, {"id": "2"}]


def test_fake_meta_reader_rejects_unknown_stub_name() -> None:
    try:
        FakeMetaReader(get_widgets=[])
    except ValueError as exc:
        assert "get_widgets" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown read method name")


def test_as_reader_wraps_client_and_passes_reader_through() -> None:
    fake = FakeMetaReader(get_account={"name": "X"})
    assert as_reader(fake) is fake  # already a provider -> returned unchanged
    assert as_reader(None) is None  # None passes through for lazy-default callers
    wrapped = as_reader(_RecordingClient())
    assert isinstance(wrapped, DirectMetaReader)
    assert wrapped.get_account("act_1", fields=["name"]) == "<get_account>"


def test_supplied_reader_short_circuits_from_env(monkeypatch) -> None:
    # A supplied reader must never trigger DirectMetaReader.from_env()'s env/token lookup (laziness).
    def _boom(*_a, **_k):
        raise AssertionError("client_from_env must not be called when a reader is supplied")

    monkeypatch.setattr("meta_ads_analysis.reader_provider.client_from_env", _boom)
    plan = {
        "account_slug": "x",
        "run_date": "2026-06-16",
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"type": "ad", "id": "1"},
                "params": {"status": "paused"},
                "rationale": "r",
            }
        ],
    }
    reader = FakeMetaReader(
        get_ad={"id": "1", "name": "Ad", "status": "PAUSED", "effective_status": "PAUSED"}
    )
    enriched = enrich_action_plan_with_live_state(plan, reader=reader)
    assert enriched["actions"][0]["live_state"]["status"] == "PAUSED"


def test_build_account_snapshot_accepts_a_fake_reader() -> None:
    # The control read entry point works with a pure FakeMetaReader (no client wrapping needed).
    reader = FakeMetaReader(
        list_campaigns=[{"id": "c1", "name": "C", "status": "ACTIVE", "effective_status": "ACTIVE"}],
        list_adsets=[
            {
                "id": "as1", "name": "S", "status": "ACTIVE", "effective_status": "ACTIVE",
                "campaign_id": "c1", "targeting": {"custom_audiences": [{"id": "A", "name": "aud-A"}]},
            }
        ],
        iter_paginated=[
            {"id": "ad1", "name": "Ad", "status": "ACTIVE", "effective_status": "ACTIVE",
             "adset_id": "as1", "issues_info": []}
        ],
    )
    from meta_ads_analysis.control import build_account_snapshot as _snap

    snap = _snap(reader, "act_1")
    assert snap["rollup"]["campaigns"] == 1
    assert snap["campaigns"][0]["adsets"][0]["included_audiences"] == ["aud-A"]


class _WriteOnlyRecordingClient:
    """A write-only client: records update_* calls and has NO read methods.

    Used to prove a mixed read+write apply routes the live re-read through the supplied
    ``reader`` (not the write client) — if the read leaked to the client, it would raise
    AttributeError here. MOCKS ONLY.
    """

    def __init__(self) -> None:
        self.updates: list[tuple] = []

    def update_adset(self, node_id, *, params, validate_only=False):
        self.updates.append(("adset", node_id, params, validate_only))
        return {"id": node_id, "success": True}


def test_apply_ops_plan_routes_read_through_reader_and_write_through_client() -> None:
    # The hybrid path: a distinct reader supplies the live re-read; the concrete client does the
    # write. The write client deliberately has no get_adset, so a leaked read would AttributeError.
    from meta_ads_analysis.control import apply_ops_plan as _apply

    reader = FakeMetaReader(get_adset={"id": "as1", "daily_budget": "10000"})
    client = _WriteOnlyRecordingClient()
    plan = {
        "ops": [
            {"op_id": "bump", "op": "set_daily_budget", "level": "adset", "id": "as1",
             "params": {"daily_budget_cents": 11000, "max_increase_percent": 20}, "status": "approved"},
        ]
    }

    results = _apply(plan, client, execute=True, reader=reader)

    assert results[0].status == "executed"
    # Read hit the reader...
    assert [c[0] for c in reader.calls] == ["get_adset"]
    # ...and the write hit the concrete client.
    assert client.updates == [("adset", "as1", {"daily_budget": "11000"}, False)]


# --- MCP read backend (MOCKS ONLY: the tool-executor is fake; no live MCP / Meta call) ---

from meta_ads_analysis.reader_provider import (  # noqa: E402
    MCPMetaReader,
    reader_from_env,
)


class _RecordingExecutor:
    """A fake MCP tool-executor: records ``(tool, arguments)`` and returns a canned raw result.

    ``returns`` maps tool-name -> the value the MCP tool would emit (a dict / list / Graph-style
    envelope / JSON string). A callable value receives the arguments dict. An unexpected tool call
    raises, so a test proves which tools were (and were not) invoked. MOCKS ONLY.
    """

    def __init__(self, returns: dict | None = None) -> None:
        self.returns = returns or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool: str, arguments: dict):
        self.calls.append((tool, arguments))
        if tool not in self.returns:
            raise AssertionError(f"unexpected MCP tool call: {tool}")
        value = self.returns[tool]
        return value(arguments) if callable(value) else value


def test_mcp_reader_signatures_match_client_exactly() -> None:
    # MCPMetaReader must be a drop-in for the same seam: its read signatures match the client's, so
    # a backend swap is invisible to every call site.
    for name in READ_METHODS:
        client_params = _sig_params(inspect.signature(getattr(MetaMarketingApiClient, name)))
        reader_params = _sig_params(inspect.signature(getattr(MCPMetaReader, name)))
        assert reader_params == client_params, f"MCPMetaReader.{name} signature drifted"


def test_mcp_reader_translates_fields_list_to_comma_string_without_dropping_any() -> None:
    # Field-list translation is the high-risk edge: a dropped field silently blanks a metric.
    execu = _RecordingExecutor({"meta_ads_get_ads_by_adaccount": {"data": []}})
    MCPMetaReader(execu).fetch_ads("act_1", fields=["ad_id", "ad_name", "spend", "impressions"])
    tool, args = execu.calls[0]
    assert tool == "meta_ads_get_ads_by_adaccount"
    assert args["act_id"] == "act_1"
    assert args["fields"] == "ad_id,ad_name,spend,impressions"  # joined to a comma string
    # Round-trips with nothing dropped — the exact guarantee the metrics pipeline depends on.
    assert args["fields"].split(",") == ["ad_id", "ad_name", "spend", "impressions"]


def test_mcp_reader_insights_translates_window_and_breakdowns() -> None:
    execu = _RecordingExecutor({"meta_ads_get_adaccount_insights": {"data": []}})
    MCPMetaReader(execu).fetch_insights(
        "act_9",
        fields=["spend", "actions"],
        date_from="2026-06-01",
        date_to="2026-06-30",
        level="ad",
        time_increment=1,
        breakdowns=["age", "gender"],
    )
    tool, args = execu.calls[0]
    assert tool == "meta_ads_get_adaccount_insights"
    assert args["fields"] == "spend,actions"
    assert args["time_range"] == {"since": "2026-06-01", "until": "2026-06-30"}
    assert args["level"] == "ad"
    assert args["breakdowns"] == ["age", "gender"]


def test_mcp_reader_list_result_shape_matches_direct_reader() -> None:
    # Result-shape parity: both backends return identical list[dict] for a list read, so every
    # downstream parser is backend-agnostic.
    canned = [
        {"id": "400", "name": "API Ad", "status": "ACTIVE"},
        {"id": "401", "name": "API Ad 2", "status": "PAUSED"},
    ]

    class _Client:
        def fetch_ads(self, ad_account_id, *, fields):
            return canned

    direct = DirectMetaReader(_Client())
    mcp = MCPMetaReader(_RecordingExecutor({"meta_ads_get_ads_by_adaccount": {"data": canned}}))
    assert mcp.fetch_ads("act_1", fields=["id"]) == direct.fetch_ads("act_1", fields=["id"]) == canned


def test_mcp_reader_node_result_shape_matches_direct_reader() -> None:
    canned = {"id": "act_1", "name": "Acme", "currency": "USD"}

    class _Client:
        def get_account(self, ad_account_id, *, fields):
            return canned

    direct = DirectMetaReader(_Client())
    mcp = MCPMetaReader(_RecordingExecutor({"meta_ads_get_ad_account_details": canned}))
    assert (
        mcp.get_account("act_1", fields=["name"])
        == direct.get_account("act_1", fields=["name"])
        == canned
    )


def test_mcp_reader_accepts_bare_list_and_json_string_results() -> None:
    # Robust to two common community-server shapes: a bare list, and tool output returned as text.
    bare = MCPMetaReader(_RecordingExecutor({"meta_ads_get_campaigns_by_adaccount": [{"id": "c1"}]}))
    assert bare.list_campaigns("act_1", fields=["id"]) == [{"id": "c1"}]
    as_text = MCPMetaReader(
        _RecordingExecutor({"meta_ads_get_ad_account_details": json.dumps({"id": "act_1"})})
    )
    assert as_text.get_account("act_1", fields=["id"]) == {"id": "act_1"}


def test_mcp_reader_drains_pagination_so_no_page_is_dropped() -> None:
    # The candidate server does not auto-paginate; the wrapper follows paging.next, never truncates.
    returns = {
        "meta_ads_get_adsets_by_adaccount": {"data": [{"id": "as1"}], "paging": {"next": "URL2"}},
        "meta_ads_fetch_pagination_url": {"data": [{"id": "as2"}], "paging": {}},
    }
    reader = MCPMetaReader(_RecordingExecutor(returns))
    assert reader.list_adsets("act_1", fields=["id"]) == [{"id": "as1"}, {"id": "as2"}]


def test_mcp_reader_refuses_to_truncate_when_pagination_tool_disabled() -> None:
    # Decision: rather than silently truncate a paged result, raise when no pagination tool exists.
    returns = {
        "meta_ads_get_adsets_by_adaccount": {"data": [{"id": "as1"}], "paging": {"next": "URL2"}}
    }
    reader = MCPMetaReader(_RecordingExecutor(returns), pagination_tool=None)
    try:
        reader.list_adsets("act_1", fields=["id"])
    except MetaApiError as exc:
        assert "truncate" in str(exc)
    else:
        raise AssertionError("expected a refusal to silently truncate a paged result")


def test_mcp_reader_unsupported_reads_raise_naming_the_method() -> None:
    # Partial coverage: reads the candidate server does not expose must raise NotImplementedError
    # naming the read, so a caller can fall back to META_READER_BACKEND=direct for that one read.
    execu = _RecordingExecutor()
    reader = MCPMetaReader(execu)
    cases = {
        "get_delivery_estimate": lambda: reader.get_delivery_estimate("as1", fields=["estimate_dau"]),
        "search_targeting": lambda: reader.search_targeting(query="jewelry"),
        "list_pixels": lambda: reader.list_pixels("act_1", fields=["id"]),
        "list_custom_conversions": lambda: reader.list_custom_conversions("act_1", fields=["id"]),
        "list_custom_audiences": lambda: reader.list_custom_audiences("act_1", fields=["id"]),
        "iter_paginated": lambda: reader.iter_paginated("/act_1/ads"),
    }
    for name, call in cases.items():
        try:
            call()
        except NotImplementedError as exc:
            assert name in str(exc), f"NotImplementedError should name the read {name!r}"
        else:
            raise AssertionError(f"{name}: expected NotImplementedError for an unexposed MCP read")
    # _tool_for raised before the executor was ever invoked for any unsupported read.
    assert execu.calls == []


def test_reader_from_env_defaults_to_direct_when_unset(monkeypatch) -> None:
    # Default-off guarantee: unset backend == DirectMetaReader (today's behavior, byte-for-byte).
    monkeypatch.delenv("META_READER_BACKEND", raising=False)
    sentinel = object()
    monkeypatch.setattr("meta_ads_analysis.reader_provider.client_from_env", lambda *a, **k: sentinel)
    reader = reader_from_env()
    assert isinstance(reader, DirectMetaReader)
    assert reader._client is sentinel  # wraps the env client; no MCP involved


def test_reader_from_env_explicit_direct(monkeypatch) -> None:
    monkeypatch.setenv("META_READER_BACKEND", "direct")
    monkeypatch.setattr("meta_ads_analysis.reader_provider.client_from_env", lambda *a, **k: object())
    assert isinstance(reader_from_env(), DirectMetaReader)


def test_reader_from_env_mcp_requires_a_tool_executor(monkeypatch) -> None:
    monkeypatch.setenv("META_READER_BACKEND", "mcp")
    try:
        reader_from_env()
    except RuntimeError as exc:
        assert "tool-executor" in str(exc)
    else:
        raise AssertionError("mcp backend without an injected executor must raise")


def test_reader_from_env_mcp_with_executor_builds_mcp_reader(monkeypatch) -> None:
    monkeypatch.setenv("META_READER_BACKEND", "mcp")
    assert isinstance(reader_from_env(tool_executor=_RecordingExecutor()), MCPMetaReader)


def test_reader_from_env_rejects_unknown_backend(monkeypatch) -> None:
    monkeypatch.setenv("META_READER_BACKEND", "bogus")
    try:
        reader_from_env()
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("unknown backend must raise ValueError")


def test_entry_point_default_reads_through_direct_when_backend_unset(monkeypatch) -> None:
    # The behavioral guarantee this whole ticket rides on: with META_READER_BACKEND unset and no
    # reader supplied, the writes-adjacent re-read path builds a DirectMetaReader around the env
    # client exactly as before — adding the MCP server cannot change production reads.
    monkeypatch.delenv("META_READER_BACKEND", raising=False)
    seen: dict = {}

    class _Client:
        def get_ad(self, ad_id, *, fields):
            seen["ad_id"] = ad_id
            return {"id": ad_id, "name": "Ad", "status": "PAUSED", "effective_status": "PAUSED"}

    monkeypatch.setattr("meta_ads_analysis.reader_provider.client_from_env", lambda *a, **k: _Client())
    plan = {
        "account_slug": "x",
        "run_date": "2026-06-16",
        "actions": [
            {
                "action_id": "pause_ad_1",
                "action_type": "pause_ad",
                "status": "proposed",
                "executable": True,
                "target": {"type": "ad", "id": "1"},
                "params": {"status": "paused"},
                "rationale": "r",
            }
        ],
    }
    enriched = enrich_action_plan_with_live_state(plan)
    assert seen["ad_id"] == "1"
    assert enriched["actions"][0]["live_state"]["status"] == "PAUSED"


# --- MCP read backend: translation/error branches (review-stage coverage; still MOCKS ONLY) ---


def test_mcp_reader_node_unwraps_single_object_data_envelope() -> None:
    # A node read returning {"data": {...}} must be unwrapped to the inner node, matching the bare
    # shape DirectMetaReader returns. This branch of _call_node was previously untested.
    canned = {"id": "c1", "name": "Campaign", "status": "ACTIVE"}
    reader = MCPMetaReader(_RecordingExecutor({"meta_ads_get_campaign_by_id": {"data": canned}}))
    assert reader.get_campaign("c1", fields=["name"]) == canned


def test_mcp_reader_raises_on_non_json_string_result() -> None:
    # A tool that returns text which is not JSON must surface a clear MetaApiError, not crash.
    reader = MCPMetaReader(_RecordingExecutor({"meta_ads_get_ad_account_details": "not json {"}))
    try:
        reader.get_account("act_1", fields=["id"])
    except MetaApiError as exc:
        assert "non-JSON" in str(exc)
    else:
        raise AssertionError("expected MetaApiError when the MCP tool returns non-JSON text")


def test_mcp_reader_list_read_rejects_unexpected_result_shape() -> None:
    # A scalar (neither a list nor a {"data": [...]} envelope) must raise, naming the read, rather
    # than silently coercing to an empty result.
    reader = MCPMetaReader(_RecordingExecutor({"meta_ads_get_campaigns_by_adaccount": 42}))
    try:
        reader.list_campaigns("act_1", fields=["id"])
    except MetaApiError as exc:
        assert "list_campaigns" in str(exc)
    else:
        raise AssertionError("expected MetaApiError for an unexpected list-read result shape")


def test_mcp_reader_node_read_rejects_non_object_result() -> None:
    reader = MCPMetaReader(_RecordingExecutor({"meta_ads_get_campaign_by_id": [1, 2, 3]}))
    try:
        reader.get_campaign("c1", fields=["id"])
    except MetaApiError as exc:
        assert "get_campaign" in str(exc)
    else:
        raise AssertionError("expected MetaApiError when a node read returns a non-object")


def test_mcp_reader_drains_three_pages_and_passes_each_next_url_to_pagination_tool() -> None:
    # More than two pages, and the pagination tool must receive the exact paging.next URL each hop.
    returns = {
        "meta_ads_get_adsets_by_adaccount": {"data": [{"id": "as1"}], "paging": {"next": "URL2"}},
        "meta_ads_fetch_pagination_url": lambda args: {
            "URL2": {"data": [{"id": "as2"}], "paging": {"next": "URL3"}},
            "URL3": {"data": [{"id": "as3"}], "paging": {}},
        }[args["url"]],
    }
    execu = _RecordingExecutor(returns)
    assert MCPMetaReader(execu).list_adsets("act_1", fields=["id"]) == [
        {"id": "as1"}, {"id": "as2"}, {"id": "as3"}
    ]
    # The pagination tool was handed each cursor in order.
    pagination_urls = [a["url"] for t, a in execu.calls if t == "meta_ads_fetch_pagination_url"]
    assert pagination_urls == ["URL2", "URL3"]


def test_mcp_reader_aborts_runaway_pagination_at_max_pages() -> None:
    # A server that returns a fresh paging.next forever must hit the runaway guard, not loop.
    returns = {
        "meta_ads_get_adsets_by_adaccount": {"data": [{"id": "as1"}], "paging": {"next": "URL"}},
        "meta_ads_fetch_pagination_url": {"data": [{"id": "asN"}], "paging": {"next": "URL"}},
    }
    reader = MCPMetaReader(_RecordingExecutor(returns))
    reader.MAX_PAGES = 3  # instance override keeps the test cheap
    try:
        reader.list_adsets("act_1", fields=["id"])
    except MetaApiError as exc:
        assert "runaway" in str(exc)
    else:
        raise AssertionError("expected the MAX_PAGES runaway guard to fire")


# ---------------------------------------------------------------------------
# Early-life ad triage (early_triage.py) — mocks only; no live Meta.
# ---------------------------------------------------------------------------

_TRIAGE_START = date(2026, 6, 24)
_ROAS_POLICY = {"primary_goal": "roas"}
_INSTALL_POLICY = {
    "primary_goal": "maximize_in_app_subscriptions",
    "secondary_cost_per_app_install_target": 3.0,
}


def _point(d, *, spend=0.0, purchases=0.0, purchase_value=0.0, installs=0.0):
    return AdDailyPoint(
        report_date=d,
        spend=spend,
        results=purchases,
        purchase_count=purchases,
        purchase_value=purchase_value,
        app_installs=installs,
    )


def _hist(ad_id, points, name=None):
    return AdHistory(ad_id=ad_id, ad_name=name or f"ad {ad_id}", points=points)


def _roas_ad(ad_id, *, days, daily_spend=15.0, recover_from=None, start=_TRIAGE_START):
    """A ROAS-goal ad: zero-purchase (struggling) early; if ``recover_from`` (an age index) is set,
    later days book purchases/value that clear the 3.0 ROAS target over the recovery window."""
    points = []
    for i in range(days):
        if recover_from is not None and i >= recover_from:
            points.append(
                _point(start + timedelta(days=i), spend=daily_spend, purchases=5.0,
                       purchase_value=daily_spend * 5)
            )
        else:
            points.append(_point(start + timedelta(days=i), spend=daily_spend))
    return _hist(ad_id, points)


def _install_ad(ad_id, *, days, daily_spend=5.0, recover_from=None, start=_TRIAGE_START):
    """An install-goal ad: zero installs (struggling) early; if ``recover_from`` is set, later days
    book cheap installs that clear the $3.00 cost-per-install target over the recovery window."""
    points = []
    for i in range(days):
        if recover_from is not None and i >= recover_from:
            points.append(_point(start + timedelta(days=i), spend=daily_spend, installs=5.0))
        else:
            points.append(_point(start + timedelta(days=i), spend=daily_spend))
    return _hist(ad_id, points)


# --- classify_own_sample: direct unit coverage of the goal-aware own-window grade -----------------
# (The monitor's _forced_decision_install exercises this through build_watch_report; these pin the
# pure-function branches directly — including the ones the monitor tests don't reach.)

_OWN_INSTALL_POLICY = {"primary_goal": "maximize_in_app_subscriptions",
                       "secondary_cost_per_app_install_target": 3.0}


def _own(**kw):
    base = dict(spend=300.0, purchase_value=None, purchases=None, app_installs=None,
               policy=_OWN_INSTALL_POLICY, roas_floor=1.5, roas_target=3.0, min_spend=100.0)
    base.update(kw)
    return classify_own_sample(**base)


def test_classify_own_sample_install_cheap_installs_keeps() -> None:
    v = _own(app_installs=200.0)  # $300 / 200 = $1.50 <= $3.00 target
    assert v.verdict == OWN_SAMPLE_KEEP
    assert v.kind == "install"
    assert v.metric_name == "cost_per_app_install"
    assert v.metric_value == 1.5
    assert v.target == 3.0
    assert v.results == 200.0


def test_classify_own_sample_install_expensive_installs_pauses() -> None:
    v = _own(app_installs=50.0)  # $300 / 50 = $6.00 > $3.00 target
    assert v.verdict == OWN_SAMPLE_PAUSE
    assert v.metric_value == 6.0
    assert "6.00" in v.reasons[0] and "3.00" in v.reasons[0]


def test_classify_own_sample_install_zero_installs_pauses_with_undefined_metric() -> None:
    # Cleared the spend floor but booked ~0 installs → struggling → pause, and the metric is undefined
    # (no divide-by-zero); the reason names the zero-result case rather than a cost figure.
    v = _own(app_installs=0.0)
    assert v.verdict == OWN_SAMPLE_PAUSE
    assert v.metric_value is None
    assert "installs" in v.reasons[0]


def test_classify_own_sample_below_min_spend_is_insufficient() -> None:
    v = _own(spend=60.0, app_installs=200.0)  # < min_spend 100 → defer to analogs
    assert v.verdict == OWN_SAMPLE_INSUFFICIENT
    assert v.target is None


def test_classify_own_sample_install_no_target_is_insufficient_even_above_floor() -> None:
    # Install goal but NO cost target configured: above the spend floor it still defers to the analog
    # path (which degrades such accounts to keep) rather than guessing a threshold. This monitor-side
    # path is otherwise unexercised — the watch tests only hit the below-floor insufficient branch.
    v = _own(policy={"primary_goal": "maximize_in_app_subscriptions"}, app_installs=200.0)
    assert v.verdict == OWN_SAMPLE_INSUFFICIENT
    assert v.target is None


def test_classify_own_sample_roas_kind_grades_on_roas() -> None:
    # classify_own_sample is public and goal-generic: a ROAS account grades on ROAS, not installs.
    assert goal_kind({"primary_goal": "roas"}) == "roas"
    # ROAS = purchase_value / spend; purchases is the result-count gate (zero purchases is struggling
    # regardless of value), so supply both for a coherent sample.
    keep = _own(policy={"primary_goal": "roas"}, purchase_value=900.0, purchases=10.0)  # ROAS 3.0 >= 1.5
    assert keep.verdict == OWN_SAMPLE_KEEP
    assert keep.kind == "roas"
    assert keep.metric_name == "blended_roas"
    pause = _own(policy={"primary_goal": "roas"}, purchase_value=150.0, purchases=10.0)  # ROAS 0.5 < 1.5
    assert pause.verdict == OWN_SAMPLE_PAUSE


def test_early_triage_keep_watch_when_comparable_new_ads_recovered() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)  # age 2 (day 3)
    triaged = _roas_ad("T", days=3)  # struggling, zero-result, $45 life-to-date
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=3) for i in range(3)]  # 3 recovered
    analogs += [_roas_ad(f"B{i}", days=10) for i in range(2)]  # 2 stayed bad

    v = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "keep_watch"
    assert v.age == 2
    assert v.analog_basis["analogs"] == 5
    assert v.analog_basis["recovered"] == 3
    assert v.analog_basis["matched_ids"] == sorted(v.analog_basis["matched_ids"])  # deterministic
    # Correlational grounding (cross-sectional), so the call can never read High.
    assert v.confidence["grounding_tier"] == "correlational"
    # 5 analogs < EARLY_LIFE_STRONG_ANALOGS (6) -> the cross-sectional data band is `low`. (The source
    # ticket narrative loosely said "medium" for 5; the authoritative knee is strong_analogs=6.)
    assert v.confidence["band"] == "low"
    # Evidence cites the ad's own thin life-to-date window first_seen..as_of.
    assert v.evidence["window"] == f"{_TRIAGE_START.isoformat()}..{as_of.isoformat()}"


def test_early_triage_strong_population_reads_medium() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _roas_ad("T", days=3)
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=3) for i in range(4)]  # 4 recovered
    analogs += [_roas_ad(f"B{i}", days=10) for i in range(2)]  # 2 stayed bad -> 6 total

    v = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "keep_watch"
    assert v.analog_basis["analogs"] == 6
    assert v.confidence["band"] == "medium"  # >= strong_analogs; capped at medium by correlational


def test_early_triage_survivorship_one_of_twenty_is_pause_candidate() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _roas_ad("T", days=3)
    analogs = [_roas_ad("R0", days=10, recover_from=3)]  # 1 lucky recovery
    analogs += [_roas_ad(f"B{i}", days=10) for i in range(19)]  # 19 stayed bad

    v = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    # Survivorship guard works on the population RATE (1/20 = 5%), not "any recovery".
    assert v.verdict == "pause_candidate"
    assert v.analog_basis["analogs"] == 20
    assert v.analog_basis["recovered"] == 1
    assert v.analog_basis["rate"] == 0.05
    assert any("5%" in reason for reason in v.reasons)  # reasons name the rate
    assert Band[v.confidence["band"]] <= Band.medium


def test_early_triage_too_few_analogs_abstains_keep() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _roas_ad("T", days=3)
    analogs = [_roas_ad(f"B{i}", days=10) for i in range(2)]  # only 2 comparable

    v = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "abstain_keep"
    # Uses the existing abstain_confidence: data axis abstains, so the combined verdict abstains.
    assert v.confidence["data_band"] == "abstain"
    assert v.confidence["band"] == "abstain"


def test_early_triage_not_struggling_short_circuits_before_analog_work() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    # Healthy early ad: ROAS well above floor.
    healthy = _hist("OK", [
        _point(_TRIAGE_START + timedelta(days=i), spend=15.0, purchases=5.0, purchase_value=150.0)
        for i in range(3)
    ])
    analogs = [_roas_ad(f"B{i}", days=10) for i in range(10)]

    v = triage_ad(
        ad_id="OK", account_slug="divine_designs", as_of=as_of,
        histories=[healthy] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "not_struggling"
    assert v.analog_basis["analogs"] == 0  # no analog work was done
    assert v.analog_basis["matched_ids"] == []


def test_early_triage_install_goal_grades_on_cost_per_install() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _install_ad("TI", days=3)  # $15, zero installs -> struggling on cost-per-install
    analogs = [_install_ad(f"R{i}", days=10, recover_from=3) for i in range(4)]  # recover to $1/install
    analogs += [_install_ad(f"B{i}", days=10) for i in range(2)]  # stayed bad

    v = triage_ad(
        ad_id="TI", account_slug="pollen_sense", as_of=as_of,
        histories=[triaged] + analogs, policy=_INSTALL_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "keep_watch"
    assert v.analog_basis["analogs"] == 6
    assert v.analog_basis["recovered"] == 4
    assert v.evidence["metric_name"] == "cost_per_app_install"


def test_early_triage_install_goal_without_target_degrades_to_abstain() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _install_ad("TI", days=3)
    analogs = [_install_ad(f"R{i}", days=10, recover_from=3) for i in range(4)]

    v = triage_ad(
        ad_id="TI", account_slug="pollen_sense", as_of=as_of,
        histories=[triaged] + analogs,
        policy={"primary_goal": "maximize_in_app_subscriptions"},  # no install-cost target
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.verdict == "abstain_keep"  # graceful, not a crash
    assert v.confidence["band"] == "abstain"


def test_early_triage_excludes_too_short_to_judge_from_population() -> None:
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _roas_ad("T", days=3)
    long_stayed_bad = [_roas_ad(f"L{i}", days=10) for i in range(3)]  # last_age 9 >= horizon
    short_lived = _roas_ad("SHORT", days=5)  # last_age 4 < horizon (7) -> too short to judge

    only_long = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + long_stayed_bad, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    with_short = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + long_stayed_bad + [short_lived], policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    # The too-short ad does NOT swell the "stayed bad" population: same count, and it is not matched.
    assert only_long.analog_basis["analogs"] == 3
    assert with_short.analog_basis["analogs"] == 3
    assert "SHORT" not in with_short.analog_basis["matched_ids"]
    assert with_short.verdict == "pause_candidate"  # 0/3 recovered


def test_early_triage_age_is_deterministic_from_as_of() -> None:
    triaged = _roas_ad("T", days=3)
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=3) for i in range(4)]

    def run(as_of):
        return triage_ad(
            ad_id="T", account_slug="divine_designs", as_of=as_of,
            histories=[triaged] + analogs, policy=_ROAS_POLICY,
            roas_floor=1.5, roas_target=3.0,
        )

    assert run(_TRIAGE_START).age == 0  # day 1 == age 0
    assert run(_TRIAGE_START + timedelta(days=2)).age == 2
    # Identical inputs -> identical verdict (no clock, no randomness).
    assert run(_TRIAGE_START + timedelta(days=2)) == run(_TRIAGE_START + timedelta(days=2))


def test_early_triage_clock_skew_before_first_seen_clamps_age_zero() -> None:
    triaged = _roas_ad("T", days=3)
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=3) for i in range(4)]
    v = triage_ad(
        ad_id="T", account_slug="divine_designs",
        as_of=_TRIAGE_START - timedelta(days=4),  # as_of predates first_seen
        histories=[triaged] + analogs, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v.age == 0


def test_early_triage_returns_none_when_missing_or_past_early_life() -> None:
    triaged = _roas_ad("T", days=10)
    # Not found.
    assert triage_ad(
        ad_id="NOPE", account_slug="x", as_of=_TRIAGE_START + timedelta(days=2),
        histories=[triaged], policy=_ROAS_POLICY, roas_floor=1.5, roas_target=3.0,
    ) is None
    # Past the early-life window (age 5 > EARLY_LIFE_MAX_AGE).
    assert triage_ad(
        ad_id="T", account_slug="x", as_of=_TRIAGE_START + timedelta(days=5),
        histories=[triaged], policy=_ROAS_POLICY, roas_floor=1.5, roas_target=3.0,
    ) is None


def test_early_triage_result_presence_mismatch_is_not_an_analog() -> None:
    # Magnitude can only be compared like-for-like: an ad WITH conversions and one with none are not
    # comparable in either direction, so neither becomes an analog of the other (engine returns no
    # matches -> abstain_keep). Covers the _is_analog branch the implement handoff flagged as untested.
    as_of = _TRIAGE_START + timedelta(days=2)

    def _with_results(ad_id, days):  # struggling (ROAS 0.67 < floor) but has conversions every day
        return _hist(ad_id, [
            _point(_TRIAGE_START + timedelta(days=i), spend=15.0, purchases=2.0, purchase_value=10.0)
            for i in range(days)
        ])

    # (a) triaged HAS results, every candidate has none.
    triaged_has = _with_results("T", 3)
    zero_result_candidates = [_roas_ad(f"B{i}", days=10) for i in range(5)]
    v_a = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged_has] + zero_result_candidates, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v_a.verdict == "abstain_keep"
    assert v_a.analog_basis["analogs"] == 0
    assert v_a.analog_basis["matched_ids"] == []

    # (b) mirror: triaged has NO results, every candidate does.
    triaged_zero = _roas_ad("T", days=3)
    has_result_candidates = [_with_results(f"R{i}", 10) for i in range(5)]
    v_b = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged_zero] + has_result_candidates, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    assert v_b.verdict == "abstain_keep"
    assert v_b.analog_basis["analogs"] == 0
    assert v_b.analog_basis["matched_ids"] == []


def test_early_triage_ratio_tolerance_band_is_inclusive() -> None:
    # ANALOG_RATIO_TOLERANCE=0.5 -> the comparable band is the CLOSED interval [0.5x, 2.0x] of the
    # triaged ad's cumulative spend (the zero-result day-1 fallback). Confirms the boundary is
    # inclusive: an analog exactly at 2.0x / 0.5x matches; a hair past it does not.
    as_of = _TRIAGE_START + timedelta(days=2)
    triaged = _roas_ad("T", days=3)  # spend 45 through age 2, zero-result -> spend-magnitude match
    candidates = [
        _roas_ad("HIGH_AT", days=10, daily_spend=30.0),    # 90 == 2.0x  -> inclusive match
        _roas_ad("HIGH_OVER", days=10, daily_spend=31.0),  # 93 > 2.0x   -> excluded
        _roas_ad("LOW_AT", days=10, daily_spend=7.5),      # 22.5 == 0.5x -> inclusive match
        _roas_ad("LOW_OVER", days=10, daily_spend=7.0),    # 21 < 0.5x   -> excluded
    ]
    v = triage_ad(
        ad_id="T", account_slug="divine_designs", as_of=as_of,
        histories=[triaged] + candidates, policy=_ROAS_POLICY,
        roas_floor=1.5, roas_target=3.0,
    )
    matched = set(v.analog_basis["matched_ids"])
    assert matched == {"HIGH_AT", "LOW_AT"}


def test_analog_confidence_is_capped_at_medium() -> None:
    strong = analog_confidence(analogs=50, recovered=50, min_analogs=3, strong_analogs=6, factors=[])
    assert strong.grounding_tier == "correlational"
    assert strong.data_band == Band.medium
    assert strong.band == Band.medium  # never High, regardless of analog count
    # Ladder: below strong -> low; below min -> abstain.
    assert analog_confidence(analogs=4, recovered=4, min_analogs=3, strong_analogs=6, factors=[]).band == Band.low
    assert analog_confidence(analogs=2, recovered=0, min_analogs=3, strong_analogs=6, factors=[]).band == Band.abstain


def test_group_histories_parses_dates_and_sorts() -> None:
    rows = [
        {"ad_id": "A", "ad_name": "Ad A", "report_date": "2026-06-26", "spend": 5.0,
         "purchase_count": 0.0, "purchase_value": 0.0, "app_installs": 0.0, "results": 0.0},
        {"ad_id": "A", "ad_name": "Ad A", "report_date": date(2026, 6, 24), "spend": 5.0,
         "purchase_count": 0.0, "purchase_value": 0.0, "app_installs": 0.0, "results": 0.0},
        {"ad_id": "", "ad_name": "no id", "report_date": "2026-06-24", "spend": 1.0},  # dropped
    ]
    histories = group_histories(rows)
    assert [h.ad_id for h in histories] == ["A"]
    history = histories[0]
    assert [p.report_date for p in history.points] == [date(2026, 6, 24), date(2026, 6, 26)]
    assert history.first_seen == date(2026, 6, 24)
    assert history.last_seen == date(2026, 6, 26)


def test_duckdb_history_provider_uses_latest_ingestion_run(tmp_path: Path) -> None:
    db_path = tmp_path / "meta_ads.duckdb"

    def row(run_date, slug, ad_id, ad_name, report_date, spend):
        return {
            "ingestion_run_date": run_date, "account_slug": slug, "report_date": report_date,
            "ad_id": ad_id, "ad_name": ad_name, "spend": spend, "purchase_count": 0.0,
            "purchase_value": 0.0, "app_installs": 0.0, "results": 0.0,
        }

    old_run = "2026-06-01"
    new_run = "2026-06-26"
    old_rows = [row(old_run, "acct", "OLD", "Old Ad", "2026-05-31", 1.0)]
    new_rows = [
        row(new_run, "acct", "A1", "Ad One", "2026-06-24", 10.0),
        row(new_run, "acct", "A1", "Ad One", "2026-06-25", 11.0),
        row(new_run, "acct", "A2", "Ad Two", "2026-06-25", 20.0),
    ]
    other_rows = [row(new_run, "other", "X", "Other Ad", "2026-06-25", 99.0)]

    with connect(db_path) as con:
        replace_run_rows(con, "acct", old_run, old_rows, [])
        replace_run_rows(con, "acct", new_run, new_rows, [])
        replace_run_rows(con, "other", new_run, other_rows, [])

    histories = DuckDBHistoryProvider(db_path).ad_histories("acct")
    # Latest run only (A1, A2) — the older run's "OLD" ad is not included.
    assert sorted(h.ad_id for h in histories) == ["A1", "A2"]
    a1 = next(h for h in histories if h.ad_id == "A1")
    assert [p.report_date for p in a1.points] == [date(2026, 6, 24), date(2026, 6, 25)]
    assert a1.ad_name == "Ad One"


def test_duckdb_history_provider_empty_for_unknown_account(tmp_path: Path) -> None:
    db_path = tmp_path / "meta_ads.duckdb"
    assert DuckDBHistoryProvider(db_path).ad_histories("nobody") == []


# ---------------------------------------------------------------------------
# Early-life triage ↔ watch-scan integration (monitor.build_watch_report + cli
# follow-up application). Mocks only: a fake reader for live metrics/meta, a fake
# HistoryProvider for analog histories, and a tmp followups root. No live Meta,
# no DuckDB, no account writes.
# ---------------------------------------------------------------------------

_WATCH_AS_OF = date(2026, 6, 26)
_ROAS_GOAL_POLICY = {"primary_goal": "roas"}


def _watch_insight(ad_id, *, spend, value=0.0, purchases=0.0, installs=0.0, name=None):
    """One ad's aggregated insight blob in the shape ``fetch_entity_metrics`` parses (ad-level keys,
    string-valued actions/action_values like the live API). ``installs`` emits a ``mobile_app_install``
    action (an ``APP_INSTALL_KEYS`` type) so install-goal accounts can be exercised end-to-end."""
    row = {"ad_id": ad_id, "ad_name": name or f"ad {ad_id}", "spend": str(spend)}
    if value:
        row["action_values"] = [{"action_type": "purchase", "value": str(value)}]
    actions = []
    if purchases:
        actions.append({"action_type": "purchase", "value": str(purchases)})
    if installs:
        actions.append({"action_type": "mobile_app_install", "value": str(installs)})
    row["actions"] = actions
    return row


def _watch_meta(ad_id, *, status="ACTIVE", updated="2026-06-25", adset="as1", name=None):
    return {"id": ad_id, "name": name or f"ad {ad_id}", "effective_status": status,
            "adset_id": adset, "updated_time": f"{updated}T00:00:00+0000"}


class _FakeHistoryProvider:
    def __init__(self, histories):
        self._histories = list(histories)

    def ad_histories(self, account_slug):
        return list(self._histories)


def _run_watch(*, insights, meta, histories, open_followups=None, **kw):
    base = dict(
        account_slug="acct", as_of=_WATCH_AS_OF,
        roas_floor=1.5, roas_target=3.0, min_spend=100.0, grace_days=5,
        policy=_ROAS_GOAL_POLICY, history_provider=_FakeHistoryProvider(histories),
        open_followups=open_followups or [],
    )
    base.update(kw)
    return build_watch_report(_WatchFakeClient(insights, meta), "act_1", **base)


def _apply_followup_actions(report, *, as_of_iso, root):
    """Mirror cli.watch_main's apply loop: file (deduped) / close (idempotent), with an explicit
    root so the test never touches the real followups tree."""
    filed = closed = 0
    for action in report.get("followup_actions", []):
        if action["action"] == "file":
            _, created = _fu.add_followup_if_absent(
                account=action["account"], slug=action["slug"], title=action["title"],
                due=action["due"], note=action["note"], created=as_of_iso,
                marker=action.get("marker", _fu.EARLY_LIFE_MARKER), ad_id=action.get("ad_id", ""),
                root=root,
            )
            filed += int(created)
        elif action["action"] == "close":
            if _fu.mark_done(account=action["account"], task_id=action["task_id"],
                             completed=as_of_iso, missing_ok=True, root=root) is not None:
                closed += 1
    return filed, closed


def test_watch_early_life_keep_watch_files_day3_followup_no_write() -> None:
    # Brand-new struggling ad (age 1) whose comparable past ads recovered → keep on probation and
    # file a day-3 follow-up at first_seen + decision_age. The scan itself writes nothing.
    triaged = _roas_ad("T", days=2, start=date(2026, 6, 25))  # first_seen 6/25 -> age 1
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=2) for i in range(3)]  # 3 recovered
    report = _run_watch(
        insights=[_watch_insight("T", spend=30)],
        meta=[_watch_meta("T")],
        histories=[triaged] + analogs,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "T")
    assert row["early_life"] is True
    assert row["age"] == 1
    assert row["verdict"] == "keep_watch"
    assert row["classification"] == "watch"
    # Cross-sectional grounding, capped low (3 analogs < strong_analogs).
    assert row["confidence"]["grounding_tier"] == "correlational"
    assert row["confidence"]["band"] == "low"
    assert row["analog_basis"]["analogs"] == 3 and row["analog_basis"]["recovered"] == 3

    files = [a for a in report["followup_actions"] if a["action"] == "file" and a["ad_id"] == "T"]
    assert len(files) == 1
    assert files[0]["slug"] == "early-life-triage-T"
    assert files[0]["due"] == "2026-06-27"  # first_seen 6/25 + decision_age 2
    assert files[0]["marker"] == _fu.EARLY_LIFE_MARKER
    # Flag-only: no close action, schema bumped, never an account write/op on the report.
    assert [a for a in report["followup_actions"] if a["action"] == "close"] == []
    assert report["schema_version"] == 2
    assert "ops" not in report and "plan" not in report


def test_watch_early_life_pause_candidate_carries_evidence_no_write() -> None:
    # Brand-new struggling ad whose comparable past ads stayed bad → early pause candidate carrying
    # the analog confidence (≤ medium) + evidence; flag-only, nothing filed, no account write.
    triaged = _roas_ad("T", days=2, start=date(2026, 6, 25))  # age 1
    analogs = [_roas_ad(f"B{i}", days=10) for i in range(3)]  # 0 recovered
    report = _run_watch(
        insights=[_watch_insight("T", spend=30)],
        meta=[_watch_meta("T")],
        histories=[triaged] + analogs,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "T")
    assert row["verdict"] == "pause_candidate"
    assert row["classification"] == "pause_candidate"
    assert row["confidence"]["grounding_tier"] == "correlational"
    assert row["confidence"]["band"] in ("abstain", "low", "medium")  # never high
    assert row["evidence"]["entity_id"] == "T"
    assert row["analog_basis"]["analogs"] == 3 and row["analog_basis"]["recovered"] == 0
    # pause_candidate never auto-writes and never files a follow-up — it is surfaced for the operator
    # to route through propose-pause-ads.
    assert [a for a in report["followup_actions"] if a["ad_id"] == "T"] == []


def test_watch_early_life_install_goal_keep_watch_is_goal_aware() -> None:
    # Install-goal account: a brand-new struggling ad (zero installs on non-trivial spend) graded
    # against comparable new install ads that later booked cheap installs → keep on probation. Proves
    # the policy threads through so the analog grading uses cost-per-install, NOT ROAS (the ad has no
    # purchases at all, so a ROAS-only path would mis-judge it).
    triaged = _install_ad("T", days=2, start=date(2026, 6, 25))  # first_seen 6/25 -> age 1, $10, 0 installs
    analogs = [_install_ad(f"R{i}", days=10, recover_from=2) for i in range(3)]  # 3 recovered (cheap installs)
    report = _run_watch(
        insights=[_watch_insight("T", spend=30)],
        meta=[_watch_meta("T")],
        histories=[triaged] + analogs,
        policy=_INSTALL_POLICY,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "T")
    assert row["early_life"] is True
    assert row["verdict"] == "keep_watch"
    assert row["classification"] == "watch"
    assert row["analog_basis"]["analogs"] == 3 and row["analog_basis"]["recovered"] == 3
    # Goal-aware: the engine's evidence cites the install metric, not ROAS.
    assert row["evidence"]["metric_name"] == "cost_per_app_install"
    files = [a for a in report["followup_actions"] if a["action"] == "file" and a["ad_id"] == "T"]
    assert len(files) == 1


def test_watch_age_from_first_seen_not_updated_time() -> None:
    # An ad edited yesterday (updated_time) but launched two weeks ago (first_seen) is NOT early-life:
    # age must come from first_seen. It falls through to the normal grace-protected path.
    old = _roas_ad("OLD", days=15, start=date(2026, 6, 12))  # first_seen 6/12 -> age 14
    report = _run_watch(
        insights=[_watch_insight("OLD", spend=200)],
        meta=[_watch_meta("OLD", updated="2026-06-25")],  # edited yesterday -> days_since_change 1
        histories=[old],
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "OLD")
    assert not row.get("early_life")
    assert row["classification"] == "watch"  # grace-protected, not triaged
    assert row["days_since_change"] == 1
    assert report["followup_actions"] == []


def test_watch_no_history_falls_back_to_classify_ad() -> None:
    # The provider has no history for a delivering ad (not yet synced) → triage returns nothing and the
    # ad takes today's normal classify_ad path. No crash.
    report = _run_watch(
        insights=[_watch_insight("M", spend=300)],  # mature loser, zero purchases
        meta=[_watch_meta("M", updated="2026-06-01")],  # days_since_change 25 -> unprotected
        histories=[_roas_ad("SOMEONE_ELSE", days=10)],  # M absent from histories
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "M")
    assert not row.get("early_life")
    assert row["classification"] == "urgent"  # mature + below floor -> normal flow
    assert report["followup_actions"] == []


def test_watch_day3_probation_own_sample_clears_floor_keep_and_close(tmp_path: Path) -> None:
    # Age-3 ad on probation whose OWN window now clears the significance floor → a real classify_ad
    # decision (direct observation, grace deliberately overridden), follow-up closed.
    root = tmp_path / "followups"
    _fu.add_followup_if_absent(
        account="acct", slug=_fu.early_life_slug("P"),
        title="day-3 decision", due="2026-06-25", created="2026-06-23",
        marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
    )
    open_followups = _fu.iter_followups("acct", root=root)
    triaged = _roas_ad("P", days=4, start=date(2026, 6, 23))  # first_seen 6/23 -> age 3
    report = _run_watch(
        insights=[_watch_insight("P", spend=300, value=600, purchases=30)],  # ROAS 2.0, clears floor
        meta=[_watch_meta("P", updated="2026-06-24")],  # young (grace) — must be overridden
        histories=[triaged],
        open_followups=open_followups,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "P")
    assert row["age"] == 3
    assert row["verdict"] == "keep"
    assert row["classification"] == "watch"
    # Grace abstain overridden: a REAL direct-observation call, not the "too young to judge" abstain.
    assert row["confidence"]["grounding_tier"] == "direct_observation"
    assert row["confidence"]["band"] != "abstain"
    close = [a for a in report["followup_actions"] if a["action"] == "close" and a["ad_id"] == "P"]
    assert len(close) == 1
    # Applying the close archives the probation follow-up (it stops looping).
    _apply_followup_actions(report, as_of_iso=_WATCH_AS_OF.isoformat(), root=root)
    assert _fu.iter_followups("acct", root=root) == []


def test_watch_day3_probation_own_sample_below_floor_pauses_and_closes(tmp_path: Path) -> None:
    # Age-3 ad on probation whose own window clears the floor but sits BELOW the pause floor → forced
    # to a pause candidate (not another protective watch), follow-up closed.
    root = tmp_path / "followups"
    _fu.add_followup_if_absent(
        account="acct", slug=_fu.early_life_slug("P"), title="day-3 decision",
        due="2026-06-25", created="2026-06-23", marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
    )
    report = _run_watch(
        insights=[_watch_insight("P", spend=300, value=60, purchases=4)],  # ROAS 0.20 < pause floor
        meta=[_watch_meta("P", updated="2026-06-24")],
        histories=[_roas_ad("P", days=4, start=date(2026, 6, 23))],
        open_followups=_fu.iter_followups("acct", root=root),
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "P")
    assert row["classification"] == "pause_candidate"
    assert row["confidence"]["grounding_tier"] == "direct_observation"
    assert any(a["action"] == "close" and a["ad_id"] == "P" for a in report["followup_actions"])


def test_watch_day3_probation_still_below_floor_analog_governs(tmp_path: Path) -> None:
    # Age-3 ad on probation whose own sample is STILL below the significance floor → the analog verdict
    # governs the keep-vs-pause call (no indefinite abstain). Recovering analogs -> keep; non-recovering
    # -> pause. Either way the follow-up closes.
    def run(analogs):
        root = tmp_path / _fu.slugify_name("acct_" + analogs[0].ad_id)  # isolate the two sub-cases
        _fu.add_followup_if_absent(
            account="acct", slug=_fu.early_life_slug("P"), title="day-3 decision",
            due="2026-06-25", created="2026-06-23", marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
        )
        triaged = _roas_ad("P", days=4, start=date(2026, 6, 23))  # spend 60 through age 3, struggling
        report = _run_watch(
            insights=[_watch_insight("P", spend=60)],  # < min_spend 100 -> classify_ad insufficient
            meta=[_watch_meta("P", updated="2026-06-24")],
            histories=[triaged] + analogs,
            open_followups=_fu.iter_followups("acct", root=root),
        )
        row = next(r for r in report["rows"] if r["ad_id"] == "P")
        closed = [a for a in report["followup_actions"] if a["action"] == "close" and a["ad_id"] == "P"]
        return row, closed

    keep_row, keep_close = run([_roas_ad(f"R{i}", days=10, recover_from=4) for i in range(3)])
    assert keep_row["verdict"] == "keep_watch"
    assert keep_row["classification"] == "watch"
    assert keep_row["confidence"]["grounding_tier"] == "correlational"  # analog-grounded
    assert len(keep_close) == 1

    pause_row, pause_close = run([_roas_ad(f"B{i}", days=10) for i in range(3)])
    assert pause_row["verdict"] == "pause_candidate"
    assert pause_row["classification"] == "pause_candidate"
    assert len(pause_close) == 1


def test_watch_day3_probation_install_goal_cheap_installs_keep_and_close(tmp_path: Path) -> None:
    # The reported bug: an install-goal probated ad whose OWN window clears the significance floor with
    # 0 purchases (ROAS ~0 by design) but CHEAP installs (cost/install ≤ target) must be graded a KEEP
    # on cost-per-install, NOT force-paused on ROAS. follow-up closed.
    root = tmp_path / "followups"
    _fu.add_followup_if_absent(
        account="acct", slug=_fu.early_life_slug("P"), title="day-3 decision",
        due="2026-06-25", created="2026-06-23", marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
    )
    report = _run_watch(
        # $300 / 200 installs = $1.50 cost-per-install — well under the $3.00 target; ZERO purchases.
        insights=[_watch_insight("P", spend=300, installs=200)],
        meta=[_watch_meta("P", updated="2026-06-24")],  # young (grace) — must be overridden
        histories=[_install_ad("P", days=4, start=date(2026, 6, 23))],  # first_seen 6/23 -> age 3
        open_followups=_fu.iter_followups("acct", root=root),
        policy=_INSTALL_POLICY,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "P")
    assert row["age"] == 3
    assert row["verdict"] == "keep"
    assert row["classification"] == "watch"
    assert row["classification"] != "pause_candidate"  # the bug: ROAS-only grading paused this ad
    # Graded on the goal metric (cost-per-install) via a real direct-observation call, not ROAS.
    assert row["evidence"]["metric_name"] == "cost_per_app_install"
    assert row["confidence"]["grounding_tier"] == "direct_observation"
    assert row["confidence"]["band"] != "abstain"
    close = [a for a in report["followup_actions"] if a["action"] == "close" and a["ad_id"] == "P"]
    assert len(close) == 1


def test_watch_day3_probation_install_goal_expensive_installs_pauses_and_closes(tmp_path: Path) -> None:
    # Same setup but cost/install OVER target → forced to a pause candidate on the install metric.
    root = tmp_path / "followups"
    _fu.add_followup_if_absent(
        account="acct", slug=_fu.early_life_slug("P"), title="day-3 decision",
        due="2026-06-25", created="2026-06-23", marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
    )
    report = _run_watch(
        # $300 / 50 installs = $6.00 cost-per-install — over the $3.00 target.
        insights=[_watch_insight("P", spend=300, installs=50)],
        meta=[_watch_meta("P", updated="2026-06-24")],
        histories=[_install_ad("P", days=4, start=date(2026, 6, 23))],
        open_followups=_fu.iter_followups("acct", root=root),
        policy=_INSTALL_POLICY,
    )
    row = next(r for r in report["rows"] if r["ad_id"] == "P")
    assert row["classification"] == "pause_candidate"
    assert row["evidence"]["metric_name"] == "cost_per_app_install"
    assert row["confidence"]["grounding_tier"] == "direct_observation"
    assert any(a["action"] == "close" and a["ad_id"] == "P" for a in report["followup_actions"])


def test_watch_day3_probation_install_goal_below_floor_analog_governs(tmp_path: Path) -> None:
    # Install analog of test_watch_day3_probation_still_below_floor_analog_governs: own sample below the
    # significance floor → defer to the analog verdict (graded goal-aware on cost-per-install).
    # Recovering install analogs -> keep; non-recovering -> pause. Either way the follow-up closes.
    def run(analogs):
        root = tmp_path / _fu.slugify_name("acct_" + analogs[0].ad_id)  # isolate the two sub-cases
        _fu.add_followup_if_absent(
            account="acct", slug=_fu.early_life_slug("P"), title="day-3 decision",
            due="2026-06-25", created="2026-06-23", marker=_fu.EARLY_LIFE_MARKER, ad_id="P", root=root,
        )
        triaged = _install_ad("P", days=4, start=date(2026, 6, 23))  # $20 through age 3, 0 installs
        report = _run_watch(
            insights=[_watch_insight("P", spend=60)],  # < min_spend 100 -> own sample insufficient
            meta=[_watch_meta("P", updated="2026-06-24")],
            histories=[triaged] + analogs,
            open_followups=_fu.iter_followups("acct", root=root),
            policy=_INSTALL_POLICY,
        )
        row = next(r for r in report["rows"] if r["ad_id"] == "P")
        closed = [a for a in report["followup_actions"] if a["action"] == "close" and a["ad_id"] == "P"]
        return row, closed

    keep_row, keep_close = run([_install_ad(f"R{i}", days=10, recover_from=4) for i in range(3)])
    assert keep_row["verdict"] == "keep_watch"
    assert keep_row["classification"] == "watch"
    assert keep_row["confidence"]["grounding_tier"] == "correlational"  # analog-grounded
    assert len(keep_close) == 1

    pause_row, pause_close = run([_install_ad(f"B{i}", days=10) for i in range(3)])
    assert pause_row["verdict"] == "pause_candidate"
    assert pause_row["classification"] == "pause_candidate"
    assert len(pause_close) == 1


def test_watch_running_scan_twice_files_one_followup(tmp_path: Path) -> None:
    # Re-running the scan while the ad is still on probation must not spawn a second follow-up: the
    # dedupe holds at both the scan level (already on probation -> no file action) and the file level.
    root = tmp_path / "followups"
    triaged = _roas_ad("T", days=2, start=date(2026, 6, 25))  # age 1
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=2) for i in range(3)]
    insights = [_watch_insight("T", spend=30)]
    meta = [_watch_meta("T")]

    report1 = _run_watch(insights=insights, meta=meta, histories=[triaged] + analogs)
    filed1, _ = _apply_followup_actions(report1, as_of_iso=_WATCH_AS_OF.isoformat(), root=root)
    assert filed1 == 1

    # Second run feeds back the now-open follow-up — the scan returns no new file action.
    open_followups = _fu.iter_followups("acct", root=root)
    report2 = _run_watch(insights=insights, meta=meta, histories=[triaged] + analogs,
                         open_followups=open_followups)
    assert [a for a in report2["followup_actions"] if a["action"] == "file"] == []
    filed2, _ = _apply_followup_actions(report2, as_of_iso=_WATCH_AS_OF.isoformat(), root=root)
    assert filed2 == 0

    open_files = list(_fu.account_dir("acct", root).glob("*.md"))
    assert len(open_files) == 1


def test_watch_build_report_performs_no_followup_writes(tmp_path, monkeypatch) -> None:
    # The pure scan returns followup_actions but writes nothing to the followups tree itself
    # (the CLI applies them). Point the followups root at an empty tmp dir and confirm it stays empty.
    monkeypatch.setattr(_fu, "FOLLOWUPS_ROOT", tmp_path / "followups")
    triaged = _roas_ad("T", days=2, start=date(2026, 6, 25))
    analogs = [_roas_ad(f"R{i}", days=10, recover_from=2) for i in range(3)]
    report = _run_watch(
        insights=[_watch_insight("T", spend=30)],
        meta=[_watch_meta("T")],
        histories=[triaged] + analogs,
    )
    assert any(a["action"] == "file" for a in report["followup_actions"])
    assert not (tmp_path / "followups").exists()  # scan touched no files


def test_followups_add_if_absent_dedupes_and_marker_roundtrips(tmp_path: Path) -> None:
    root = tmp_path / "followups"
    slug = _fu.early_life_slug("123")
    assert slug == "early-life-triage-123"

    path1, created1 = _fu.add_followup_if_absent(
        account="acct", slug=slug, title="t", due="2026-06-27", created="2026-06-26",
        marker=_fu.EARLY_LIFE_MARKER, ad_id="123", root=root,
    )
    assert created1 is True
    # A second call with the same slug is a no-op (same path, created False) even on a different due.
    path2, created2 = _fu.add_followup_if_absent(
        account="acct", slug=slug, title="t", due="2026-07-01", created="2026-06-30",
        marker=_fu.EARLY_LIFE_MARKER, ad_id="123", root=root,
    )
    assert created2 is False and path2 == path1
    assert len(list(_fu.account_dir("acct", root).glob("*.md"))) == 1

    found = _fu.find_open_followup("acct", slug=slug, root=root)
    assert found is not None
    assert _fu.early_life_ad_id(found) == "123"  # ad_id round-trips out of the filename slug
    # A non-early-life follow-up yields None.
    other = _fu.add_followup(account="acct", title="quarterly refresh", due="2026-09-01",
                             created="2026-06-26", root=root)
    assert _fu.early_life_ad_id(_fu._parse(other)) is None


def test_followups_mark_done_missing_ok_is_idempotent(tmp_path: Path) -> None:
    import pytest

    root = tmp_path / "followups"
    path, _ = _fu.add_followup_if_absent(
        account="acct", slug=_fu.early_life_slug("9"), title="t", due="2026-06-27",
        created="2026-06-26", marker=_fu.EARLY_LIFE_MARKER, ad_id="9", root=root,
    )
    task_id = path.stem
    assert _fu.mark_done(account="acct", task_id=task_id, completed="2026-06-28", root=root) is not None
    # Closing an already-archived follow-up must not raise when missing_ok.
    assert _fu.mark_done(account="acct", task_id=task_id, completed="2026-06-29",
                         missing_ok=True, root=root) is None
    with pytest.raises(FileNotFoundError):
        _fu.mark_done(account="acct", task_id=task_id, completed="2026-06-29", root=root)
