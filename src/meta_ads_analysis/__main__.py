"""Module execution helper."""

from __future__ import annotations

import sys

from .cli import (
    apply_meta_actions_main,
    build_meta_report_main,
    ingest_meta_exports_main,
    operator_brief_main,
    propose_meta_actions_main,
    sync_meta_cli_main,
    sync_meta_api_main,
)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(
            "Usage: python -m meta_ads_analysis <ingest|report|sync-api|sync-cli|propose-actions|apply-actions|operator-brief> [args]\n"
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
    if command in {"sync_cli", "sync_meta_cli"}:
        sync_meta_cli_main()
        return
    if command in {"propose_actions", "propose_meta_actions"}:
        propose_meta_actions_main()
        return
    if command in {"apply_actions", "apply_meta_actions"}:
        apply_meta_actions_main()
        return
    if command in {"operator_brief", "brief"}:
        operator_brief_main()
        return

    raise SystemExit(
        f"Unknown command: {command}. Use `ingest`, `report`, `sync-api`, `sync-cli`, `propose-actions`, `apply-actions`, or `operator-brief`."
    )


if __name__ == "__main__":
    main()
