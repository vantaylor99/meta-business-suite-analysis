# Meta Ads Analysis Repo

This repository gives you a fast, CSV-first workflow for analyzing Meta ad performance without building the Meta API integration first.

The workflow is:

1. Export reports from Meta Ads Manager.
2. Drop the CSVs into `data/raw/meta_ads/<run_date>/`.
3. Run `ingest_meta_exports`.
4. Run `build_meta_report`.
5. Review the generated report in `reports/<run_date>/`.

The implementation is intentionally API-ready. When you later add a direct Marketing API pull, it should map into the same normalized schema instead of changing the downstream analysis.

## Install

```powershell
pip install -e .[dev]
```

## Expected Input Files

Place exports in:

```text
data/raw/meta_ads/<run_date>/
```

The `<run_date>` folder should usually be a date like `2026-04-21`.

### Required

- `performance_daily.csv`
  - Export at `Ads` level
  - Add a `Day` breakdown
  - Include IDs and names for campaign, ad set, and ad
  - Include spend, impressions, reach, frequency, clicks, link or outbound clicks, CTR, CPC, CPM, results, cost per result, purchase counts, purchase value, and ROAS if available

### Recommended

- `video_daily.csv`
  - Export at `Ads` level
  - Add a `Day` breakdown
  - Include `3-second video plays` and `ThruPlays`

### Optional

- `creative_lookup.csv`
  - Include `Ad ID`
  - Optional fields: creative type, primary text, headline, launch date, preview link, post link

## Suggested Ads Manager Export Setup

### `performance_daily.csv`

- Level: `Ads`
- Breakdown: `Time > Day`
- Suggested columns:
  - `Campaign ID`, `Campaign name`
  - `Ad set ID`, `Ad set name`
  - `Ad ID`, `Ad name`
  - `Amount spent`
  - `Impressions`, `Reach`, `Frequency`
  - `Clicks`
  - `Inline link clicks` and/or `Outbound clicks`
  - `CTR`, `CPC`, `CPM`
  - `Results`, `Cost per result`
  - `Purchases`
  - `Website purchases conversion value`
  - `Purchase ROAS (return on ad spend)` or website purchase ROAS equivalent

### `video_daily.csv`

- Level: `Ads`
- Breakdown: `Time > Day`
- Suggested columns:
  - `Ad ID`, `Ad name`
  - `Day`
  - `Impressions`
  - `3-second video plays`
  - `ThruPlays`

## Commands

### Ingest and normalize exports

```powershell
ingest_meta_exports --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis ingest --run-date 2026-04-21
```

This reads from:

```text
data/raw/meta_ads/2026-04-21/
```

and writes:

- DuckDB database: `data/normalized/meta_ads.duckdb`
- CSV snapshots: `data/normalized/meta_ads/2026-04-21/`
- Ingestion summary: `data/normalized/meta_ads/2026-04-21/ingestion_summary.json`

### Build the report

```powershell
build_meta_report --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis report --run-date 2026-04-21
```

This writes:

- Markdown report: `reports/2026-04-21/meta_ads_report.md`
- JSON summary: `reports/2026-04-21/meta_ads_report.json`

## What The Report Covers

- Executive summary
- Budget waste findings
- Fatigue and staleness findings
- Hook-rate and creative-performance findings
- Scaling candidates
- Tracking and measurement concerns
- Recommended next-7-day actions

## Notes

- Raw exports and generated reports are ignored by git by default because they usually contain sensitive business data.
- ROAS quality depends on the measurement setup behind the ad account. If purchase value tracking is weak, the report will say so instead of pretending the numbers are clean.
- The current version is read-only and manual-export based. That is deliberate for v1.

See [AGENTS.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/AGENTS.md) for the analysis contract that Codex or another agent should follow when reviewing the generated data.

For step-by-step export instructions inside Meta, see [docs/META_EXPORT_GUIDE.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/docs/META_EXPORT_GUIDE.md).
