"""Module execution helper."""

from __future__ import annotations

import sys

from .cli import (
    apply_disable_advantage_main,
    apply_meta_actions_main,
    apply_ops_main,
    apply_renames_main,
    apply_rotation_main,
    account_info_main,
    apply_authoring_main,
    build_meta_report_main,
    copy_library_main,
    diagnose_main,
    estimate_main,
    ingest_meta_exports_main,
    inspect_main,
    intake_video_main,
    list_audiences_main,
    list_pixels_main,
    metrics_main,
    search_interests_main,
    operator_brief_main,
    propose_disable_advantage_main,
    propose_duplicate_ad_main,
    propose_enable_ads_main,
    propose_lookalike_main,
    propose_meta_actions_main,
    propose_pause_ads_main,
    propose_renames_main,
    propose_rotation_main,
    propose_video_ad_main,
    sync_meta_api_main,
    upload_video_main,
)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(
            "Usage: python -m meta_ads_analysis <ingest|report|sync-api|inspect|metrics|diagnose|"
            "list-audiences|account-info|estimate|search-interests|list-pixels|copy-library|propose-actions|"
            "apply-actions|propose-rotation|apply-rotation|propose-disable-advantage|"
            "apply-disable-advantage|propose-renames|apply-renames|propose-enable-ads|propose-pause-ads|"
            "apply-ops|propose-duplicate-ad|propose-lookalike|apply-authoring|intake-video|"
            "upload-video|propose-video-ad|operator-brief> [args]\n"
            "Example: python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-21"
        )
        return

    command = sys.argv[1].replace("-", "_")
    sys.argv = [sys.argv[0], *sys.argv[2:]]

    if command in {"ingest", "ingest_meta_exports"}:
        ingest_meta_exports_main()
        return
    if command in {"report", "build_meta_report"}:
        build_meta_report_main()
        return
    if command in {"sync_api", "sync", "sync_meta_api"}:
        sync_meta_api_main()
        return
    if command in {"propose_actions", "propose_meta_actions"}:
        propose_meta_actions_main()
        return
    if command in {"apply_actions", "apply_meta_actions"}:
        apply_meta_actions_main()
        return
    if command in {"propose_rotation", "propose_audience_rotation"}:
        propose_rotation_main()
        return
    if command in {"apply_rotation", "apply_audience_rotation"}:
        apply_rotation_main()
        return
    if command in {"inspect", "snapshot"}:
        inspect_main()
        return
    if command in {"metrics", "performance"}:
        metrics_main()
        return
    if command in {"diagnose", "scan_issues"}:
        diagnose_main()
        return
    if command in {"list_audiences", "audiences"}:
        list_audiences_main()
        return
    if command in {"account_info", "account"}:
        account_info_main()
        return
    if command in {"estimate", "delivery_estimate"}:
        estimate_main()
        return
    if command in {"search_interests", "interests"}:
        search_interests_main()
        return
    if command in {"list_pixels", "pixels"}:
        list_pixels_main()
        return
    if command in {"copy_library", "winning_copy"}:
        copy_library_main()
        return
    if command in {"apply_authoring", "authoring"}:
        apply_authoring_main()
        return
    if command in {"propose_duplicate_ad", "duplicate_ad"}:
        propose_duplicate_ad_main()
        return
    if command in {"intake_video", "intake"}:
        intake_video_main()
        return
    if command in {"upload_video"}:
        upload_video_main()
        return
    if command in {"propose_video_ad", "video_ad"}:
        propose_video_ad_main()
        return
    if command in {"propose_lookalike", "lookalike"}:
        propose_lookalike_main()
        return
    if command in {"propose_enable_ads", "enable_ads"}:
        propose_enable_ads_main()
        return
    if command in {"propose_pause_ads", "pause_ads"}:
        propose_pause_ads_main()
        return
    if command in {"apply_ops", "ops"}:
        apply_ops_main()
        return
    if command in {"propose_disable_advantage", "propose_disable_aa"}:
        propose_disable_advantage_main()
        return
    if command in {"apply_disable_advantage", "apply_disable_aa"}:
        apply_disable_advantage_main()
        return
    if command in {"propose_renames", "propose_adset_renames"}:
        propose_renames_main()
        return
    if command in {"apply_renames", "apply_adset_renames"}:
        apply_renames_main()
        return
    if command in {"operator_brief", "brief"}:
        operator_brief_main()
        return

    raise SystemExit(
        f"Unknown command: {command}. Use `ingest`, `report`, `sync-api`, `inspect`, `metrics`, "
        "`diagnose`, `list-audiences`, `account-info`, `estimate`, `search-interests`, `list-pixels`, "
        "`copy-library`, "
        "`propose-actions`, `apply-actions`, `propose-rotation`, `apply-rotation`, "
        "`propose-disable-advantage`, `apply-disable-advantage`, `propose-renames`, `apply-renames`, "
        "`propose-enable-ads`, `propose-pause-ads`, `apply-ops`, `propose-duplicate-ad`, "
        "`propose-lookalike`, `apply-authoring`, `intake-video`, `upload-video`, "
        "`propose-video-ad`, or `operator-brief`."
    )


if __name__ == "__main__":
    main()
