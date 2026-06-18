# Meta Action Workflow

This repo now supports a guarded path from analysis to action:

1. Sync or ingest data.
2. Build the report.
3. Generate `action_plan.json`.
4. Generate `operator_brief.md`.
5. Review and approve specific executable actions.
6. Dry-run the approved actions.
7. Execute only after the dry run matches the operator's intent.
8. Review the timestamped action result log.

## Commands

Generate a plan:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-05-04
```

Generate a plan with current live ad status:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-05-04 --enrich-live-state
```

Dry-run approved actions:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-05-04
```

Build the operator brief:

```powershell
python -m meta_ads_analysis operator-brief --account pollen_sense --run-date 2026-05-04
```

Execute approved actions:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-05-04 --execute
```

## Approval Model

Actions are generated with `status: "proposed"`.

Only executable actions with `status: "approved"` are sent to the Meta CLI. Non-executable actions remain operator tasks, even if their status is changed.

## Account Goals

Account-specific action policy lives in `config/meta_ads_accounts.json`.

- `pollen_sense`: prioritize in-app subscription results first, regardless of cost. When subscription results are sparse, use app installs as the secondary signal with a target of `$3` per install.
- `divine_designs`: optimize toward `3.0` blended ROAS or better.

These goals change both waste detection and scaling recommendations. For example, a Pollen ad can be paused for missing subscription results and exceeding the install target, while a Divine Designs ad is judged primarily against ROAS.

## Executable Scope

The executor supports:

- `pause_ad`: pauses a specific ad with high waste risk or account-policy waste risk.
- `increase_adset_budget`: raises a daily ad set budget only when the proposed action includes the current daily budget, the proposed new daily budget, and the increase stays within the action's `max_increase_percent`.

Budget increases are intentionally capped. The plan can identify the ad set to scale, but live-state enrichment must populate the current daily budget before the executor will build a command.

The workflow intentionally does not execute:

- budget decreases,
- campaign creation,
- ad set creation,
- creative creation,
- creative replacement,
- broad automated rules.

Those can be added later, but they need tighter account-specific controls because broad mutations are harder to unwind than pausing one clearly wasteful ad or applying a capped budget increase.

## Meta AI / Advantage+ Policy

Keep Meta AI creative features turned off by default.

The executor only allows explicit status changes for approved V1 actions. It blocks action parameters that try to set Meta AI or Advantage+ creative controls such as:

- Advantage+ creative enhancements,
- automatic text variations,
- image expansion,
- visual touch-ups,
- music generation,
- flexible media or AI-generated creative variants.

Live-state enrichment also checks ad set payloads for signs of targeting automation or Advantage audience controls. When detected, the plan adds a non-executable remediation task so the operator can disable those controls in Meta or after the CLI exposes a safe explicit field.

If a future workflow needs to create ads, ad sets, or creatives, it should carry this policy forward by making all AI/Advantage+ controls explicit and defaulting them to disabled.

## Result Logs

Each dry run or execution writes:

```text
reports/<account_slug>/<run_date>/action_results_<timestamp>.json
```

Use this file as the audit trail for what was skipped, dry-run, executed, blocked, or failed.

## Fresh Data

When `META_ACCESS_TOKEN` is not available but the Meta CLI is authenticated, use:

```powershell
python -m meta_ads_analysis sync-cli --account divine_designs --run-date 2026-06-16
```

This keeps the normal raw, normalized, and report outputs while sourcing insights from `meta ads insights get`.

## Later Phase: Operator Brief

The operator brief turns the generated `action_plan.json` into:

- what changed since the last run,
- what is approved to execute,
- what still needs human judgment,
- and which account goal each action supports.

It writes:

```text
reports/<account_slug>/<run_date>/operator_brief.md
reports/<account_slug>/<run_date>/operator_brief.json
```

Keep `operator_brief_todo` enabled in account policy while this brief is still being refined against real operator use.
