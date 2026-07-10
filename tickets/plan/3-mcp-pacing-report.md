description: Show whether each ad account is on track to spend its budget for the period — which accounts are overspending, which are underspending, and the projected end-of-month total — across all the accounts someone oversees.
prereq: mcp-cross-account-batched-fanout
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

Budget pacing is a distinct question from performance: "given how much has been spent so far this
period and the configured budget, will each account land over, under, or on target?" At scale this
is the difference between catching a runaway or stalled account mid-month and finding out at
month-end. It needs spend-to-date plus the account/campaign budget configuration, which is separate
data from the insights metrics.

## What it must deliver

- Per account in scope: spend-to-date over the period, the applicable budget (account spend cap
  and/or summed active daily/lifetime budgets — plan stage to define which is authoritative), the
  elapsed fraction of the period, a **projected end-of-period spend**, and a pacing status
  (`on_track` / `over` / `under` with a % variance).
- Roll up a scope-level view: total budgeted vs. total projected in a reporting currency (reuse
  normalization from `cross_account_performance` if available; otherwise per-currency subtotals),
  and a shortlist of the worst over/under-pacers.
- Handle the several ways budget is expressed in Meta (account spend cap, campaign budget
  optimization, adset daily/lifetime budgets) — the plan stage must state the precedence rule and
  what is read via which fields, since naive summing double-counts CBO vs. adset budgets.

## Behavior / interface (proposal — plan stage to finalize)

- `pacing_report(date_from, date_to, account_ids=None, reporting_currency="USD")` → per-account
  `{account_id, name, currency, spend_to_date, budget, elapsed_fraction, projected_spend,
  status, variance_pct}` + a scope rollup + `errors`.
- Projection helper (spend-to-date / elapsed × period) as a pure, testable function.

## Edge cases & interactions

- Period not yet started / elapsed fraction 0 → no projection (guard divide-by-zero), status
  "not started."
- No budget configured (uncapped account) → status "no budget set," excluded from over/under math,
  reported explicitly.
- Mixed budget types in one account (CBO campaign + adset budgets) → documented precedence to avoid
  double-counting.
- Paused/closed accounts → excluded or clearly marked, not counted as under-pacing.
- Currency: budget and spend must be compared in the same currency per account; rollup uses
  normalized figures.
- Determinism + partial failure inherited from the fan-out engine.

## Use cases

- WWFT / manager: mid-month "are we on track to spend the period's budget across all accounts, and
  who's off?"
- Supervisor: catch a specialist's account that stopped delivering (under-pacing) or is about to
  blow its cap (over-pacing) before month-end.
- Specialist: confirm their own accounts will land on budget.
