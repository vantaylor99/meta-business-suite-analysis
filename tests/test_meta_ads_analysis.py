from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

from meta_ads_analysis.actions import (
    apply_action_plan,
    build_action_plan,
    build_api_operation,
    enrich_action_plan_with_live_state,
)
from meta_ads_analysis.account_registry import load_account_registry, resolve_account
from meta_ads_analysis.analyze import build_report_payload
from meta_ads_analysis.briefs import build_operator_brief, render_operator_brief
from meta_ads_analysis.cli import build_meta_report_main, ingest_meta_exports_main, sync_meta_api_main
from meta_ads_analysis.meta_api import MetaApiError, MetaMarketingApiClient
from meta_ads_analysis.normalize import ingest_raw_exports
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

    enriched = enrich_action_plan_with_live_state(plan, client=client)
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

    enriched = enrich_action_plan_with_live_state(plan, client=client)
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

    enriched = enrich_action_plan_with_live_state(plan, client=client)

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


# --- Control layer (inspect + guarded ops + enable-ads) ----------------------

from meta_ads_analysis.control import (
    apply_ops_plan,
    build_account_snapshot,
    build_enable_ads_plan,
    validate_op,
)


class _ControlFakeClient:
    """Fake client for control-layer tests: campaigns/adsets/ads + updates."""

    def __init__(self, campaigns, adsets, ads):
        self._campaigns = campaigns
        self._adsets = adsets
        self._ads = ads
        self._by_id = {e["id"]: e for e in campaigns + adsets + ads}
        self.updates = []

    def list_campaigns(self, ad_account_id, *, fields, effective_status=None):
        return self._campaigns

    def list_adsets(self, ad_account_id, *, fields, effective_status=None):
        return self._adsets

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


# --- Authoring (create / duplicate / lookalike) + breakdowns + account-info --

from meta_ads_analysis.authoring import (
    apply_authoring_plan,
    build_duplicate_ad_plan,
    build_lookalike_plan,
    validate_authoring_op,
)
from meta_ads_analysis.control import account_info, fetch_breakdown_metrics


class _AuthoringFakeClient:
    def __init__(self, ad_creative_id="cr1"):
        self._creative_id = ad_creative_id
        self.creates = []

    def get_ad(self, ad_id, *, fields):
        return {"id": ad_id, "name": "Source Ad", "creative": {"id": self._creative_id}}

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

    client = _AuthoringFakeClient()
    plan["ops"][0]["status"] = "approved"
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
