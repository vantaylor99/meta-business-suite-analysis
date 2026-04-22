"""Console entry points."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from .analyze import build_report_payload
from .account_registry import resolve_account
from .config import DEFAULT_DB_PATH, DEFAULT_NORMALIZED_ROOT, DEFAULT_RAW_ROOT, DEFAULT_REPORTS_ROOT
from .normalize import creative_fieldnames, ingest_raw_exports, normalized_fieldnames
from .reporting import render_markdown_report
from .storage import connect, fetch_run_rows, replace_run_rows
from .sync_api import (
    default_normalized_dir,
    default_report_dir,
    sync_account_from_api,
    write_api_sync_summary,
)
from .utils import ensure_dir, slugify_name, write_csv_rows, write_json


def _resolve_account_slug(value: str | None) -> str | None:
    if value is None:
        return None
    return slugify_name(value)


def ingest_run(
    *,
    run_date: str,
    account_slug: str | None,
    input_dir: Path,
    db_path: Path,
    normalized_root: Path,
) -> dict[str, object]:
    artifacts = ingest_raw_exports(input_dir, run_date, account_slug=account_slug)
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
        replace_run_rows(
            con,
            account_slug,
            run_date,
            artifacts.normalized_rows,
            artifacts.creative_rows,
        )

    total_spend = sum(row.get("spend") or 0.0 for row in artifacts.normalized_rows)
    summary = {
        "run_date": run_date,
        "account_slug": account_slug,
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
    return summary


def build_report_run(
    *,
    run_date: str,
    account_slug: str | None,
    db_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    ensure_dir(output_dir)

    with connect(db_path) as con:
        rows = fetch_run_rows(con, run_date, account_slug)

    measurement_focus: dict[str, object] | None = None
    if account_slug:
        try:
            account = resolve_account(account_slug)
        except (FileNotFoundError, KeyError, ValueError):
            account = None
        if account is not None:
            measurement_focus = {
                "primary_metric": account.primary_metric or "results",
                "primary_result_action_type": account.primary_result_action_type,
                "primary_result_label": account.primary_result_label,
                "secondary_metric": account.secondary_metric,
                "secondary_metric_label": account.secondary_metric_label,
                "roas_role": account.roas_role,
                "analysis_notes": account.analysis_notes,
            }

    payload = build_report_payload(rows, run_date, measurement_focus=measurement_focus)
    payload["account_slug"] = account_slug
    write_json(output_dir / "meta_ads_report.json", payload)
    (output_dir / "meta_ads_report.md").write_text(
        render_markdown_report(payload),
        encoding="utf-8",
    )
    return payload


def ingest_meta_exports_main() -> None:
    parser = argparse.ArgumentParser(description="Normalize raw Meta Ads CSV exports.")
    parser.add_argument(
        "--run-date",
        required=True,
        help="Folder date under data/raw/meta_ads/<account_slug>/, e.g. 2026-04-21",
    )
    parser.add_argument(
        "--account",
        help="Account/company slug or name, e.g. pollen_sense or 'Pollen Sense'.",
    )
    parser.add_argument("--input-dir", help="Override the raw input directory.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB database path.")
    parser.add_argument(
        "--normalized-root",
        default=str(DEFAULT_NORMALIZED_ROOT),
        help="Root directory for normalized CSV snapshots.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    input_dir = (
        Path(args.input_dir)
        if args.input_dir
        else (DEFAULT_RAW_ROOT / account_slug / args.run_date if account_slug else DEFAULT_RAW_ROOT / args.run_date)
    )
    normalized_root = (
        Path(args.normalized_root) / account_slug / args.run_date
        if account_slug
        else Path(args.normalized_root) / args.run_date
    )
    db_path = Path(args.db_path)

    summary = ingest_run(
        run_date=args.run_date,
        account_slug=account_slug,
        input_dir=input_dir,
        db_path=db_path,
        normalized_root=normalized_root,
    )
    account_context = f" for {account_slug}" if account_slug else ""
    print(f"Ingested {summary['row_count']} rows{account_context} on {args.run_date} into {db_path}")


def build_meta_report_main() -> None:
    parser = argparse.ArgumentParser(description="Build a Markdown and JSON report from normalized Meta Ads data.")
    parser.add_argument("--run-date", required=True, help="Ingestion run date to report on.")
    parser.add_argument(
        "--account",
        help="Account/company slug or name, e.g. pollen_sense or 'Pollen Sense'.",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB database path.")
    parser.add_argument(
        "--output-dir",
        help="Override the report output directory. Defaults to reports/<account_slug>/<run_date>/ when --account is set.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    db_path = Path(args.db_path)
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (
            DEFAULT_REPORTS_ROOT / account_slug / args.run_date
            if account_slug
            else DEFAULT_REPORTS_ROOT / args.run_date
        )
    )
    build_report_run(
        run_date=args.run_date,
        account_slug=account_slug,
        db_path=db_path,
        output_dir=output_dir,
    )
    account_context = f" for {account_slug}" if account_slug else ""
    print(f"Built report{account_context} on {args.run_date} in {output_dir}")


def sync_meta_api_main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Meta Ads data from the Marketing API and build reports.")
    parser.add_argument(
        "--account",
        required=True,
        help="Account/company slug or name, e.g. pollen_sense or 'Pollen Sense'.",
    )
    parser.add_argument("--run-date", required=True, help="Snapshot folder date in YYYY-MM-DD format.")
    parser.add_argument("--date-from", help="Optional reporting window start date in YYYY-MM-DD format.")
    parser.add_argument("--date-to", help="Optional reporting window end date in YYYY-MM-DD format.")
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Only write raw CSV exports from the API and skip ingest/report.",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB database path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required for sync-api.")

    artifacts = sync_account_from_api(
        account_slug=account_slug,
        run_date=args.run_date,
        date_from=args.date_from,
        date_to=args.date_to,
        api_version=args.api_version,
    )

    normalized_dir = default_normalized_dir(artifacts.run_date, account_slug)
    report_dir = default_report_dir(artifacts.run_date, account_slug)
    if not args.raw_only:
        ingest_run(
            run_date=artifacts.run_date,
            account_slug=account_slug,
            input_dir=artifacts.raw_dir,
            db_path=Path(args.db_path),
            normalized_root=normalized_dir,
        )
        build_report_run(
            run_date=artifacts.run_date,
            account_slug=account_slug,
            db_path=Path(args.db_path),
            output_dir=report_dir,
        )

    summary_path = write_api_sync_summary(
        artifacts,
        normalized_dir=normalized_dir if not args.raw_only else None,
        report_dir=report_dir if not args.raw_only else None,
        completed_full_pipeline=not args.raw_only,
    )

    print(f"Synced Meta API data for {account_slug} on {artifacts.run_date}")
    print(f"Raw exports: {artifacts.raw_dir}")
    if not args.raw_only:
        print(f"Normalized data: {normalized_dir}")
        print(f"Report output: {report_dir}")
    print(f"Sync summary: {summary_path}")
