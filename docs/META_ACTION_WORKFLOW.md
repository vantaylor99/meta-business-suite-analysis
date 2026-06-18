# Meta Action Workflow

This repo now supports a guarded path from analysis to action:

1. Sync or ingest data.
2. Build the report.
3. Generate `action_plan.json`.
4. Review and approve specific executable actions.
5. Dry-run the approved actions.
6. Execute only after the dry run matches the operator's intent.
7. Review the timestamped action result log.

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

Execute approved actions:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-05-04 --execute
```

## Approval Model

Actions are generated with `status: "proposed"`.

Only executable actions with `status: "approved"` are sent to the Meta CLI. Non-executable actions remain operator tasks, even if their status is changed.

## V1 Executable Scope

V1 supports only:

- `pause_ad`: pauses a specific ad with high waste risk.

V1 intentionally does not execute:

- budget increases,
- budget decreases,
- campaign creation,
- ad set creation,
- creative creation,
- creative replacement,
- broad automated rules.

Those can be added later, but they need tighter account-specific controls because bad budget mutations are harder to unwind than pausing one clearly wasteful ad.

## Meta AI / Advantage+ Policy

Keep Meta AI creative features turned off by default.

The executor only allows explicit status changes for approved V1 actions. It blocks action parameters that try to set Meta AI or Advantage+ creative controls such as:

- Advantage+ creative enhancements,
- automatic text variations,
- image expansion,
- visual touch-ups,
- music generation,
- flexible media or AI-generated creative variants.

If a future workflow needs to create ads or creatives, it should carry this policy forward by making all AI/Advantage+ creative controls explicit and defaulting them to disabled.

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
