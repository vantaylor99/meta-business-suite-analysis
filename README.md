# Meta Ads Analysis Repo

This repository gives you a fast workflow for analyzing Meta ad performance from either manual CSV exports or a direct Meta Marketing API sync.

The workflow is:

1. Export reports from Meta Ads Manager.
2. Drop the CSVs into `data/raw/meta_ads/<account_slug>/<run_date>/`.
3. Run `ingest_meta_exports`.
4. Run `build_meta_report`.
5. Review the generated report in `reports/<account_slug>/<run_date>/`.

The implementation keeps one normalized reporting pipeline and supports two input paths:

- manual Ads Manager exports
- direct API sync into the same raw CSV contract

## Install

```powershell
pip install -e .[dev]
```

## API Sync

To pull data directly from Meta for one configured account:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22
```

By default this:

- uses `META_ACCESS_TOKEN`
- looks up the account in `config/meta_ads_accounts.json`
- reads the account's measurement focus from `config/meta_ads_accounts.json`
- fetches a trailing 30-day daily window ending on `--run-date`
- writes the raw CSV contract into `data/raw/meta_ads/<account_slug>/<run_date>/`
- then runs ingest and report automatically

To stop after raw files only:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22 --raw-only
```

For setup details, see [docs/META_API_SETUP.md](/C:/Van%20%26%20Kim%20Venture%20Strategy/meta-business-suite-analysis/docs/META_API_SETUP.md).

## Expected Input Files

Place exports in:

```text
data/raw/meta_ads/<account_slug>/<run_date>/
```

Use a readable slug for `<account_slug>`, such as `pollen_sense` or `divine_designs`.

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

### Sync data from the Meta API

```powershell
sync_meta_api --account pollen_sense --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-21
```

Optional overrides:

- `--date-from 2026-04-01`
- `--date-to 2026-04-21`
- `--raw-only`
- `--db-path data/normalized/meta_ads.duckdb`
- `--api-version v22.0`

### Ingest and normalize exports

```powershell
ingest_meta_exports --account pollen_sense --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis ingest --account pollen_sense --run-date 2026-04-21
```

This reads from:

```text
data/raw/meta_ads/pollen_sense/2026-04-21/
```

and writes:

- DuckDB database: `data/normalized/meta_ads.duckdb`
- CSV snapshots: `data/normalized/meta_ads/pollen_sense/2026-04-21/`
- Ingestion summary: `data/normalized/meta_ads/pollen_sense/2026-04-21/ingestion_summary.json`

### Build the report

```powershell
build_meta_report --account pollen_sense --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis report --account pollen_sense --run-date 2026-04-21
```

This writes:

- Markdown report: `reports/pollen_sense/2026-04-21/meta_ads_report.md`
- JSON summary: `reports/pollen_sense/2026-04-21/meta_ads_report.json`

## What The Report Covers

- Executive summary
- Budget waste findings
- Fatigue and staleness findings
- Hook-rate and creative-performance findings
- Scaling candidates
- Tracking and measurement concerns
- Recommended next-7-day actions

## Reusable Agent Prompt

If you want an agent to review the newest run in the repo using the repo's own analysis rules, reuse:

```text
prompts/latest_meta_ads_analysis_prompt.md
```

That prompt tells the agent to:

- find the latest `reports/<account_slug>/YYYY-MM-DD/` folder for the requested account,
- start with `meta_ads_report.json`,
- fall back to the markdown report and normalized CSVs only as needed,
- and follow the interpretation rules in `AGENTS.md`.

## Notes

- Raw exports and generated reports are ignored by git by default because they usually contain sensitive business data.
- ROAS quality depends on the measurement setup behind the ad account. If purchase value tracking is weak, the report will say so instead of pretending the numbers are clean.
- The Meta API sync is read-only and uses `results` first, `app_installs` second, and ROAS only when revenue visibility is trustworthy.

See [AGENTS.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/AGENTS.md) for the analysis contract that Codex or another agent should follow when reviewing the generated data.

For step-by-step export instructions inside Meta, see [docs/META_EXPORT_GUIDE.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/docs/META_EXPORT_GUIDE.md).
