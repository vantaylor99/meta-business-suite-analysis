# Meta Ads Analysis Repo

This repository gives you a fast workflow for analyzing Meta ad performance from either manual CSV exports or a direct Meta Marketing API sync.

The workflow is:

1. Export reports from Meta Ads Manager.
2. Drop the CSVs into `data/raw/meta_ads/<account_slug>/<run_date>/`.
3. Run `ingest_meta_exports`.
4. Run `build_meta_report`.
5. Review the generated report in `reports/<account_slug>/<run_date>/`.
6. Generate an approved action plan before making Meta account changes.

The implementation keeps one normalized reporting pipeline with two read input paths and a broad
guarded-write surface:

- **Reads:** manual Ads Manager exports, or a direct API sync into the same raw CSV contract. Reads
  flow through a swappable backend (direct Graph client by default, or an opt-in Meta MCP read server).
- **Guarded writes:** the action plan (pause / budget-increase), control ops (enable/pause, CBO-aware
  budget +/-, targeting, creative features), authoring (create campaign / ad set / ad / video ad /
  lookalike — all created PAUSED), and audience rotation / Advantage-disable / rename. Every write is
  proposed, evidence-grounded, adversarially reviewed, approved, dry-run/validated, then executed —
  **there is no delete or archive**. See [Hybrid Meta integration](#hybrid-meta-integration) for the
  full catalog.

## Install

```powershell
pip install -e .[dev]
```

## Hybrid Meta integration

The Meta integration is **hybrid and grounded**, and runs as a **single operator** today:

- **Reads are swappable.** They flow through a `MetaReaderProvider` seam selected by
  `META_READER_BACKEND` — `direct` (default; the live Graph client) or `mcp` (a Meta MCP read server).
  A community token-based MCP server is wired as a **disabled, unvetted placeholder** in `.mcp.json`,
  and Meta's official OAuth server is a **config-only drop-in for later** (no code change). Writes
  always use the direct Graph client.
- **Writes are guarded and broad.** Beyond the action plan, the agent can enable/pause ads, change
  CBO-aware daily budgets (up or down), edit targeting/creative features, author new campaigns / ad
  sets / ads / video ads / lookalikes (all created **PAUSED**), and rotate audiences / disable
  Advantage-Audience / rename — all behind a propose → review → approve → validate → execute gate with
  an audit log, and **no delete/archive**. (The one exception is media-library uploads — `upload-video`
  and ad-authoring image upload — which push an *inert, unreferenced* asset to the account directly;
  see the AGENTS.md catalog for why they are deliberately ungated.)
- **Auth is single-operator now.** One long-lived `META_ACCESS_TOKEN`; multi-user / OAuth login is a
  documented later concern, not built.

**The authoritative reference** — read model, auth posture, and the full per-capability write catalog
(levels, reversible vs create-only, exact guardrails, and which CLI proposes each) — is
[AGENTS.md → Hybrid Meta integration](AGENTS.md#hybrid-meta-integration-read-model--auth--write-catalog).
The end-to-end workflow + diagram is in
[docs/META_ACTION_WORKFLOW.md](docs/META_ACTION_WORKFLOW.md), and MCP/token setup is in
[docs/META_API_SETUP.md](docs/META_API_SETUP.md).

> One command caveat: the budget proposer ships only as the `propose_budget` console script (after
> `pip install -e .`); it is not yet wired into `python -m meta_ads_analysis` (tracked in
> `tickets/backlog/wire-propose-budget-into-m-dispatch`). All other write commands work both ways.

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

> **Note:** All live data sync and actions now run through the Meta Graph API. The previous `sync-cli` path (which shelled out to the `meta` CLI and required WSL on Windows) has been removed — use `sync-api` everywhere. Live writes require `META_ACCESS_TOKEN` to have the `ads_management` permission; read-only sync needs only `ads_read`.

### Rotate audiences across active ad sets

Experiment with moving each active ad set's custom audience forward to the next ad set, recomputing exclusions so each ad set still targets one audience and excludes the others:

```powershell
# Propose (reads live ad sets, writes rotation_plan.json, no writes)
python -m meta_ads_analysis propose-rotation --account pollen_sense

# After approving rotations in rotation_plan.json, dry-run then execute
python -m meta_ads_analysis apply-rotation --account pollen_sense
python -m meta_ads_analysis apply-rotation --account pollen_sense --execute
```

Add `--disable-advantage-audience` to `propose-rotation` to also set `advantage_audience=0` on each rotated ad set that has it enabled, so the custom audience is genuinely respected during the experiment. It only ever turns the control off, never on, and is recorded per-rotation in the plan for approval.

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

Creative refreshes, measurement concerns, and Meta AI / Advantage control remediation are logged as non-executable operator tasks until a human supplies exact instructions, so the executor never silently changes targeting automation.

### Dry-run or apply approved actions

Dry-run approved actions first:

```powershell
apply_meta_actions --account pollen_sense --run-date 2026-04-21
```

Actually execute approved actions through the Meta Graph API (token needs `ads_management`):

```powershell
apply_meta_actions --account pollen_sense --run-date 2026-04-21 --execute
```

If the installed script is not on your `PATH`, use:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-04-21 --execute
```

This writes a timestamped results log:

- `reports/<account_slug>/<run_date>/action_results_<timestamp>.json`

The executor writes directly through the Meta Graph API client
(`MetaMarketingApiClient.update_ad` / `update_adset` / `update_campaign`) — there is no dependency on
the old `meta` CLI or WSL. It only changes explicit approved fields, and `--validate-only` pre-flights
the change against Meta before any real write.

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

Before the brief is built, an adversarial **review gate** re-checks each recommendation from its own cited evidence and claimed confidence band (sample size, window length, correlation-vs-cause, whether the band is actually earned, and whether the action agrees with its own number). Calls that fail are corrected (band downgraded) or dropped from their normal section and surfaced — never silently deleted — under a "Refuted / Downgraded By Review" heading, with the failing input and reason shown. The gate can only ever demote a call, never promote one. Pass `--no-review` to skip the gate and reproduce the pre-gate brief.

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
- The Meta API reporting sync is read-only and uses `results` first, `app_installs` second, and ROAS only when revenue visibility is trustworthy.
- Graph API execution is intentionally not automatic. Generate a plan, review it, approve specific actions, dry-run, then use `--execute`. Writes require an `ads_management`-scoped `META_ACCESS_TOKEN`.
- Use `propose-actions --enrich-live-state` before approving budget increases so the plan has current live ad set budgets.

See [AGENTS.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/AGENTS.md) for the analysis contract that Codex or another agent should follow when reviewing the generated data.

For step-by-step export instructions inside Meta, see [docs/META_EXPORT_GUIDE.md](/C:/van-and-kim-venture-strategy/meta-business-suite-analysis/docs/META_EXPORT_GUIDE.md).
