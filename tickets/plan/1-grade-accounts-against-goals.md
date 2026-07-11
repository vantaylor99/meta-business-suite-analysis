description: A tool that grades each managed ad account against the goal we set for it — e.g. is its cost-per-lead under the target, in a watch zone, or over the pause line — and returns a simple on-goal / watch / pause-candidate verdict for the whole portfolio in one call. Today nothing compares an account's real cost-per-result to its own configured goal.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/account_registry.py, src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Problem

The account config (`config/meta_ads_accounts.json`, read via `account_registry.resolve_account`)
already carries per-account **goals**: `target_cost_per_result`, `pause_cost_per_result_above`,
`roas_role`, `primary_result_label`, `evaluation_grace_days`, `primary_metric`, `primary_goal`.
`cross_account_performance` now returns a real `cost_per_result` / `roas` per account — but **nothing
compares that value to the account's own thresholds** and says where it stands. The verdict logic
(`pause_candidate` / `watch` / `keep`) already exists in `early_triage.py`
(`VERDICT_PAUSE_CANDIDATE`, `VERDICT_KEEP_WATCH`, …) and `monitor.py`, but only on the
single-account / CLI path — it is **not exposed as a cross-account MCP tool**.

This is the operational output the whole per-account goal config was built for, and the natural thing
a specialist opens the tool to answer: *"which of my accounts are off their goal right now?"*

## What it delivers

A read tool (working name `grade_accounts_against_goals`) that, over a scope, grades each
**configured** account against its own goal and returns a verdict:

- `on_goal` — at/under target
- `watch` — between target and pause threshold
- `pause_candidate` — over `pause_cost_per_result_above` (for cost) / under the ROAS bar
- plus the numbers behind it: the metric used, its value, `target_cost_per_result`,
  `pause_cost_per_result_above`, and currency.

Reuse — do not reinvent:
- the metric resolution from `cross_account_performance` (lead-key-family cost-per-result, ROAS),
- the verdict constants / classification from `early_triage.py` / `monitor.py`,
- the `evaluation_grace_days` grace logic so a just-launched account isn't harshly graded.

Respect `roas_role`: an account with `roas_role: not_applicable` (e.g. lead-gen like Seattle) is
**never** graded on ROAS — grade on `cost_per_result`. An account whose goal is ROAS-based grades on
ROAS.

## Scope note (the one legitimately config-scoped analysis tool)

Goals exist **only for accounts in `config/meta_ads_accounts.json`**. Reads remain open to every
reachable account, but grading requires a configured goal — so an account in scope that has **no
config entry** is reported `no_goal_configured` (not graded, not an error). Default scope = the
configured accounts. This is consistent with [[reads-open-writes-config-scoped]] — reads open,
but *goals* are per-managed-account.

## Output shape (proposal — plan stage finalizes)
Per account: `{account_id, name, currency, metric ("cost_per_result"|"roas"), value, target,
pause_threshold, in_grace (bool), verdict}`; plus a rollup: counts of
`on_goal` / `watch` / `pause_candidate` / `insufficient_data` / `no_goal_configured`, and an explicit
`pause_candidates` shortlist.

## Edge cases & interactions
- Account with no results → `cost_per_result` null → `insufficient_data`, NOT `pause_candidate`.
- `evaluation_grace_days`: a just-relaunched account (e.g. Seattle) inside its grace window →
  `in_grace: true`, softened verdict (reuse existing grace logic, don't invent a new rule).
- Direction correctness: lower cost = better; higher ROAS = better. A test must assert a cheap
  account grades `on_goal` and an over-threshold one `pause_candidate`.
- Currency: thresholds are stated in the account's own currency, so compare **native**
  cost_per_result to the native threshold — do not FX-convert the threshold (simplest + correct; per
  [[currency-precision-low-priority]]).
- Missing/partial thresholds (e.g. `target` set but `pause` null) → grade on what's present, report
  the gap; never crash.
- Real datum for validation: Seattle (`act_103014553`) reads ~$58–66 cost-per-lead vs `$10` target /
  `$40` pause → should grade `pause_candidate` (modulo grace).

## Use cases
- Specialist: "which of my ~15 accounts are over their cost-per-lead goal?"
- Supervisor / WWFT: portfolio verdict counts + the pause-candidate shortlist.
- Feeds the portfolio digest (`portfolio-digest`, which consumes this tool's verdicts).
