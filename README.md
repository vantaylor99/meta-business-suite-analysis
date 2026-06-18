# Meta Ads Analysis Repo

This repository gives you a fast workflow for analyzing Meta ad performance from either manual CSV exports or a direct Meta Marketing API sync.

The workflow is:

1. Export reports from Meta Ads Manager.
2. Drop the CSVs into `data/raw/meta_ads/<account_slug>/<run_date>/`.
3. Run `ingest_meta_exports`.
4. Run `build_meta_report`.
5. Review the generated report in `reports/<account_slug>/<run_date>/`.
6. Generate an approved action plan before making Meta account changes.

The implementation keeps one normalized reporting pipeline and supports two input paths:

- manual Ads Manager exports
- direct API sync into the same raw CSV contract
- guarded Meta CLI actions from approved report findings

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

### Sync data through the installed Meta CLI

Use this when the Meta CLI is authenticated but `META_ACCESS_TOKEN` is not available in the shell:

```powershell
sync_meta_cli --account divine_designs --run-date 2026-06-16
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis sync-cli --account divine_designs --run-date 2026-06-16
```

This uses `meta ads insights get`, writes the same raw CSV contract as the API sync, then runs ingest and report unless `--raw-only` is provided.

The CLI sync defaults to a faster ad selection mode:

```powershell
python -m meta_ads_analysis sync-cli --account divine_designs --run-date 2026-06-16 --ad-filter active_or_recently_updated --max-workers 6
```

Filter options:

- `active_or_recently_updated` skips old paused ads while keeping active and recently changed ads.
- `active` only queries currently active/effectively active ads.
- `all` queries every ad returned by the Meta CLI and is slowest.

Use `--max-workers 1` for sequential calls if the Meta CLI or API starts rate-limiting parallel requests.

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

### Propose account actions

```powershell
propose_meta_actions --account pollen_sense --run-date 2026-04-21
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-04-21
```

To include current live Meta status for ads in the plan:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-04-21 --enrich-live-state
```

Live-state enrichment checks whether proposed ad-level actions are already resolved, for example when an ad is already paused. It also checks ad set state for current daily budgets and signs of Meta AI / Advantage audience automation.

This reads `meta_ads_report.json` and writes:

- Action plan: `reports/<account_slug>/<run_date>/action_plan.json`

The action plan is intentionally approval-based. Executable actions start with:

```json
"status": "proposed"
```

To allow execution, review the rationale and evidence, then change only the intended actions to:

```json
"status": "approved"
```

Executable actions are conservative:

- `pause_ad` for high-waste ads or account-policy waste risk.
- `increase_adset_budget` for qualifying scale candidates, capped by the account policy and requiring live current-budget evidence before execution.

Creative refreshes, measurement concerns, and Meta AI / Advantage control remediation are logged as non-executable operator tasks until a human supplies exact instructions or the Meta CLI exposes a safe explicit field.

### Dry-run or apply approved actions

Dry-run approved actions first:

```powershell
apply_meta_actions --account pollen_sense --run-date 2026-04-21
```

Actually execute approved actions through the installed Meta CLI:

```powershell
apply_meta_actions --account pollen_sense --run-date 2026-04-21 --execute
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-04-21 --execute
```

This writes a timestamped results log:

- `reports/<account_slug>/<run_date>/action_results_<timestamp>.json`

The executor uses the installed `meta` CLI and currently sends commands shaped like:

```text
meta --no-input -o json ads --ad-account-id <act_id> ad update <ad_id> --status paused
meta --no-input -o json ads --ad-account-id <act_id> adset update <adset_id> --daily-budget <cents>
```

Meta AI / Advantage+ features are kept out of the execution surface by default. The action executor only changes explicit approved fields and blocks parameters that try to set Meta AI or Advantage+ controls.

Account action goals are configured in `config/meta_ads_accounts.json`:

- Pollen Sense prioritizes in-app subscription results first, then app installs at a `$3` target.
- Divine Designs optimizes toward `3.0` blended ROAS or better.

### Build an operator brief

After generating an action plan, build the short human review brief:

```powershell
python -m meta_ads_analysis operator-brief --account divine_designs --run-date 2026-06-16
```

This writes:

- `reports/<account_slug>/<run_date>/operator_brief.md`
- `reports/<account_slug>/<run_date>/operator_brief.json`

The brief summarizes the account goal, what changed from the previous run, what is ready for approval, what is already approved to execute, what still needs human judgment, and any Meta AI / Advantage follow-ups.

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
- Meta CLI execution is intentionally not automatic. Generate a plan, review it, approve specific actions, dry-run, then use `--execute`.
- Use `propose-actions --enrich-live-state` before approving budget increases so the plan has current live ad set budgets.

See [AGENTS.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/AGENTS.md) for the analysis contract that Codex or another agent should follow when reviewing the generated data.

For step-by-step export instructions inside Meta, see [docs/META_EXPORT_GUIDE.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/docs/META_EXPORT_GUIDE.md).
