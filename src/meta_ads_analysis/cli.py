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
from .control import (
    apply_ops_plan,
    build_account_snapshot,
    build_enable_ads_plan,
    build_pause_plan,
    default_audiences_path,
    default_diagnose_path,
    default_metrics_path,
    default_ops_plan_path,
    default_ops_results_path,
    default_snapshot_path,
    fetch_entity_metrics,
    list_account_audiences,
    resolve_ad_account_id,
    scan_issues,
    write_ops_results,
    write_plan,
)
from .reporting import render_markdown_report
from .rotation import (
    apply_advantage_disable_plan,
    apply_rename_plan,
    apply_rotation_plan,
    build_advantage_disable_plan,
    build_rename_plan,
    build_rotation_plan,
    default_advantage_disable_plan_path,
    default_advantage_disable_results_path,
    default_rename_plan_path,
    default_rename_results_path,
    default_rotation_plan_path,
    default_rotation_results_path,
    fetch_active_adsets,
    write_advantage_disable_results,
    write_rename_plan,
    write_rename_results,
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
        "--disable-advantage-audience",
        action="store_true",
        help=(
            "Also set advantage_audience=0 on each rotated ad set that has it enabled, so the "
            "custom audience is genuinely respected. Only ever turns it off, never on."
        ),
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
        disable_advantage_audience=args.disable_advantage_audience,
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
        "--validate-only",
        action="store_true",
        help="Send each approved rotation to Meta with validate_only: real responses, no changes.",
    )
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
    results = apply_rotation_plan(plan, client, execute=args.execute, validate_only=args.validate_only)
    results_path = Path(args.results_path) if args.results_path else default_rotation_results_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rotation_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for item in results if item.status in {"dry_run", "executed", "validated"})
    blocked = sum(1 for item in results if item.status in {"blocked", "failed", "validation_failed"})
    mode = "validate-only" if args.validate_only else ("executed" if args.execute else "dry-run")
    print(f"Completed {mode} rotation for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved rotations: {ran}; blocked or failed: {blocked}")


def propose_disable_advantage_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Propose turning Advantage Audience off on active ad sets, keeping audiences as-is (no writes)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Plan folder date under reports/<account>/. Defaults to today.")
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--output-path", help="Override plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id, adsets = fetch_active_adsets(account_slug, client=client)
    plan = build_advantage_disable_plan(adsets, account_slug=account_slug, ad_account_id=ad_account_id)
    output_path = Path(args.output_path) if args.output_path else default_advantage_disable_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rotation_plan(plan, output_path)
    on = sum(1 for item in plan["items"] if item["advantage_audience"])
    print(f"Wrote Advantage-Audience disable plan for {account_slug}: {output_path}")
    print(f"Ad sets with Advantage Audience currently on: {on} of {len(plan['items'])}")
    for item in plan["items"]:
        flag = "ON -> off" if item["advantage_audience"] else "already off"
        print(f"  {item['adset_name']!r}: {flag}")
    print("Approve by changing an item's status from 'proposed' to 'approved', then run apply-disable-advantage.")


def apply_disable_advantage_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Dry-run, validate, or execute approved Advantage Audience disables through the Graph API."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Plan folder date under reports/<account>/. Defaults to today.")
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--plan-path", help="Override plan path.")
    parser.add_argument("--results-path", help="Override results path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Send each approved change to Meta with validate_only: real responses, no changes.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually apply. Without this flag, this is a dry run.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_advantage_disable_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    if not plan_path.exists():
        raise SystemExit(f"Plan not found: {plan_path}. Run propose-disable-advantage first.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    client = client_from_env(args.api_version)
    results = apply_advantage_disable_plan(plan, client, execute=args.execute, validate_only=args.validate_only)
    results_path = Path(args.results_path) if args.results_path else default_advantage_disable_results_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_advantage_disable_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for item in results if item.status in {"dry_run", "executed", "validated"})
    blocked = sum(1 for item in results if item.status in {"blocked", "failed", "validation_failed"})
    mode = "validate-only" if args.validate_only else ("executed" if args.execute else "dry-run")
    print(f"Completed {mode} Advantage-Audience disable for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved items: {ran}; blocked or failed: {blocked}")


def propose_renames_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Propose ad set names derived from each ad set's current included audience (no writes)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Plan folder date under reports/<account>/. Defaults to today.")
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--output-path", help="Override rename plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id, adsets = fetch_active_adsets(account_slug, client=client)
    plan = build_rename_plan(adsets, account_slug=account_slug, ad_account_id=ad_account_id)
    output_path = Path(args.output_path) if args.output_path else default_rename_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rename_plan(plan, output_path)
    print(f"Wrote rename plan for {account_slug} ({len(plan['renames'])} ad sets): {output_path}")
    for rename in plan["renames"]:
        flag = " (unchanged)" if rename["unchanged"] else ""
        print(f"  {rename['old_name']!r} -> {rename['new_name']!r}{flag}")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    print("Approve by changing a rename's status from 'proposed' to 'approved', then run apply-renames.")


def apply_renames_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Dry-run, validate, or execute approved ad set renames through the Meta Graph API."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Plan folder date under reports/<account>/. Defaults to today.")
    parser.add_argument(
        "--reports-root",
        default=str(DEFAULT_REPORTS_ROOT),
        help="Reports root. Defaults to reports/.",
    )
    parser.add_argument("--plan-path", help="Override rename plan path.")
    parser.add_argument("--results-path", help="Override rename results path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Send each approved rename to Meta with validate_only: real responses, no changes.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually apply approved renames. Without this flag, apply-renames is a dry run.",
    )
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_rename_plan_path(
        account_slug,
        run_date,
        reports_root,
    )
    if not plan_path.exists():
        raise SystemExit(f"Rename plan not found: {plan_path}. Run propose-renames first.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    client = client_from_env(args.api_version)
    results = apply_rename_plan(plan, client, execute=args.execute, validate_only=args.validate_only)
    results_path = Path(args.results_path) if args.results_path else default_rename_results_path(
        account_slug,
        run_date,
        reports_root,
    )
    write_rename_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for item in results if item.status in {"dry_run", "executed", "validated"})
    blocked = sum(1 for item in results if item.status in {"blocked", "failed", "validation_failed"})
    mode = "validate-only" if args.validate_only else ("executed" if args.execute else "dry-run")
    print(f"Completed {mode} renames for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved renames: {ran}; blocked or failed: {blocked}")


def inspect_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Read-only snapshot of an ad account: campaigns -> ad sets -> ads, with status, issues, budgets, audiences."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--active-only", action="store_true", help="Only include ACTIVE campaigns/ad sets.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT), help="Reports root. Defaults to reports/.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    snap = build_account_snapshot(client, ad_account_id, active_only=args.active_only)
    snap["account_slug"] = account_slug
    output_path = default_snapshot_path(account_slug, run_date, reports_root)
    write_plan(snap, output_path)

    r = snap["rollup"]
    print(f"{account_slug} [{ad_account_id}] — {r['campaigns']} campaigns, {r['adsets']} ad sets, "
          f"{r['ads']} ads ({r['active_ads']} active); {r['ads_with_issues']} ads with issues; "
          f"{r['adsets_with_advantage_audience']} ad sets with Advantage Audience on")
    for c in snap["campaigns"]:
        print(f"\n[{c['effective_status']}] CAMPAIGN {c['name']}")
        for a in c["adsets"]:
            aa = " AA:on" if a["advantage_audience"] else ""
            budget = f" ${int(a['daily_budget'])/100:.0f}/day" if a.get("daily_budget") else ""
            print(f"  [{a['effective_status']}]{budget}{aa} {a['name']}  inc={a['included_audiences']} exc={len(a['excluded_audiences'])}")
            for ad in a["ads"]:
                flag = f"  !! {'; '.join(ad['issues'])}" if ad["issues"] else ""
                print(f"      [{ad['effective_status']:<14}] {ad['name']}{flag}")
    if snap["ads_with_issues"]:
        print(f"\nAds with delivery issues: {len(snap['ads_with_issues'])} (see {output_path})")
    print(f"\nSnapshot: {output_path}")


def metrics_main() -> None:
    from .meta_api import client_from_env
    from .sync_api import resolve_date_window

    parser = argparse.ArgumentParser(description="Live per-entity performance (ROAS/spend/purchases) over a window.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--level", choices=["account", "campaign", "adset", "ad"], default="adset")
    parser.add_argument("--date-from", help="Window start YYYY-MM-DD. Defaults to trailing 30 days.")
    parser.add_argument("--date-to", help="Window end YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    date_from, date_to = resolve_date_window(date.today(), date_from=args.date_from, date_to=args.date_to)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    rows = fetch_entity_metrics(client, ad_account_id, level=args.level, date_from=date_from, date_to=date_to)
    out = {"account_slug": account_slug, "level": args.level, "date_from": date_from, "date_to": date_to, "rows": rows}
    output_path = default_metrics_path(account_slug, run_date, args.level, Path(args.reports_root))
    write_plan(out, output_path)

    total_spend = sum(r["spend"] for r in rows)
    total_value = sum(r["purchase_value"] or 0 for r in rows)
    print(f"{account_slug} {args.level} metrics {date_from}..{date_to} — spend ${total_spend:,.0f} "
          f"value ${total_value:,.0f} ROAS {(total_value/total_spend if total_spend else 0):.2f}")
    print(f"{'name':<34}{'spend':>10}{'value':>10}{'ROAS':>7}{'purch':>7}{'CPP':>9}")
    for r in rows:
        print(f"{str(r['name'])[:33]:<34}{r['spend']:>10.0f}{(r['purchase_value'] or 0):>10.0f}"
              f"{(r['roas'] if r['roas'] is not None else 0):>7.2f}{(r['purchases'] or 0):>7.0f}"
              f"{(r['cost_per_purchase'] if r['cost_per_purchase'] is not None else 0):>9.2f}")
    print(f"\nMetrics JSON: {output_path}")


def diagnose_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Scan the account for ad delivery issues, grouped by issue.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    scan = scan_issues(client, ad_account_id)
    scan["account_slug"] = account_slug
    output_path = default_diagnose_path(account_slug, run_date, Path(args.reports_root))
    write_plan(scan, output_path)
    print(f"{account_slug}: {scan['ads_with_issues']} of {scan['ads_scanned']} ads have delivery issues")
    for issue, info in scan["by_issue"].items():
        print(f"\n  [{info['count']}] {issue}")
        for ad in info["ads"][:8]:
            print(f"      - {ad['name']} ({ad['effective_status']})")
        if info["count"] > 8:
            print(f"      ... and {info['count'] - 8} more")
    print(f"\nIssue scan JSON: {output_path}")


def list_audiences_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="List the custom audiences available in the account.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    auds = list_account_audiences(client, ad_account_id)
    output_path = default_audiences_path(account_slug, run_date, Path(args.reports_root))
    write_plan({"account_slug": account_slug, "audiences": auds}, output_path)
    print(f"{account_slug}: {len(auds)} custom audiences")
    for a in auds:
        size = f"{a['size_lower']}-{a['size_upper']}" if a.get("size_lower") is not None else "?"
        print(f"  {str(a['name'])[:44]:<45} {str(a['subtype'] or ''):<14} size~{size} [{a.get('status')}]")
    print(f"\nAudiences JSON: {output_path}")


def propose_pause_ads_main() -> None:
    from .meta_api import client_from_env
    from .sync_api import resolve_date_window

    parser = argparse.ArgumentParser(
        description="Propose pausing ACTIVE ads by filter and/or a performance rule (no writes)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--adset-id", action="append", help="Limit to ad(s) in this ad set id (repeatable).")
    parser.add_argument("--name-contains", help="Limit to ads whose name contains this substring.")
    parser.add_argument("--roas-below", type=float, help="Only ads with ROAS below this (pulls live metrics).")
    parser.add_argument("--min-spend", type=float, default=0.0, help="With --roas-below, require at least this spend.")
    parser.add_argument("--date-from", help="Metrics window start (with --roas-below). Defaults to trailing 30 days.")
    parser.add_argument("--date-to", help="Metrics window end. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--output-path", help="Override ops plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    date_from = date_to = None
    if args.roas_below is not None:
        date_from, date_to = resolve_date_window(date.today(), date_from=args.date_from, date_to=args.date_to)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    plan = build_pause_plan(
        client, ad_account_id, account_slug=account_slug, adset_ids=args.adset_id,
        name_contains=args.name_contains, roas_below=args.roas_below, min_spend=args.min_spend,
        date_from=date_from, date_to=date_to,
    )
    output_path = Path(args.output_path) if args.output_path else default_ops_plan_path(account_slug, run_date, Path(args.reports_root))
    write_plan(plan, output_path)
    print(f"Wrote pause-ads plan for {account_slug} ({len(plan['ops'])} ads): {output_path}")
    for op in plan["ops"]:
        print(f"  {op['name']} — {op['note']}")
    print("Approve by setting an op's status to 'approved', then run apply-ops --validate-only / --execute.")


def propose_enable_ads_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Propose enabling (status ACTIVE) currently-inactive ads, optionally filtered (no writes)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--adset-id", action="append", help="Limit to ad(s) in this ad set id (repeatable).")
    parser.add_argument("--name-contains", help="Limit to ads whose name contains this substring.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT), help="Reports root. Defaults to reports/.")
    parser.add_argument("--output-path", help="Override ops plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    plan = build_enable_ads_plan(
        client, ad_account_id, account_slug=account_slug,
        adset_ids=args.adset_id, name_contains=args.name_contains,
    )
    output_path = Path(args.output_path) if args.output_path else default_ops_plan_path(account_slug, run_date, reports_root)
    write_plan(plan, output_path)
    print(f"Wrote enable-ads plan for {account_slug} ({len(plan['ops'])} inactive ads): {output_path}")
    for op in plan["ops"]:
        print(f"  {op['name']} — {op['note']}")
    print("Approve by setting an op's status to 'approved', then run apply-ops --validate-only / --execute.")


def apply_ops_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Dry-run, validate, or execute an approved ops plan (set_status / set_daily_budget / rename)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT), help="Reports root. Defaults to reports/.")
    parser.add_argument("--plan-path", help="Override ops plan path. Defaults to ops_plan.json.")
    parser.add_argument("--results-path", help="Override results path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument("--validate-only", action="store_true", help="Send each approved op to Meta with validate_only.")
    parser.add_argument("--execute", action="store_true", help="Actually apply approved ops. Without this, it's a dry run.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    reports_root = Path(args.reports_root)
    plan_path = Path(args.plan_path) if args.plan_path else default_ops_plan_path(account_slug, run_date, reports_root)
    if not plan_path.exists():
        raise SystemExit(f"Ops plan not found: {plan_path}. Run propose-enable-ads (or author an ops plan) first.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    client = client_from_env(args.api_version)
    results = apply_ops_plan(plan, client, execute=args.execute, validate_only=args.validate_only)
    results_path = Path(args.results_path) if args.results_path else default_ops_results_path(account_slug, run_date, reports_root)
    write_ops_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for r in results if r.status in {"dry_run", "validated", "executed"})
    blocked = sum(1 for r in results if r.status in {"blocked", "failed", "validation_failed"})
    mode = "validate-only" if args.validate_only else ("executed" if args.execute else "dry-run")
    print(f"Completed {mode} ops for {account_slug} on {run_date}: {results_path}")
    print(f"Runnable approved ops: {ran}; blocked or failed: {blocked}")
    for r in results:
        if r.reason:
            print(f"  {r.op_id}: {r.status} — {r.reason}")


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
