"""Module execution helper."""

from __future__ import annotations

import sys

from .cli import build_meta_report_main, ingest_meta_exports_main


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        raise SystemExit(
            "Usage: python -m meta_ads_analysis <ingest|report> [args]\n"
            "Example: python -m meta_ads_analysis ingest --run-date 2026-04-21"
        )

    command = sys.argv[1].replace("-", "_")
    sys.argv = [sys.argv[0], *sys.argv[2:]]

    if command in {"ingest", "ingest_meta_exports"}:
        ingest_meta_exports_main()
        return
    if command in {"report", "build_meta_report"}:
        build_meta_report_main()
        return

    raise SystemExit(
        f"Unknown command: {command}. Use `ingest` or `report`."
    )


if __name__ == "__main__":
    main()
