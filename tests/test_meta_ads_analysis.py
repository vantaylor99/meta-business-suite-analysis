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
    build_meta_cli_command,
    enrich_action_plan_with_live_state,
)
from meta_ads_analysis.account_registry import load_account_registry, resolve_account
from meta_ads_analysis.analyze import build_report_payload
from meta_ads_analysis.cli import build_meta_report_main, ingest_meta_exports_main, sync_meta_api_main
from meta_ads_analysis.meta_api import MetaApiError, MetaMarketingApiClient
from meta_ads_analysis.normalize import ingest_raw_exports
from meta_ads_analysis.reporting import render_markdown_report
from meta_ads_analysis.storage import connect, fetch_run_rows, replace_run_rows
from meta_ads_analysis.sync_api import resolve_date_window, sync_account_from_cli
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


def test_sync_cli_writes_raw_files_from_meta_cli_payload(tmp_path: Path) -> None:
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
                        "measurement_focus": {
                            "primary_result_action_type": "purchase",
                            "primary_result_label": "Website purchases",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_runner(command, check, capture_output, text):
        assert command[:6] == ["meta", "--output", "json", "ads", "--ad-account-id", "act_555"]
        if command[6:8] == ["ad", "list"]:
            return Mock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": "3",
                            "name": "CLI Ad",
                            "adset_id": "2",
                            "campaign_id": "1",
                            "status": "ACTIVE",
                            "effective_status": "ACTIVE",
                        }
                    ]
                ),
                stderr="",
            )
        assert command[-2:] == ["--ad-id", "3"]
        payload = {
            "data": [
                {
                    "account_id": "act_555",
                    "account_name": "Divine Designs",
                    "campaign_id": "1",
                    "campaign_name": "Campaign",
                    "adset_id": "2",
                    "adset_name": "Set",
                    "ad_id": "3",
                    "ad_name": "CLI Ad",
                    "date_start": "2026-06-15",
                    "date_stop": "2026-06-15",
                    "impressions": "1000",
                    "spend": "50",
                    "actions": [
                        {"action_type": "purchase", "value": "2"},
                        {"action_type": "video_view", "value": "300"},
                    ],
                    "action_values": [{"action_type": "purchase", "value": "120"}],
                    "purchase_roas": [{"action_type": "purchase", "value": "2.4"}],
                }
            ]
        }
        return Mock(returncode=0, stdout=json.dumps(payload), stderr="")

    artifacts = sync_account_from_cli(
        account_slug="divine_designs",
        run_date="2026-06-16",
        raw_root=raw_root,
        accounts_config_path=accounts_path,
        meta_binary="meta",
        runner=fake_runner,
    )

    assert artifacts.api_version == "meta-cli"
    assert (raw_root / "divine_designs" / "2026-06-16" / "performance_daily.csv").exists()
    assert artifacts.performance_rows[0]["Ad name"] == "CLI Ad"
    assert artifacts.performance_rows[0]["Results"] == "2"
    assert artifacts.creative_rows[0]["Ad ID"] == "3"


def test_sync_cli_default_filter_skips_old_paused_ads(tmp_path: Path) -> None:
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
    insight_ad_ids: list[str] = []

    def fake_runner(command, check, capture_output, text):
        if command[6:8] == ["ad", "list"]:
            return Mock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": "active",
                            "name": "Active Ad",
                            "status": "ACTIVE",
                            "effective_status": "ACTIVE",
                            "updated_time": "2026-01-01T00:00:00+0000",
                        },
                        {
                            "id": "recent-paused",
                            "name": "Recently Paused Ad",
                            "status": "PAUSED",
                            "effective_status": "PAUSED",
                            "updated_time": "2026-06-10T00:00:00+0000",
                        },
                        {
                            "id": "old-paused",
                            "name": "Old Paused Ad",
                            "status": "PAUSED",
                            "effective_status": "PAUSED",
                            "updated_time": "2026-04-01T00:00:00+0000",
                        },
                    ]
                ),
                stderr="",
            )
        ad_id = command[-1]
        insight_ad_ids.append(ad_id)
        return Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": [
                        {
                            "ad_id": ad_id,
                            "ad_name": f"{ad_id} name",
                            "date_start": "2026-06-15",
                            "date_stop": "2026-06-15",
                            "spend": "1",
                        }
                    ]
                }
            ),
            stderr="",
        )

    artifacts = sync_account_from_cli(
        account_slug="divine_designs",
        run_date="2026-06-16",
        raw_root=raw_root,
        accounts_config_path=accounts_path,
        runner=fake_runner,
    )

    assert insight_ad_ids == ["active", "recent-paused"]
    assert len(artifacts.performance_rows) == 2
    assert "selected 2 of 3 ads" in artifacts.warnings[0]


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


def test_meta_cli_command_only_allows_explicit_pause_without_meta_ai_params() -> None:
    action = {
        "action_type": "pause_ad",
        "target": {"id": "123"},
        "params": {"status": "paused"},
    }

    command = build_meta_cli_command(action, "act_999", meta_binary="meta")

    assert command == [
        "meta",
        "--no-input",
        "-o",
        "json",
        "ads",
        "--ad-account-id",
        "act_999",
        "ad",
        "update",
        "123",
        "--status",
        "paused",
    ]

    action["params"]["advantage_plus_creative"] = True
    try:
        build_meta_cli_command(action, "act_999")
    except ValueError as exc:
        assert "Meta AI" in str(exc)
    else:
        raise AssertionError("Expected Meta AI guardrail to block action")


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
    assert dry_run[0].command is not None
    assert "act_12345" in dry_run[0].command


def test_live_state_enrichment_marks_only_ad_status_paused_as_resolved(tmp_path: Path, monkeypatch) -> None:
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

    def fake_runner(command, check, capture_output, text):
        ad_id = command[-1]
        status = "PAUSED" if ad_id == "1" else "ACTIVE"
        effective_status = "PAUSED" if ad_id == "1" else "ADSET_PAUSED"
        return Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": ad_id,
                        "name": f"Ad {ad_id}",
                        "status": status,
                        "effective_status": effective_status,
                    }
                ]
            ),
            stderr="",
        )

    enriched = enrich_action_plan_with_live_state(plan, runner=fake_runner)
    by_id = {action["action_id"]: action for action in enriched["actions"]}

    assert by_id["pause_ad_1"]["status"] == "already_resolved"
    assert by_id["pause_ad_1"]["executable"] is False
    assert by_id["pause_ad_2"]["status"] == "proposed"
    assert by_id["pause_ad_2"]["executable"] is True
    assert by_id["pause_ad_2"]["live_state"]["effective_status"] == "ADSET_PAUSED"


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
