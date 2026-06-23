"""Console entry points."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .actions import (
    apply_action_plan,
    build_action_plan,
    default_action_plan_path,
    default_action_results_path,
    enrich_action_plan_with_live_state,
    find_latest_report_run,
    load_report_payload,
    write_action_plan,
    write_apply_results,
)
from .analyze import build_report_payload
from .account_registry import resolve_account
from .briefs import (
    build_operator_brief,
    default_operator_brief_json_path,
    default_operator_brief_path,
    find_previous_report_run,
    load_plan,
    load_report,
    write_operator_brief,
)
from .config import DEFAULT_DB_PATH, DEFAULT_NORMALIZED_ROOT, DEFAULT_RAW_ROOT, DEFAULT_REPORTS_ROOT
from .normalize import creative_fieldnames, ingest_raw_exports, normalized_fieldnames
from .reporting import render_markdown_report
from .rotation import (
    apply_rotation_plan,
    build_rotation_plan,
    default_rotation_plan_path,
    default_rotation_results_path,
    fetch_active_adsets,
    write_rotation_plan,
    write_rotation_results,
)
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
    from .storage import connect, replace_run_rows

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
    from .storage import connect, fetch_run_rows

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


def propose_meta_actions_main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a human-approved Meta action plan from a report JSON."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--run-date",
        help="Report run date. Defaults to latest reports/<account_slug>/YYYY-MM-DD folder.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument(
        "--output-path",
        help="Override action plan path. Defaults to reports/<account>/<run_date>/action_plan.json.",
    )
    parser.add_argument(
        "--enrich-live-state",
        action="store_true",
        help="Use read-only Graph API lookups to include current live status in the action plan.",
    )
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    reports_root = Path(args.reports_root)
    run_date = args.run_date or find_latest_report_run(account_slug, reports_root)
    payload = load_report_payload(account_slug, run_date, reports_root)
    plan = build_action_plan(payload)
    if args.enrich_live_state:
        from .meta_api import client_from_env

        plan = enrich_action_plan_with_live_state(plan, client=client_from_env(args.api_version))
    output_path = Path(args.output_path) if args.output_path else default_action_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_action_plan(plan, output_path)
    executable_count = sum(1 for action in plan["actions"] if action.get("executable"))
    print(f"Wrote action plan for {account_slug} on {run_date}: {output_path}")
    print(f"Actions proposed: {len(plan['actions'])}; executable after approval: {executable_count}")
    print("Approve by changing an executable action status from 'proposed' to 'approved'.")


def apply_meta_actions_main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run or execute approved actions from action_plan.json through Meta CLI."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--run-date",
        help="Report run date. Defaults to latest reports/<account_slug>/YYYY-MM-DD folder.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument(
        "--plan-path",
        help="Override action plan path. Defaults to reports/<account>/<run_date>/action_plan.json.",
    )
    parser.add_argument(
        "--results-path",
        help="Override action results path. Defaults to timestamped action_results file.",
    )
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute approved actions. Without this flag, apply-actions is a dry run.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    reports_root = Path(args.reports_root)
    run_date = args.run_date or find_latest_report_run(account_slug, reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_action_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    if not plan_path.exists():
        raise SystemExit(f"Action plan not found: {plan_path}. Run propose-actions first.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    client = None
    if args.execute:
        from .meta_api import client_from_env

        client = client_from_env(args.api_version)
    results = apply_action_plan(plan, execute=args.execute, client=client)
    results_path = Path(args.results_path) if args.results_path else default_action_results_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_apply_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    executed_or_ready = sum(1 for item in results if item.status in {"dry_run", "executed"})
    blocked_or_failed = sum(1 for item in results if item.status in {"blocked", "failed"})
    mode = "executed" if args.execute else "dry-run"
    print(f"Completed {mode} for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved actions: {executed_or_ready}; blocked or failed: {blocked_or_failed}")


def propose_rotation_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Read active ad sets and propose an audience rotation plan (no writes)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--run-date",
        help="Plan folder date under reports/<account>/. Defaults to today.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=1,
        help="How many ad sets forward to shift each audience. Defaults to 1.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--output-path", help="Override rotation plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id, adsets = fetch_active_adsets(account_slug, client=client)
    plan = build_rotation_plan(
        adsets,
        account_slug=account_slug,
        ad_account_id=ad_account_id,
        offset=args.offset,
    )
    output_path = Path(args.output_path) if args.output_path else default_rotation_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rotation_plan(plan, output_path)
    print(f"Wrote rotation plan for {account_slug} ({len(plan['rotations'])} ad sets): {output_path}")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    print("Approve by changing a rotation's status from 'proposed' to 'approved', then run apply-rotation.")


def apply_rotation_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Dry-run or execute approved audience rotations through the Meta Graph API."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--run-date",
        help="Plan folder date under reports/<account>/. Defaults to today.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--plan-path", help="Override rotation plan path.")
    parser.add_argument("--results-path", help="Override rotation results path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually apply approved rotations. Without this flag, apply-rotation is a dry run.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_rotation_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    if not plan_path.exists():
        raise SystemExit(f"Rotation plan not found: {plan_path}. Run propose-rotation first.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    client = client_from_env(args.api_version)
    results = apply_rotation_plan(plan, client, execute=args.execute)
    results_path = Path(args.results_path) if args.results_path else default_rotation_results_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rotation_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for item in results if item.status in {"dry_run", "executed"})
    blocked = sum(1 for item in results if item.status in {"blocked", "failed"})
    mode = "executed" if args.execute else "dry-run"
    print(f"Completed {mode} rotation for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved rotations: {ran}; blocked or failed: {blocked}")


def operator_brief_main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a concise operator brief from an action plan and report."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--run-date",
        help="Report run date. Defaults to latest reports/<account_slug>/YYYY-MM-DD folder.",
    )
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument(
        "--plan-path",
        help="Override action plan path. Defaults to reports/<account>/<run_date>/action_plan.json.",
    )
    parser.add_argument(
        "--report-path",
        help="Override report JSON path. Defaults to reports/<account>/<run_date>/meta_ads_report.json.",
    )
    parser.add_argument(
        "--output-path",
        help="Override Markdown brief path. Defaults to reports/<account>/<run_date>/operator_brief.md.",
    )
    parser.add_argument(
        "--json-output-path",
        help="Override JSON brief path. Defaults to reports/<account>/<run_date>/operator_brief.json.",
    )
    parser.add_argument(
        "--no-previous",
        action="store_true",
        help="Skip comparison against the previous report run.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    reports_root = Path(args.reports_root)
    run_date = args.run_date or find_latest_report_run(account_slug, reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_action_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    report_path = Path(args.report_path) if args.report_path else (
        reports_root / account_slug / run_date / "meta_ads_report.json"
    )
    if not plan_path.exists():
        raise SystemExit(f"Action plan not found: {plan_path}. Run propose-actions first.")
    if not report_path.exists():
        raise SystemExit(f"Report JSON not found: {report_path}. Run report first.")

    previous_plan = None
    previous_report = None
    if not args.no_previous:
        previous_run = find_previous_report_run(account_slug, run_date, reports_root)
        if previous_run:
            previous_plan_path = default_action_plan_path(account_slug, previous_run, reports_root)
            previous_report_path = reports_root / account_slug / previous_run / "meta_ads_report.json"
            if previous_plan_path.exists():
                previous_plan = load_plan(previous_plan_path)
            if previous_report_path.exists():
                previous_report = load_report(previous_report_path)

    brief = build_operator_brief(
        plan=load_plan(plan_path),
        report=load_report(report_path),
        previous_plan=previous_plan,
        previous_report=previous_report,
    )
    markdown_path = Path(args.output_path) if args.output_path else default_operator_brief_path(
        account_slug,
        run_date,
        reports_root,
    )
    json_path = (
        Path(args.json_output_path)
        if args.json_output_path
        else default_operator_brief_json_path(account_slug, run_date, reports_root)
    )
    write_operator_brief(brief=brief, markdown_path=markdown_path, json_path=json_path)
    print(f"Wrote operator brief for {account_slug} on {run_date}: {markdown_path}")
    print(f"Brief JSON: {json_path}")
