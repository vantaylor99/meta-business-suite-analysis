"""Console entry points."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
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
from .authoring import (
    apply_authoring_plan,
    build_duplicate_ad_plan,
    build_lookalike_plan,
    build_video_ad_plan,
    default_authoring_plan_path,
    default_authoring_results_path,
    write_authoring_plan,
    write_authoring_results,
)
from .control import (
    account_info,
    apply_ops_plan,
    build_account_snapshot,
    build_budget_plan,
    build_copy_library,
    build_enable_ads_plan,
    build_pause_plan,
    DEFAULT_OPT_IN_FEATURES,
    DEFAULT_OPT_OUT_FEATURES,
    default_winning_copy_path,
    default_audiences_path,
    default_diagnose_path,
    default_metrics_path,
    default_ops_plan_path,
    default_ops_results_path,
    default_snapshot_path,
    estimate_adset_audience,
    fetch_breakdown_metrics,
    fetch_entity_metrics,
    list_account_audiences,
    list_account_conversions,
    list_account_pixels,
    render_copy_library_md,
    resolve_ad_account_id,
    scan_issues,
    search_interests,
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

        plan = enrich_action_plan_with_live_state(plan, reader=client_from_env(args.api_version))
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
        "--date-from",
        help="Fatigue-evidence window start. Defaults to a 30-day trailing window.",
    )
    parser.add_argument(
        "--date-to",
        help="Fatigue-evidence window end. Defaults to the run date / today.",
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

    from .control import _resolve_grounding_window, resolve_action_policy

    client = client_from_env(args.api_version)
    ad_account_id, adsets = fetch_active_adsets(account_slug, reader=client)
    policy = resolve_action_policy(account_slug)
    date_from, date_to, recency_days, run_date_iso = _resolve_grounding_window(
        args.date_from, args.date_to, run_date
    )
    # Read each ad set's recent performance — the fatigue signal that grounds the swap.
    metrics_by_id = {
        str(m["id"]): m
        for m in fetch_entity_metrics(
            client, ad_account_id, level="adset", date_from=date_from, date_to=date_to
        )
    }
    plan = build_rotation_plan(
        adsets,
        account_slug=account_slug,
        ad_account_id=ad_account_id,
        offset=args.offset,
        disable_advantage_audience=args.disable_advantage_audience,
        metrics_by_id=metrics_by_id,
        goal=policy.get("primary_goal"),
        policy=policy,
        date_from=date_from,
        date_to=date_to,
        recency_days=recency_days,
        run_date=run_date_iso,
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
    ad_account_id, adsets = fetch_active_adsets(account_slug, reader=client)
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
    ad_account_id, adsets = fetch_active_adsets(account_slug, reader=client)
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
    parser.add_argument(
        "--breakdown",
        help="Split by a dimension instead of by entity (e.g. age, gender, country, "
             "publisher_platform, platform_position, impression_device). Comma-separate for multiple.",
    )
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

    if args.breakdown:
        rows = fetch_breakdown_metrics(
            client, ad_account_id, breakdown=args.breakdown, date_from=date_from, date_to=date_to, level=args.level
        )
        out = {"account_slug": account_slug, "level": args.level, "breakdown": args.breakdown,
               "date_from": date_from, "date_to": date_to, "rows": rows}
        output_path = default_metrics_path(account_slug, run_date, f"{args.level}_by_{args.breakdown.replace(',', '_')}", Path(args.reports_root))
        write_plan(out, output_path)
        total_spend = sum(r["spend"] for r in rows)
        total_value = sum(r["purchase_value"] or 0 for r in rows)
        print(f"{account_slug} {args.level} by {args.breakdown} {date_from}..{date_to} — spend ${total_spend:,.0f} "
              f"value ${total_value:,.0f} ROAS {(total_value/total_spend if total_spend else 0):.2f}")
        print(f"{'segment':<34}{'spend':>10}{'value':>10}{'ROAS':>7}{'purch':>7}")
        for r in rows:
            seg = ", ".join(str(v) for v in r["segment"].values())
            print(f"{seg[:33]:<34}{r['spend']:>10.0f}{(r['purchase_value'] or 0):>10.0f}"
                  f"{(r['roas'] if r['roas'] is not None else 0):>7.2f}{(r['purchases'] or 0):>7.0f}")
        print(f"\nMetrics JSON: {output_path}")
        return

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


def lint_vault_main() -> None:
    """Lint the knowledge vault: enforce provenance tags on every learning and age out stale facts.

    Scans ``knowledge/learnings.md`` (full enforcement) and, if present, the divine_designs
    ``profile.md`` baseline header (staleness only). CI-usable: exits 1 on any error.
    """
    from .config import KNOWLEDGE_REVERIFY_DAYS, PROJECT_ROOT
    from .knowledge_provenance import (
        lint,
        lint_profile_baseline,
        parse_learnings,
        render_report,
    )

    parser = argparse.ArgumentParser(
        description="Lint the knowledge vault: require provenance tags + flag stale (fast) facts."
    )
    parser.add_argument("--path", help="learnings.md to lint (default: knowledge/learnings.md).")
    parser.add_argument(
        "--profile",
        help="profile.md to scan for baseline staleness "
        "(default: knowledge/accounts/divine_designs/profile.md; skipped if missing).",
    )
    parser.add_argument("--today", help="Override today (YYYY-MM-DD) for staleness math.")
    parser.add_argument(
        "--reverify-days",
        type=int,
        default=KNOWLEDGE_REVERIFY_DAYS,
        help=f"Age (days) after which a fast fact is flagged re-verify (default {KNOWLEDGE_REVERIFY_DAYS}).",
    )
    parser.add_argument("--strict", action="store_true", help="Treat ⏳ warnings as failures too.")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    learnings_path = Path(args.path) if args.path else PROJECT_ROOT / "knowledge" / "learnings.md"
    entries = parse_learnings(learnings_path.read_text(encoding="utf-8"))
    findings = lint(entries, today=today, reverify_days=args.reverify_days)

    profile_path = (
        Path(args.profile)
        if args.profile
        else PROJECT_ROOT / "knowledge" / "accounts" / "divine_designs" / "profile.md"
    )
    if profile_path.exists():
        findings += lint_profile_baseline(
            profile_path.read_text(encoding="utf-8"),
            today=today,
            reverify_days=args.reverify_days,
            label=profile_path.name,
        )

    report, exit_code = render_report(findings, entries_count=len(entries), strict=args.strict)
    print(report)
    raise SystemExit(exit_code)


# --- audit-vault (drift re-check) -------------------------------------------------------------
#
# `audit-vault` re-pulls each stored `metric:` claim over a FRESH trailing window and diffs it
# against the stored value (the pure verdict + markdown mutation live in knowledge_provenance.py).
# This module is the ONLY Meta-touching half: it resolves the named metric out of the live rows
# (`resolve_fresh_metric`) and orchestrates select → re-pull → classify → render/apply
# (`run_vault_audit`). Both are pure given an injected `fetch_metrics`, so they unit-test with a
# fake provider (no live Meta in tests).

# Abbreviation aliases so a metric name's identifier token can match a fresh row's segment value or
# entity name (e.g. `ig_roas` ↔ an `instagram` publisher_platform row).
_SEGMENT_ALIASES: dict[str, str] = {
    "ig": "instagram",
    "fb": "facebook",
    "an": "audience_network",
}
# Tokens in a metric NAME that describe the measure or the scope, not WHICH entity/segment it is —
# dropped before matching so `engaged_adset_roas` reduces to the discriminating token {engaged}.
_DROP_METRIC_TOKENS: frozenset[str] = frozenset(
    {"roas", "aov", "cpp", "cpa", "cpm", "cpc", "ctr", "cvr", "spend", "value", "purchases", "cost",
     "account", "campaign", "adset", "ad", "level", "blended"}
)


def _opt_float(value: object) -> float | None:
    """A config threshold coerced to float, or None if absent/non-numeric (no fabricated default)."""
    return float(value) if isinstance(value, (int, float)) else None


def _row_value(row: dict[str, object]) -> tuple[float | None, float | None, float | None]:
    """``(roas, purchases, spend)`` from one fetched metrics row. ROAS stays ``None`` when value is
    missing for the window (→ could_not_audit, never a fabricated 0 — see AGENTS.md / the ticket's
    'Derived ROAS' edge case)."""
    spend = row.get("spend")
    purchases = row.get("purchases")
    roas = row.get("roas")
    value = row.get("purchase_value")
    if roas is None and value is not None and isinstance(spend, (int, float)) and spend:
        roas = round(value / spend, 2)
    return (
        roas if isinstance(roas, (int, float)) else None,
        purchases if isinstance(purchases, (int, float)) else None,
        spend if isinstance(spend, (int, float)) else None,
    )


def _aggregate_value(rows: list[dict[str, object]]) -> tuple[float | None, float | None, float | None]:
    """Blended ``(roas, purchases, spend)`` over ``rows`` (the account-aggregate case). If every row
    is missing purchase_value, or total spend is 0, the value is ``None`` (could_not_audit) rather
    than a fabricated 0."""
    spend = sum(float(r["spend"]) for r in rows if isinstance(r.get("spend"), (int, float)))
    purchases = sum(float(r["purchases"]) for r in rows if isinstance(r.get("purchases"), (int, float)))
    values = [r.get("purchase_value") for r in rows]
    if not spend or all(v is None for v in values):
        return None, (purchases or None), (spend or None)
    total_value = sum(float(v) for v in values if isinstance(v, (int, float)))
    return round(total_value / spend, 2), purchases, spend


def _metric_identifier_tokens(metric_name: str | None) -> set[str]:
    """Discriminating identity tokens of a metric name, alias-expanded — `ig_roas` → {instagram},
    `engaged_adset_roas` → {engaged}. Measure/scope words are dropped (see `_DROP_METRIC_TOKENS`)."""
    parts = [p for p in re.split(r"[_\s]+", (metric_name or "").lower()) if p]
    return {_SEGMENT_ALIASES.get(p, p) for p in parts if p not in _DROP_METRIC_TOKENS}


def _row_identifier_text(row: dict[str, object]) -> str:
    """A fresh row's identity string: its breakdown segment values, else its entity name (lower)."""
    seg = row.get("segment")
    if isinstance(seg, dict):
        return " ".join(str(v) for v in seg.values()).lower()
    return str(row.get("name") or "").lower()


def _row_matches_selector(row: dict[str, object], selector: dict[str, str]) -> bool:
    """True if EVERY selector key/value is present in the row's ``segment`` dict (full-value,
    case-insensitive — NOT token/substring overlap). A missing key, or a row with no ``segment``
    dict (e.g. an account-level pull), → no match — the safe-abstain direction."""
    seg = row.get("segment")
    if not isinstance(seg, dict):
        return False
    return all(str(seg.get(k, "")).lower() == v.lower() for k, v in selector.items())


def resolve_fresh_metric(
    rows: list[dict[str, object]],
    *,
    level: str,
    breakdowns: list[str],
    metric_name: str | None,
    selector: dict[str, str] | None = None,
) -> tuple[float | None, float | None, float | None]:
    """Resolve the named metric's fresh ``(value, purchases, spend)`` out of the fetched rows.

    Resolution order:

    - **Explicit ``selector``** (a parsed ``select: key=value,…`` tag) — the author named the exact
      breakdown slice this metric summarizes, so resolve against it *first*, by full-value
      (case-insensitive) match on each row's ``segment`` dict, never name-token overlap:

      - **zero** matches → ``(None, None, None)`` → ``could_not_audit`` (a renamed/vanished segment;
        the safe-abstain direction is preserved).
      - **exactly one** match → that row via :func:`_row_value` (keeps the roas-or-derived path).
      - **several** matches → :func:`_aggregate_value` over them — the **intentional, author-
        specified blend** (e.g. ``select: publisher_platform=instagram`` under a
        ``publisher_platform,platform_position`` breakdown blends all IG cells → the platform-level
        number). With an explicit selector, "several" is the author's coarser slice, NOT ambiguity.

    - **Account aggregate** (no selector, ``level == account``, no breakdown): the blended account
      ROAS — :func:`_aggregate_value` over the (single) row(s).
    - **Segment / entity scoped** (no selector; a breakdown, or a campaign/adset/ad level): the
      metric names a *specific* row (e.g. ``ig_roas`` → the ``instagram`` segment), matched by token
      overlap between the metric name and the row's segment/entity identity. **Exactly one** match
      resolves; zero or several → ``(None, None, None)`` so the verdict is ``could_not_audit`` and
      the claim is never silently confirmed (AGENTS.md: don't collapse missing into zeros). This
      "several → abstain" heuristic applies ONLY on the no-selector path.
    """
    if not rows:
        return None, None, None
    if selector:
        matches = [r for r in rows if _row_matches_selector(r, selector)]
        if not matches:
            return None, None, None  # vanished/renamed segment → could_not_audit
        if len(matches) == 1:
            return _row_value(matches[0])  # single cell — keep roas-or-derived path
        return _aggregate_value(matches)  # author-specified slice — blend the subset
    if not breakdowns and level == "account":
        return _aggregate_value(rows)
    tokens = _metric_identifier_tokens(metric_name)
    if not tokens:
        return None, None, None
    matches = [r for r in rows if any(tok in _row_identifier_text(r) for tok in tokens)]
    if len(matches) != 1:
        return None, None, None
    return _row_value(matches[0])


def _window_length_days(date_from: str | None, date_to: str | None, *, default: int = 30) -> int:
    """Inclusive day-span of a stored ``--date-from``/``--date-to`` window; ``default`` (30) when
    either bound is absent or unparseable."""
    if not (date_from and date_to):
        return default
    try:
        span = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1
    except ValueError:
        return default
    return span if span > 0 else default


def _fresh_verify_cmd(
    account_slug: str, level: str, breakdowns: list[str], date_from: str, date_to: str
) -> str:
    """The ``account_metrics …`` command that reproduces the FRESH value, for the logged ``➖``."""
    cmd = (
        f"account_metrics --account {account_slug} --level {level} "
        f"--date-from {date_from} --date-to {date_to}"
    )
    if breakdowns:
        cmd += f" --breakdown {','.join(breakdowns)}"
    return cmd


def run_vault_audit(
    *,
    text: str,
    account_slug: str,
    as_of: date,
    target_roas: float | None,
    pause_roas_floor: float | None,
    fetch_metrics,
    apply: bool,
) -> tuple[str, str | None, dict[str, int]]:
    """Orchestrate one audit pass over ``learnings.md`` text and return
    ``(report, new_text_or_None, counts)``.

    Pure given ``fetch_metrics`` — a callable ``(level, breakdowns, date_from, date_to) -> rows`` —
    so it is testable with a fake provider. For each auditable, account-scoped ``metric:`` claim it
    re-pulls a FRESH trailing window of the stored length ending ``as_of`` (current reality, NOT the
    historical window), resolves the metric, and classifies drift. ``new_text`` is ``None`` unless
    ``apply`` (report-only ⇒ zero file changes); when ``apply`` it is the surgically-edited markdown.
    """
    from .knowledge_provenance import (
        FreshSample,
        apply_entry_edits,
        audit_claim,
        parse_learnings,
        parse_verify_query,
        plan_edits,
        render_audit_report,
        select_auditable,
    )
    from .sync_api import resolve_date_window

    entries = parse_learnings(text)
    pairs = select_auditable(entries, account_slug=account_slug)

    outcomes = []
    fresh_verify_for: dict[int, str] = {}
    for entry, ev in pairs:
        pq = parse_verify_query(ev.verify_query)
        level = pq["level"] or "account"
        breakdowns = pq["breakdowns"]
        window_len = _window_length_days(pq["date_from"], pq["date_to"])
        date_from, date_to = resolve_date_window(as_of, lookback_days=window_len)
        rows = fetch_metrics(level, breakdowns, date_from, date_to)
        value, purchases, spend = resolve_fresh_metric(
            rows,
            level=level,
            breakdowns=breakdowns,
            metric_name=ev.metric_name,
            selector=ev.metric_selector,
        )
        fresh = FreshSample(
            value=value, purchases=purchases, spend=spend, window=f"{date_from}..{date_to}"
        )
        outcomes.append(
            audit_claim(
                entry,
                ev,
                fresh,
                target_roas=target_roas,
                pause_roas_floor=pause_roas_floor,
            )
        )
        fresh_verify_for[ev.lineno] = _fresh_verify_cmd(
            account_slug, level, breakdowns, date_from, date_to
        )

    report, counts = render_audit_report(
        outcomes, account_slug=account_slug, as_of=as_of.isoformat(), apply_mode=apply
    )
    new_text: str | None = None
    if apply:
        edits = plan_edits(
            outcomes,
            as_of=as_of.isoformat(),
            account_slug=account_slug,
            fresh_verify_for=fresh_verify_for,
        )
        new_text = apply_entry_edits(text, edits)
    return report, new_text, counts


def audit_vault_main() -> None:
    """Re-check the vault's data-backed ``metric:`` claims against fresh live data.

    Read-only against Meta; report-only by default. With ``--apply`` it edits ``learnings.md`` ONLY:
    a drifted claim gets a dated ``➖`` and its band lowered one level (refuted → 🔴 ``(contested)``);
    a confirmed claim just gets ``Verified:`` refreshed (this is how a ``lint-vault ⏳ re-verify``
    flag clears). It NEVER edits claim text and NEVER deletes an entry — a human decides deletion.
    """
    from .config import PROJECT_ROOT
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Re-check the knowledge vault's metric: claims against fresh live data; surface "
        "drift loudly, lower confidence, and log it — never silently overwrite or delete."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument(
        "--as-of", help="Anchor date YYYY-MM-DD for the fresh trailing window. Defaults to today."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write band/Verified/➖ edits to learnings.md. Without it, report-only (no file changes).",
    )
    parser.add_argument("--path", help="learnings.md to audit (default: knowledge/learnings.md).")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    learnings_path = Path(args.path) if args.path else PROJECT_ROOT / "knowledge" / "learnings.md"
    text = learnings_path.read_text(encoding="utf-8")

    policy = resolve_account(account_slug).action_policy or {}
    target_roas = _opt_float(policy.get("target_roas") or policy.get("scale_roas_floor"))
    pause_roas_floor = _opt_float(policy.get("pause_roas_floor"))

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)

    def fetch_metrics(level, breakdowns, date_from, date_to):
        if breakdowns:
            return fetch_breakdown_metrics(
                client, ad_account_id, breakdown=",".join(breakdowns),
                date_from=date_from, date_to=date_to, level=level,
            )
        return fetch_entity_metrics(
            client, ad_account_id, level=level, date_from=date_from, date_to=date_to
        )

    report, new_text, _counts = run_vault_audit(
        text=text,
        account_slug=account_slug,
        as_of=as_of,
        target_roas=target_roas,
        pause_roas_floor=pause_roas_floor,
        fetch_metrics=fetch_metrics,
        apply=args.apply,
    )
    print(report)
    if args.apply:
        if new_text is not None and new_text != text:
            learnings_path.write_text(new_text, encoding="utf-8")
            print(f"\naudit-vault: wrote updates to {learnings_path}")
        else:
            print("\naudit-vault: no drift to apply — file unchanged.")


def estimate_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Estimated audience size/reach for an ad set's current targeting.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--adset-id", required=True, help="Ad set to estimate.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    client = client_from_env(args.api_version)
    est = estimate_adset_audience(client, args.adset_id)
    print(f"Ad set {args.adset_id} — estimate_ready={est['estimate_ready']}")
    print(f"  estimated reachable (MAU): {est['mau_lower']} - {est['mau_upper']}")
    print(f"  estimated daily active (DAU): {est['estimate_dau']}")


def search_interests_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Search detailed-targeting interests by keyword.")
    parser.add_argument("--account", required=True, help="Account/company slug or name (for token resolution).")
    parser.add_argument("--query", required=True, help="Keyword to search, e.g. 'jewelry'.")
    parser.add_argument("--limit", type=int, default=25, help="Max results (default 25).")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    if _resolve_account_slug(args.account) is None:
        raise SystemExit("--account is required.")
    client = client_from_env(args.api_version)
    rows = search_interests(client, args.query, limit=args.limit)
    print(f"{len(rows)} interests for '{args.query}':")
    print(f"{'name':<40}{'size (lower-upper)':>28}  id")
    for r in rows:
        size = f"{r['audience_lower']}-{r['audience_upper']}" if r.get("audience_lower") is not None else "?"
        print(f"{str(r['name'])[:39]:<40}{size:>28}  {r['id']}")


def list_pixels_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="List the account's Meta pixels and custom conversions.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    pixels = list_account_pixels(client, ad_account_id)
    conversions = list_account_conversions(client, ad_account_id)
    print(f"{account_slug}: {len(pixels)} pixel(s)")
    for p in pixels:
        print(f"  {p.get('name')} ({p.get('id')}) last_fired={p.get('last_fired_time')} unavailable={p.get('is_unavailable')}")
    print(f"\n{len(conversions)} custom conversion(s)")
    for c in conversions:
        print(f"  {c.get('name')} ({c.get('id')}) type={c.get('custom_event_type')} archived={c.get('is_archived')}")


def copy_library_main() -> None:
    from .meta_api import client_from_env
    from .sync_api import resolve_date_window
    from .utils import ensure_dir

    parser = argparse.ArgumentParser(
        description="Pull top ROAS ads + their copy into a winning-copy swipe file in the knowledge base."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--date-from", help="Window start YYYY-MM-DD. Defaults to trailing 30 days.")
    parser.add_argument("--date-to", help="Window end YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--min-spend", type=float, default=50.0, help="Min spend in the window to qualify (default 50).")
    parser.add_argument("--top", type=int, default=20, help="How many top performers to keep (default 20).")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    date_from, date_to = resolve_date_window(date.today(), date_from=args.date_from, date_to=args.date_to)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    rows = build_copy_library(
        client, ad_account_id, date_from=date_from, date_to=date_to, min_spend=args.min_spend, top_n=args.top
    )
    md = render_copy_library_md(account_slug, rows, date_from=date_from, date_to=date_to)
    out_path = default_winning_copy_path(account_slug)
    ensure_dir(out_path.parent)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote winning-copy library for {account_slug} ({len(rows)} ads): {out_path}")
    for r in rows[:8]:
        primary = (r["primary_text"] or "")[:70]
        print(f"  ROAS {r['roas']:>5} | ${r['spend']:>6.0f} | {r['ad_name']}: {primary}")


def account_info_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Show account-level status, currency, spend, spend cap, funding.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    client = client_from_env(args.api_version)
    info = account_info(client, resolve_ad_account_id(account_slug))
    print(f"{account_slug} [{info['ad_account_id']}]")
    for k in ("name", "business_name", "status", "currency", "timezone", "amount_spent", "spend_cap", "balance", "funding_source", "disable_reason"):
        print(f"  {k:<16}: {info.get(k)}")


def intake_video_main() -> None:
    from .video_intake import list_inbox_videos, process_video

    parser = argparse.ArgumentParser(
        description="Transcribe a video locally (whisper) + sample frames into a creative brief the agent can use."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--file", help="A specific video file. If omitted, processes the account's inbox folder.")
    parser.add_argument("--model", default="base", help="faster-whisper model size (tiny/base/small/medium/large).")
    parser.add_argument("--frames", type=int, default=4, help="Number of sample frames to extract (default 4).")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")

    if args.file:
        videos = [Path(args.file)]
    else:
        videos = list_inbox_videos(account_slug)
        if not videos:
            raise SystemExit(
                f"No videos in inbox. Drop files in data/video_intake/{account_slug}/inbox/ or pass --file."
            )
    for video in videos:
        print(f"Processing {video.name} ...")
        brief = process_video(video, account_slug=account_slug, model_size=args.model, frame_count=args.frames)
        transcript = brief["transcript"]
        preview = (transcript[:300] + "…") if len(transcript) > 300 else transcript
        print(f"  duration: {brief['video']['duration_seconds']}s | frames: {len(brief['frames'])}")
        print(f"  transcript: {preview or '(empty)'}")
        print(f"  brief: {brief['brief_path']}")
    print("\nNext: the agent reads the brief + knowledge/ad_copy_best_practices.md, drafts 5 copy options "
          "and an ad-set pick, then runs propose-video-ad with the chosen copy.")


def upload_video_main() -> None:
    import time

    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Upload a video to the ad account and report its id + processing status.")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--file", required=True, help="Path to the video file.")
    parser.add_argument("--name", help="Optional name for the video in the media library.")
    parser.add_argument("--poll-seconds", type=int, default=60, help="How long to poll for 'ready' (default 60s).")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    resp = client.upload_video(ad_account_id, file_path=args.file, name=args.name)
    video_id = resp.get("id")
    print(f"Uploaded. video_id = {video_id}")
    waited = 0
    status = "?"
    while waited < args.poll_seconds:
        v = client.get_video(video_id, fields=["status"])
        status = (v.get("status") or {}).get("video_status", "?")
        if status in {"ready", "error"}:
            break
        time.sleep(5)
        waited += 5
    print(f"Processing status: {status}")
    if status != "ready":
        print("Video may still be processing; it's usable once status is 'ready'. Re-check with this command's id.")
    print(f"Use this in propose-video-ad: --video-id {video_id}")


def propose_video_ad_main() -> None:
    parser = argparse.ArgumentParser(
        description="Propose a video ad (created PAUSED) from an uploaded video_id + chosen copy."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--adset-id", required=True, help="Ad set to create the ad in.")
    parser.add_argument("--video-id", required=True, help="Uploaded video id (from upload-video).")
    parser.add_argument("--page-id", required=True, help="Facebook Page id behind the ad.")
    parser.add_argument("--name", required=True, help="Ad name.")
    parser.add_argument("--message", required=True, help="Primary text.")
    parser.add_argument("--link", required=True, help="Destination URL.")
    parser.add_argument("--title", help="Headline.")
    parser.add_argument("--description", help="Link description.")
    parser.add_argument("--cta", default="SHOP_NOW", help="Call-to-action type (default SHOP_NOW).")
    parser.add_argument("--image-hash", help="Thumbnail image hash (from upload-image), if required.")
    parser.add_argument("--date-from", help="Evidence window start (YYYY-MM-DD). Defaults to a 30-day trailing window.")
    parser.add_argument("--date-to", help="Evidence window end (YYYY-MM-DD). Defaults to the run date.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    ad_account_id = resolve_ad_account_id(account_slug)
    plan = build_video_ad_plan(
        ad_account_id, name=args.name, adset_id=args.adset_id, video_id=args.video_id, page_id=args.page_id,
        message=args.message, link=args.link, title=args.title, description=args.description,
        call_to_action_type=args.cta, image_hash=args.image_hash, account_slug=account_slug,
        date_from=args.date_from, date_to=args.date_to, run_date=run_date,
    )
    output_path = default_authoring_plan_path(account_slug, run_date, Path(args.reports_root))
    write_authoring_plan(plan, output_path)
    print(f"Wrote authoring plan ({plan['ops'][0]['note']}): {output_path}")
    print("Approve the op (status -> 'approved'), then apply-authoring --validate-only / --execute. Created PAUSED.")


def apply_authoring_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description="Dry-run, validate, or execute an approved authoring plan (create campaign/adset/ad/lookalike; all created PAUSED)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--plan-path", help="Override authoring plan path.")
    parser.add_argument("--results-path", help="Override results path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument("--validate-only", action="store_true", help="Send each approved op to Meta with validate_only.")
    parser.add_argument("--execute", action="store_true", help="Actually create. Without this, it's a dry run.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    plan_path = Path(args.plan_path) if args.plan_path else default_authoring_plan_path(account_slug, run_date, Path(args.reports_root))
    if not plan_path.exists():
        raise SystemExit(f"Authoring plan not found: {plan_path}.")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    client = client_from_env(args.api_version)
    results = apply_authoring_plan(plan, client, execute=args.execute, validate_only=args.validate_only)
    results_path = Path(args.results_path) if args.results_path else default_authoring_results_path(account_slug, run_date, Path(args.reports_root))
    write_authoring_results(plan=plan, results=results, output_path=results_path, execute=args.execute)
    ran = sum(1 for r in results if r.status in {"dry_run", "validated", "created"})
    blocked = sum(1 for r in results if r.status in {"blocked", "failed", "validation_failed"})
    mode = "validate-only" if args.validate_only else ("executed" if args.execute else "dry-run")
    print(f"Completed {mode} authoring for {account_slug}: {results_path}")
    print(f"Runnable approved ops: {ran}; blocked or failed: {blocked}")
    for r in results:
        if r.created_id or r.reason:
            print(f"  {r.op_id}: {r.status} {('-> ' + r.created_id) if r.created_id else ''} {r.reason or ''}")


def propose_duplicate_ad_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(description="Propose duplicating an existing ad's creative into a target ad set (created PAUSED).")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--source-ad-id", required=True, help="Ad to copy the creative from.")
    parser.add_argument("--target-adset-id", required=True, help="Ad set to create the new ad in.")
    parser.add_argument("--name", help="Name for the new ad. Defaults to '<source name> (copy)'.")
    parser.add_argument("--date-from", help="Evidence window start (YYYY-MM-DD) for the source ad's metric. Defaults to a 30-day trailing window.")
    parser.add_argument("--date-to", help="Evidence window end (YYYY-MM-DD). Defaults to the run date.")
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
    plan = build_duplicate_ad_plan(
        client, ad_account_id, source_ad_id=args.source_ad_id,
        target_adset_id=args.target_adset_id, name=args.name, account_slug=account_slug,
        date_from=args.date_from, date_to=args.date_to, run_date=run_date,
    )
    output_path = default_authoring_plan_path(account_slug, run_date, Path(args.reports_root))
    write_authoring_plan(plan, output_path)
    print(f"Wrote authoring plan ({plan['ops'][0]['note']}): {output_path}")
    print("Approve the op (status -> 'approved'), then apply-authoring --validate-only / --execute. Created PAUSED.")


def propose_lookalike_main() -> None:
    parser = argparse.ArgumentParser(description="Propose creating a lookalike audience from a seed (no writes).")
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--name", required=True, help="Name for the new lookalike audience.")
    parser.add_argument("--origin-audience-id", required=True, help="Seed custom audience id.")
    parser.add_argument("--country", default="US", help="Country code for the lookalike (default US).")
    parser.add_argument("--ratio", type=float, default=0.01, help="Lookalike ratio 0.01-0.20 (default 0.01 = 1%%).")
    parser.add_argument("--date-from", help="Evidence window start (YYYY-MM-DD) labeling the seed-basis evidence. Defaults to a 30-day trailing window.")
    parser.add_argument("--date-to", help="Evidence window end (YYYY-MM-DD). Defaults to the run date.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    ad_account_id = resolve_ad_account_id(account_slug)
    plan = build_lookalike_plan(
        ad_account_id, name=args.name, origin_audience_id=args.origin_audience_id,
        country=args.country, ratio=args.ratio, account_slug=account_slug,
        date_from=args.date_from, date_to=args.date_to, run_date=run_date,
    )
    output_path = default_authoring_plan_path(account_slug, run_date, Path(args.reports_root))
    write_authoring_plan(plan, output_path)
    print(f"Wrote authoring plan ({plan['ops'][0]['note']}): {output_path}")
    print("Approve the op (status -> 'approved'), then apply-authoring --validate-only / --execute.")


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
        date_from=date_from, date_to=date_to, run_date=run_date,
    )
    output_path = Path(args.output_path) if args.output_path else default_ops_plan_path(account_slug, run_date, Path(args.reports_root))
    write_plan(plan, output_path)
    print(f"Wrote pause-ads plan for {account_slug} ({len(plan['ops'])} ads): {output_path}")
    for op in plan["ops"]:
        print(f"  {op['name']} — {op['note']}")
    print("Approve by setting an op's status to 'approved', then run apply-ops --validate-only / --execute.")


def propose_budget_main() -> None:
    from .meta_api import client_from_env

    parser = argparse.ArgumentParser(
        description=(
            "Propose a CBO-aware daily-budget change (increase OR decrease) for one ad set or "
            "campaign, grounded + reviewed (no writes). Under CBO, redirects to a campaign-level op."
        )
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--adset-id", help="Ad set to set the daily budget on (redirects under CBO).")
    target.add_argument("--campaign-id", help="Campaign to set the daily budget on (CBO budget level).")
    parser.add_argument(
        "--daily-budget-cents", type=int, required=True,
        help="Target daily budget in account minor units (cents). May be above OR below the current.",
    )
    parser.add_argument("--max-increase-percent", type=float, help="Override the increase cap (default 20%%).")
    parser.add_argument("--max-decrease-percent", type=float, help="Override the decrease cap (default config/per-account).")
    parser.add_argument("--date-from", help="Evidence-window start. Defaults to a 30-day trailing window.")
    parser.add_argument("--date-to", help="Evidence-window end. Defaults to the run date / today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--output-path", help="Override ops plan path.")
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)
    plan = build_budget_plan(
        client, ad_account_id, account_slug=account_slug,
        adset_id=args.adset_id, campaign_id=args.campaign_id,
        new_daily_budget_cents=args.daily_budget_cents,
        max_increase_percent=args.max_increase_percent,
        max_decrease_percent=args.max_decrease_percent,
        date_from=args.date_from, date_to=args.date_to, run_date=run_date,
    )
    output_path = Path(args.output_path) if args.output_path else default_ops_plan_path(account_slug, run_date, Path(args.reports_root))
    write_plan(plan, output_path)
    print(f"Wrote budget plan for {account_slug} ({len(plan['ops'])} op(s)): {output_path}")
    for op in plan["ops"]:
        flags = " [CBO redirect]" if op.get("cbo_detected") else ""
        band = (op.get("confidence") or {}).get("band", "?")
        print(f"  {op['level']}:{op['id']} — {op.get('note')} (band: {band}){flags}")
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
    parser.add_argument("--date-from", help="Evidence-window start. Defaults to a 30-day trailing window.")
    parser.add_argument("--date-to", help="Evidence-window end. Defaults to the run date / today.")
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
        date_from=args.date_from, date_to=args.date_to, run_date=run_date,
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


def experiment_main() -> None:
    from datetime import date as _date

    from .experiment import define_experiment, list_experiments, load_experiment, read_experiment

    parser = argparse.ArgumentParser(prog="experiment", description="A/B experiment harness: define a test and read it out with significance.")
    sub = parser.add_subparsers(dest="action", required=True)
    pd = sub.add_parser("define", help="Define an A/B experiment (control vs variant, one variable).")
    pd.add_argument("--account", required=True)
    pd.add_argument("--id", required=True, help="Short experiment id (slug).")
    pd.add_argument("--hypothesis", required=True)
    pd.add_argument("--variable", required=True, help="The single thing changed (e.g. 'enhance_cta on vs off').")
    pd.add_argument("--level", choices=["ad", "adset", "campaign"], default="ad")
    pd.add_argument("--control", nargs="+", required=True, help="Control entity id(s).")
    pd.add_argument("--variant", nargs="+", required=True, help="Variant entity id(s).")
    pd.add_argument("--metric", default="roas")
    pd.add_argument("--days", type=int, default=14)
    pd.add_argument("--start", help="Start date YYYY-MM-DD (default today).")
    pd.add_argument("--notes", default="")
    pr = sub.add_parser("readout", help="Pull both arms and compare with a significance check.")
    pr.add_argument("--account", required=True)
    pr.add_argument("--id", required=True)
    pr.add_argument("--as-of", help="Treat this date as 'today'.")
    pr.add_argument("--min-conversions", type=int, default=25)
    pr.add_argument("--api-version")
    pr.add_argument("--json-output-path", help="Write readout result as JSON to this path (in addition to stdout).")
    pl = sub.add_parser("list", help="List experiments for an account.")
    pl.add_argument("--account", required=True)
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")

    if args.action == "define":
        path = define_experiment(
            account=account_slug, exp_id=args.id, hypothesis=args.hypothesis, variable=args.variable,
            level=args.level, control_ids=args.control, variant_ids=args.variant, metric=args.metric,
            start_date=args.start or _date.today().isoformat(), planned_days=args.days, notes=args.notes,
            created=_date.today().isoformat(),
        )
        print(f"Defined experiment '{args.id}': {path}")
        print(f"  {args.variable}  | control {args.control} vs variant {args.variant} | {args.days}d")
    elif args.action == "list":
        items = list_experiments(account_slug)
        print(f"{len(items)} experiment(s) for {account_slug}:")
        for e in items:
            print(f"  [{e.status}] {e.id} — {e.variable} (since {e.start_date}, {e.planned_days}d)")
    elif args.action == "readout":
        from .meta_api import client_from_env
        exp = load_experiment(account_slug, args.id)
        client = client_from_env(args.api_version)
        ad_account_id = resolve_ad_account_id(account_slug)
        as_of = _date.fromisoformat(args.as_of) if args.as_of else _date.today()
        r = read_experiment(client, ad_account_id, exp, as_of=as_of, min_conversions=args.min_conversions)
        c, v = r["control"], r["variant"]
        print(f"Experiment '{exp.id}' — {exp.variable}  | window {r['window']} (level {exp.level})")
        print(f"  hypothesis: {exp.hypothesis}")
        print(f"  {'':<10}{'spend':>9}{'value':>9}{'ROAS':>7}{'purch':>7}{'impr':>9}{'CVR':>9}")
        for label, a in (("control", c), ("variant", v)):
            print(f"  {label:<10}{a['spend']:>9.0f}{a['purchase_value']:>9.0f}"
                  f"{(a['roas'] if a['roas'] is not None else 0):>7.2f}{a['purchases']:>7}{a['impressions']:>9}"
                  f"{(a['cvr'] if a['cvr'] is not None else 0):>9.4f}")
        if r["roas_lift_pct"] is not None:
            print(f"  ROAS lift (variant vs control): {r['roas_lift_pct']:+.1f}%  | conversion-rate p-value: {r['conversion_rate_pvalue']}")
        print(f"\n  VERDICT: {r['verdict']}")
        print(f"  caveat: {r['caveat']}")
        if args.json_output_path:
            out = Path(args.json_output_path)
            ensure_dir(out.parent)
            write_json(out, r)
            print(f"Wrote readout JSON: {out}")


def watch_main() -> None:
    from datetime import date as _date

    from .config import (
        DEFAULT_DB_PATH,
        EARLY_LIFE_DECISION_AGE,
        EARLY_LIFE_MAX_AGE,
        EARLY_LIFE_MIN_ANALOGS,
        EARLY_LIFE_RECOVERY_HORIZON,
        EARLY_LIFE_RECOVERY_RATE,
        EARLY_LIFE_STRONG_ANALOGS,
    )
    from .early_triage import DuckDBHistoryProvider
    from .followups import EARLY_LIFE_MARKER, add_followup_if_absent, iter_followups, mark_done
    from .meta_api import client_from_env
    from .monitor import build_watch_report, default_watch_report_path, load_watchlist, save_watchlist

    parser = argparse.ArgumentParser(
        description="Read-only runaway/outlier scanner: flag ads spending while underperforming (protects new/changed ads)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--as-of", help="Treat this date as 'today' (YYYY-MM-DD).")
    parser.add_argument("--window-days", type=int, default=7, help="Judgment window (default 7).")
    parser.add_argument("--recent-days", type=int, default=3, help="Recent window for spend-velocity (default 3).")
    parser.add_argument("--min-spend", type=float, default=100.0, help="Significance floor (default 100).")
    parser.add_argument("--grace-days", type=int, default=5, help="Protect ads created/changed within N days (default 5).")
    parser.add_argument("--roas-floor", type=float, help="Override pause floor (default from account policy).")
    parser.add_argument("--roas-target", type=float, help="Override target ROAS (default from account policy).")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    parser.add_argument("--api-version", help="Override the pinned Meta Graph API version.")
    parser.add_argument(
        "--no-early-life", dest="early_life", action="store_false",
        help="Disable the early-life triage of brand-new struggling ads (on by default).",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="DuckDB path for ad histories (early-life triage).")
    parser.add_argument("--early-life-max-age", type=int, default=EARLY_LIFE_MAX_AGE,
                        help=f"Triage applies only to ads this young (default {EARLY_LIFE_MAX_AGE}).")
    parser.add_argument("--early-life-decision-age", type=int, default=EARLY_LIFE_DECISION_AGE,
                        help=f"Force a keep/kill by this age (default {EARLY_LIFE_DECISION_AGE}).")
    parser.add_argument("--early-life-recovery-horizon", type=int, default=EARLY_LIFE_RECOVERY_HORIZON,
                        help=f"An analog must have lived this long to judge recovery (default {EARLY_LIFE_RECOVERY_HORIZON}).")
    parser.add_argument("--early-life-min-analogs", type=int, default=EARLY_LIFE_MIN_ANALOGS,
                        help=f"Fewer comparable analogs than this → abstain & keep (default {EARLY_LIFE_MIN_ANALOGS}).")
    parser.add_argument("--early-life-strong-analogs", type=int, default=EARLY_LIFE_STRONG_ANALOGS,
                        help=f"Analog count at/above which the correlational band reads medium (default {EARLY_LIFE_STRONG_ANALOGS}).")
    parser.add_argument("--early-life-recovery-rate", type=float, default=EARLY_LIFE_RECOVERY_RATE,
                        help=f"Keep only if the analog recovery rate clears this (default {EARLY_LIFE_RECOVERY_RATE}).")
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    as_of = _date.fromisoformat(args.as_of) if args.as_of else _date.today()
    run_date = args.run_date or _date.today().isoformat()
    reports_root = Path(args.reports_root)

    client = client_from_env(args.api_version)
    ad_account_id = resolve_ad_account_id(account_slug)

    history_provider = DuckDBHistoryProvider(Path(args.db_path)) if args.early_life else None
    open_followups = (
        [f for f in iter_followups(account_slug) if EARLY_LIFE_MARKER in f.path.stem]
        if args.early_life else []
    )
    report = build_watch_report(
        client, ad_account_id, account_slug=account_slug, as_of=as_of,
        window_days=args.window_days, recent_days=args.recent_days, min_spend=args.min_spend,
        grace_days=args.grace_days, roas_floor=args.roas_floor, roas_target=args.roas_target,
        prior_watchlist=load_watchlist(account_slug, reports_root),
        early_life=args.early_life, history_provider=history_provider, open_followups=open_followups,
        early_life_max_age=args.early_life_max_age, early_life_decision_age=args.early_life_decision_age,
        early_life_recovery_horizon=args.early_life_recovery_horizon,
        early_life_min_analogs=args.early_life_min_analogs,
        early_life_strong_analogs=args.early_life_strong_analogs,
        early_life_recovery_rate=args.early_life_recovery_rate,
    )
    write_plan(report, default_watch_report_path(account_slug, run_date, reports_root))
    save_watchlist(account_slug, report["watchlist"], reports_root)

    # Apply the follow-up file/close actions the (filesystem-free) scan returned. Filing is deduped by
    # the deterministic per-ad slug; closing an already-archived follow-up is a no-op (missing_ok).
    filed = closed = 0
    for action in report.get("followup_actions", []):
        if action["action"] == "file":
            _, created = add_followup_if_absent(
                account=action["account"], slug=action["slug"], title=action["title"],
                due=action["due"], note=action["note"], created=as_of.isoformat(),
                marker=action.get("marker", EARLY_LIFE_MARKER), ad_id=action.get("ad_id", ""),
            )
            filed += int(created)
        elif action["action"] == "close":
            if mark_done(account=action["account"], task_id=action["task_id"],
                         completed=as_of.isoformat(), missing_ok=True) is not None:
                closed += 1

    rows = report["rows"]
    early = [r for r in rows if r.get("early_life")]
    urgent = [r for r in rows if r["classification"] == "urgent" and not r.get("early_life")]
    under = [r for r in rows if r["classification"] == "underperforming" and not r.get("early_life")]
    watch = [r for r in rows if r["classification"] == "watch" and not r.get("early_life")]
    early_pause = [r for r in early if r["classification"] == "pause_candidate"]
    early_keep = [r for r in early if r["classification"] != "pause_candidate"]
    p = report["params"]
    print(f"{account_slug} watch — window {report['window']} | floor {p['roas_floor']} target {p['roas_target']} "
          f"| min-spend ${p['min_spend']:.0f} | grace {p['grace_days']}d")
    print(f"URGENT {len(urgent)} · underperforming {len(under)} · watch(protected/learning) {len(watch)}")
    if p.get("early_life"):
        print(f"early-life: kept-on-probation {len(early_keep)} · early-pause-candidate {len(early_pause)} "
              f"(follow-ups filed {filed}, closed {closed})")
    print()

    def line(r):
        flags = []
        if r["accelerating"]:
            flags.append("ACCEL")
        if r["times_flagged"] >= 2:
            flags.append(f"{r['times_flagged']}x running")
        tag = (" [" + ", ".join(flags) + "]") if flags else ""
        roas = f"{r['roas']:.2f}" if r["roas"] is not None else "0.00"
        if r.get("early_life"):
            basis = r.get("analog_basis") or {}
            age_disp = f"age {r.get('age')}d (early-life)"
            tag = f" [{basis.get('analogs', 0)} analogs at age {basis.get('age')}, {basis.get('recovered', 0)} recovered]"
        else:
            age_disp = f"age {r['days_since_change']}d"
        print(f"  {r['classification'].upper():<16} {str(r['ad_name'])[:26]:<27} ROAS {roas} | ${r['spend']:.0f} "
              f"| ${(r.get('dollars_at_risk') or 0.0):.0f} at risk | {age_disp}{tag}")
        for reason in r["reasons"]:
            print(f"        - {reason}")

    for r in urgent + early_pause + under + early_keep + watch:
        line(r)
    if not rows:
        print("  Nothing flagged. (No delivering ad is past the significance floor and below target.)")
    print(f"\nReport: {default_watch_report_path(account_slug, run_date, reports_root)}")
    print("Flag-only — review the urgent / early-pause ones case-by-case, then pause via propose-pause-ads / apply-ops if warranted.")


def propose_creative_features_main() -> None:
    parser = argparse.ArgumentParser(
        description="Propose setting creative enhancement features on an ad (default: additive ON, Text Improvements OFF)."
    )
    parser.add_argument("--account", required=True, help="Account/company slug or name.")
    parser.add_argument("--ad-id", required=True, help="Ad to set creative features on.")
    parser.add_argument("--opt-in", nargs="*", help="Features to OPT_IN (default: the account additive set).")
    parser.add_argument("--opt-out", nargs="*", help="Features to OPT_OUT (default: text_optimizations, replace_media_text).")
    parser.add_argument("--run-date", help="Folder date under reports/<account>/. Defaults to today.")
    parser.add_argument("--reports-root", default=str(DEFAULT_REPORTS_ROOT))
    args = parser.parse_args()

    account_slug = _resolve_account_slug(args.account)
    if account_slug is None:
        raise SystemExit("--account is required.")
    run_date = args.run_date or date.today().isoformat()
    opt_in = args.opt_in if args.opt_in is not None else DEFAULT_OPT_IN_FEATURES
    opt_out = args.opt_out if args.opt_out is not None else DEFAULT_OPT_OUT_FEATURES
    plan = {
        "schema_version": 1,
        "plan_type": "ops",
        "intent": "set_creative_features",
        "account_slug": account_slug,
        "approval_instructions": "Set the op's status to 'approved', then apply-ops --validate-only / --execute.",
        "guardrails": {"requires_explicit_approval": True},
        "ops": [
            {
                "op_id": f"creative_features_{args.ad_id}",
                "op": "set_creative_features",
                "level": "ad",
                "id": args.ad_id,
                "params": {"opt_in": opt_in, "opt_out": opt_out},
                "status": "proposed",
                "note": f"OPT_IN {opt_in}; OPT_OUT {opt_out}",
            }
        ],
    }
    output_path = default_ops_plan_path(account_slug, run_date, Path(args.reports_root))
    write_plan(plan, output_path)
    print(f"Wrote creative-features ops plan for ad {args.ad_id}: {output_path}")
    print(f"  OPT_IN:  {opt_in}")
    print(f"  OPT_OUT: {opt_out}")
    print("Approve the op (status -> 'approved'), then apply-ops --validate-only / --execute.")


def followups_main() -> None:
    from datetime import date as _date

    from .followups import add_followup, due_followups, iter_followups, mark_done

    parser = argparse.ArgumentParser(
        prog="followups", description="Due-date-aware account follow-up tasks (separate from agent tickets)."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p_due = sub.add_parser("due", help="List ONLY tasks due/overdue for an account (the check-in entry point).")
    p_due.add_argument("--account", required=True)
    p_due.add_argument("--as-of", help="Treat this date as 'today' (YYYY-MM-DD).")
    p_list = sub.add_parser("list", help="List open follow-ups (with --all, include done).")
    p_list.add_argument("--account", required=True)
    p_list.add_argument("--all", action="store_true")
    p_add = sub.add_parser("add", help="Add a follow-up task.")
    p_add.add_argument("--account", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--due", required=True, help="Due date YYYY-MM-DD.")
    p_add.add_argument("--note", default="", help="Body: what to do, why, how.")
    p_done = sub.add_parser("done", help="Mark a follow-up done (archives it).")
    p_done.add_argument("--account", required=True)
    p_done.add_argument("task_id", help="The task id (filename stem) shown by `due`/`list`.")
    args = parser.parse_args()

    if args.action == "due":
        as_of = _date.fromisoformat(args.as_of) if args.as_of else _date.today()
        due = due_followups(args.account, as_of=as_of)
        if not due:
            print(f"No follow-ups due for {args.account} as of {as_of}.")
            return
        print(f"{len(due)} follow-up(s) due for {args.account} as of {as_of}:")
        for f in due:
            overdue = " (OVERDUE)" if f.due and f.due < as_of else ""
            print(f"  [{f.due}]{overdue} {f.title}  — id: {f.task_id}")
        print("\nRead a task's body with its file, act on it, then `followups done --account "
              f"{args.account} <id>`.")
    elif args.action == "list":
        items = iter_followups(args.account, include_done=args.all)
        print(f"{len(items)} follow-up(s) for {args.account}{' (incl. done)' if args.all else ' (open)'}:")
        for f in items:
            print(f"  [{f.due or 'no-date'}] {f.status:<6} {f.title}  — id: {f.task_id}")
    elif args.action == "add":
        path = add_followup(
            account=args.account, title=args.title, due=args.due, note=args.note,
            created=_date.today().isoformat(),
        )
        print(f"Added follow-up (due {args.due}): {path}")
    elif args.action == "done":
        dest = mark_done(account=args.account, task_id=args.task_id, completed=_date.today().isoformat())
        print(f"Marked done: {dest}")


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
    parser.add_argument(
        "--no-review",
        action="store_true",
        help=(
            "Skip the adversarial review gate (escape hatch). By default the gate corrects or drops "
            "recommendations that cannot survive a second-opinion challenge before they reach the brief."
        ),
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
        review_enabled=not args.no_review,
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
