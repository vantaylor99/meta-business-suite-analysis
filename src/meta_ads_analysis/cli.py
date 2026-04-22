"""Console entry points."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .analyze import build_report_payload
from .config import DEFAULT_DB_PATH, DEFAULT_NORMALIZED_ROOT, DEFAULT_RAW_ROOT, DEFAULT_REPORTS_ROOT
from .normalize import creative_fieldnames, ingest_raw_exports, normalized_fieldnames
from .reporting import render_markdown_report
from .storage import connect, fetch_run_rows, replace_run_rows
from .utils import ensure_dir, write_csv_rows, write_json


def ingest_meta_exports_main() -> None:
    parser = argparse.ArgumentParser(description="Normalize raw Meta Ads CSV exports.")
    parser.add_argument("--run-date", required=True, help="Folder date under data/raw/meta_ads, e.g. 2026-04-21")
    parser.add_argument("--input-dir", help="Override the raw input directory.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB database path.")
    parser.add_argument(
        "--normalized-root",
        default=str(DEFAULT_NORMALIZED_ROOT),
        help="Root directory for normalized CSV snapshots.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir) if args.input_dir else DEFAULT_RAW_ROOT / args.run_date
    normalized_root = Path(args.normalized_root) / args.run_date
    db_path = Path(args.db_path)

    artifacts = ingest_raw_exports(input_dir, args.run_date)
    ensure_dir(normalized_root)
    write_csv_rows(
        normalized_root / "ad_daily_metrics.csv",
        artifacts.normalized_rows,
        normalized_fieldnames(),
    )
    write_csv_rows(
        normalized_root / "creative_lookup.csv",
        artifacts.creative_rows,
        creative_fieldnames(),
    )

    with connect(db_path) as con:
        replace_run_rows(con, args.run_date, artifacts.normalized_rows, artifacts.creative_rows)

    total_spend = sum(row.get("spend") or 0.0 for row in artifacts.normalized_rows)
    summary = {
        "run_date": args.run_date,
        "input_dir": str(input_dir),
        "normalized_dir": str(normalized_root),
        "db_path": str(db_path),
        "row_count": len(artifacts.normalized_rows),
        "creative_row_count": len(artifacts.creative_rows),
        "total_spend": round(total_spend, 2),
        "warnings": artifacts.warnings,
        "generated_on": date.today().isoformat(),
    }
    write_json(normalized_root / "ingestion_summary.json", summary)
    print(f"Ingested {len(artifacts.normalized_rows)} rows for {args.run_date} into {db_path}")


def build_meta_report_main() -> None:
    parser = argparse.ArgumentParser(description="Build a Markdown and JSON report from normalized Meta Ads data.")
    parser.add_argument("--run-date", required=True, help="Ingestion run date to report on.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB database path.")
    parser.add_argument(
        "--output-dir",
        help="Override the report output directory. Defaults to reports/<run_date>/",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_REPORTS_ROOT / args.run_date
    ensure_dir(output_dir)

    with connect(db_path) as con:
        rows = fetch_run_rows(con, args.run_date)

    payload = build_report_payload(rows, args.run_date)
    write_json(output_dir / "meta_ads_report.json", payload)
    (output_dir / "meta_ads_report.md").write_text(
        render_markdown_report(payload),
        encoding="utf-8",
    )
    print(f"Built report for {args.run_date} in {output_dir}")
