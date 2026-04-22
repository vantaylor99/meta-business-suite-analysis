from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from meta_ads_analysis.analyze import build_report_payload
from meta_ads_analysis.cli import build_meta_report_main, ingest_meta_exports_main
from meta_ads_analysis.normalize import ingest_raw_exports
from meta_ads_analysis.reporting import render_markdown_report
from meta_ads_analysis.storage import connect, fetch_run_rows, replace_run_rows
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
    assert creative_b["tracking_confidence"] == "low_purchase_value_missing"
    assert creative_b["purchase_count"] == 2
    assert creative_b["purchase_value"] is None


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
        replace_run_rows(con, run_date, artifacts.normalized_rows, artifacts.creative_rows)
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
            "--run-date",
            run_date,
            "--db-path",
            str(db_path),
            "--output-dir",
            str(reports_root / run_date),
        ],
    )
    build_meta_report_main()

    assert (normalized_root / run_date / "ad_daily_metrics.csv").exists()
    assert (normalized_root / run_date / "ingestion_summary.json").exists()
    assert (reports_root / run_date / "meta_ads_report.md").exists()
    assert (reports_root / run_date / "meta_ads_report.json").exists()

    summary = json.loads((reports_root / run_date / "meta_ads_report.json").read_text(encoding="utf-8"))
    assert summary["account_summary"]["ad_count"] == 1


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
