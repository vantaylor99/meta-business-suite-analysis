description: Our automated ad-grading and action pipeline only understands "ROAS" and "app-install" account goals — it has no notion of a "cost per lead" goal, so lead-gen accounts can't be automatically graded, paused, or scaled against their cost-per-lead targets.
prereq: seattle-mission-lead-metric-resolution
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/briefs.py, config/meta_ads_accounts.json
----

## Background

`seattle_mission` has `action_policy.primary_goal = "minimize_cost_per_lead"` with
`primary_metric = "cost_per_result"`, `target_cost_per_result = 10.0`, and
`pause_cost_per_result_above = 40.0`. Once the sibling ticket
[[seattle-mission-lead-metric-resolution]] lands, `results`/`cost_per_result` (= cost per lead)
populate correctly, so an **agent** can grade the account by hand against those thresholds per the
AGENTS.md grounding rules.

However, the **automated** goal-aware machinery does not understand a cost-per-lead goal at all. A
grep confirms `minimize_cost_per_lead`, `target_cost_per_result`, and `pause_cost_per_result_above`
appear **only in config** — no code reads them. Every goal-aware helper hardcodes two kinds,
`"roas"` and `"install"`, and falls back to ROAS otherwise:

- `actions._select_action_metric` / `actions._qualifies_for_budget_increase` (`actions.py:626`, `:785`)
- `control._status_metric` / the goal-aware conversion-count helper (`control.py:640`)
- `monitor._policy_floors` / `_floors_from_policy` (`monitor.py:275`) and the goal-aware own-sample grade
- `early_triage._goal_kind` / `_goal_thresholds` / `_metric_name` (`early_triage.py:178-207`) — kinds are literally `"roas" | "install"`
- `briefs._account_goal` (`briefs.py:367`) — has no `minimize_cost_per_lead` branch, so the operator brief says "No specific account action goal is configured" for this account
- `account_discovery` benchmark/attention logic already treats `cost_per_result` as lower-is-better, so parts of the read side are ready

## Why this is not urgent (and why it's split out)

`seattle_mission` is run day-to-day by account manager Hunter Johanson **directly in Meta Ads
Manager, out-of-band** from this repo's guarded write flow (see the account decision log). We are not
executing automated pauses/scales on it today, so the immediate need — being able to *read and grade*
it — is fully met by the sibling ticket plus the agent-facing grade rules. Adding a first-class
`"lead"` goal-kind threads a new branch through five subsystems (actions, control, monitor,
early_triage, briefs) plus their tests; that is a coherent feature in its own right and too large to
fold into the metric-resolution fix without blowing past one agent run.

## Scope (when promoted)

Introduce a third goal-kind (`"lead"`, or generalize to a cost-per-conversion kind) so that:

- the metric selected for grading/pausing/scaling is `cost_per_result` (cost per lead),
  lower-is-better;
- the pause threshold is `pause_cost_per_result_above` and the target/scale threshold is
  `target_cost_per_result`, honouring `evaluation_grace_days` (3) and `min_spend_before_pause` ($100)
  exactly as the ROAS/install paths do;
- ROAS is never applied (`roas_role: not_applicable`);
- `briefs._account_goal` describes the cost-per-lead goal;
- the significance-floor / abstain and adversarial-review behavior is preserved for the new kind.

This is a specification, not a design — a future `plan/` pass should settle the exact goal-kind
abstraction (new discrete kind vs. a generalized cost-per-conversion kind covering both install and
lead), the direction checks in `review.py` for a lower-is-better cost metric, and the test matrix,
before emitting implement tickets.
