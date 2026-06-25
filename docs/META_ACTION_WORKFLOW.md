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

Only executable actions with `status: "approved"` are sent to the Meta Graph API. Non-executable actions remain operator tasks, even if their status is changed.

## Evidence and Confidence

Each recommendation-bearing action (`pause_ad`, `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`) carries two structured blocks:

- `evidence`: the deterministic facts behind the call — the metric the decision rests on (ROAS for ROAS-goal accounts, cost-per-install for install-goal accounts), the window, the sample (purchases / spend), the entity, and a `regenerating_query` that reproduces the metric.
- `confidence`: a computed band (`high` / `medium` / `low` / `abstain`) from the shared confidence engine. The band is never free-typed; it is derived from sample size, recency, and how causal the evidence is. Grounding caps data strength, so a large-sample correlational call can never read `high`.

For the executable pause/budget paths, a sample below the significance floor (too few conversions and too little spend) does **not** become a confident pause or scale. The action is flipped to a non-executable `verdict: "insufficient_data"` recommendation — "promising test, keep running and re-check as more data accrues" — with `executable: false` and `approval_required: false`, so thin data can never be approved into a write.

## Account Goals

Account-specific action policy lives in `config/meta_ads_accounts.json`.

- `pollen_sense`: prioritize in-app subscription results first, regardless of cost. When subscription results are sparse, use app installs as the secondary signal with a target of `$3` per install.
- `divine_designs`: optimize toward `3.0` blended ROAS or better.

These goals change both waste detection and scaling recommendations. For example, a Pollen ad can be paused for missing subscription results and exceeding the install target, while a Divine Designs ad is judged primarily against ROAS.

## Executable Scope

The executor supports:

- `pause_ad`: pauses a specific ad with high waste risk or account-policy waste risk.
- `increase_adset_budget`: raises a daily ad set budget only when the proposed action includes the current daily budget, the proposed new daily budget, and the increase stays within the action's `max_increase_percent`.

Budget increases are intentionally capped. The plan can identify the ad set to scale, but live-state enrichment must populate the current daily budget before the executor will build an operation.

Writes go through the Meta Graph API (`MetaMarketingApiClient.update_ad` / `update_adset`), so the action workflow runs natively on any platform with no CLI/WSL dependency. Executing actions requires `META_ACCESS_TOKEN` to carry the `ads_management` permission; dry runs and live-state reads only need `ads_read`.

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

Live-state enrichment also checks ad set payloads for signs of targeting automation or Advantage audience controls. When detected, the plan adds a non-executable remediation task so the operator can disable those controls in Meta. Disabling automation is intentionally left as an operator follow-up rather than an automatic write, so the executor never silently changes targeting automation.

If a future workflow needs to create ads, ad sets, or creatives, it should carry this policy forward by making all AI/Advantage+ controls explicit and defaulting them to disabled.

## Result Logs

Each dry run or execution writes:

```text
reports/<account_slug>/<run_date>/action_results_<timestamp>.json
```

Use this file as the audit trail for what was skipped, dry-run, executed, blocked, or failed.

## Fresh Data

Pull fresh data from the Meta Graph API (requires `META_ACCESS_TOKEN`):

```powershell
python -m meta_ads_analysis sync-api --account divine_designs --run-date 2026-06-16
```

This writes the normal raw, normalized, and report outputs by sourcing insights directly from the Graph API.

## Later Phase: Operator Brief

The operator brief turns the generated `action_plan.json` into:

- what changed since the last run,
- what is approved to execute,
- what still needs human judgment,
- and which account goal each action supports.

Under every recommendation the brief also surfaces the action's `evidence` and `confidence`
(carried through from the action plan, not recomputed): a compact `Evidence:` line (the number,
window, sample, and entity), a `Confidence:` band line in the shared 🟢/🟡/🔴/⚪ vocabulary, a
`Re-check:` line with the exact `account_metrics` command that reproduces the number, and what
would raise or lower the band. Abstain actions read as "Insufficient data — keep running" (never a
percentage), and a correlational causal claim shows the "confirm via A/B" caveat plus the offer to
file an experiment via `experiment define`.

It writes:

```text
reports/<account_slug>/<run_date>/operator_brief.md
reports/<account_slug>/<run_date>/operator_brief.json
```

Keep `operator_brief_todo` enabled in account policy while this brief is still being refined against real operator use.
