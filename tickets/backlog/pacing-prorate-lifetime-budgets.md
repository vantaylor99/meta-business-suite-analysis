description: The budget-pacing tool can tell you whether an account is on track when its budget is set as a daily amount, but when the budget is instead a single lifetime pot spread over a campaign's whole run, it just reports the number and says "can't project" — it never works out whether that account is ahead of or behind schedule.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## Context

`pacing_report` projects end-of-period spend against the sum of **active daily budgets** (the period
budget). An account whose only budget is a **lifetime budget** — a fixed pot Meta paces over the
entity's own `start_time`..`stop_time` schedule — is currently classified `budget_not_projectable`:
its `lifetime_budget_total` is reported for context, but no over/under verdict is produced, because a
lifetime budget spans the entity's own schedule, not the arbitrary `date_from`..`date_to` reporting
window the tool is asked about. Prorating it correctly needs each campaign/adset's schedule, which the
budget read here (`PACING_CAMPAIGN_FIELDS` / `PACING_ADSET_FIELDS`) deliberately does **not** fetch.

## What to build

Extend pacing so a lifetime-budget entity gets a real pacing verdict by **prorating its lifetime budget
across the overlap** between its own schedule and the reporting window:

- add `start_time` / `stop_time` to the campaign + adset budget reads (still budget-only fields);
- for each ACTIVE lifetime-budget entity, compute the fraction of its own schedule that falls inside
  `[date_from, effective_as_of]` vs. its full `[start_time, stop_time]`, and derive an
  expected-to-date figure to compare against spend-to-date;
- fold the result into the existing `over` / `under` / `on_track` classification (or a clearly-labeled
  lifetime variant), so a lifetime-only account is no longer a blanket `budget_not_projectable`.

Edge cases to design for at plan stage: an entity with no `stop_time` (open-ended), a schedule that
does not overlap the reporting window at all, a schedule wholly inside the window, and mixed accounts
(some daily-budget, some lifetime) whose rollup must combine both consistently. Keep the pure/clock-free
helper shape (`pacing_period`-style) so it stays unit-testable with explicit dates.

## Acceptance

- A lifetime-only account with a schedule overlapping the reporting window returns an over/under/on_track
  verdict grounded in the prorated expectation, not `budget_not_projectable`.
- Daily-budget accounts are unaffected (byte-identical output).
- The "lifetime budgets are reported but NOT projected" caveat in the `pacing_report` /
  `summarize_account_budget` docstrings is updated.
