description: Extend the "which accounts need attention" scan with two heavier signals it doesn't yet cover — whether an account is off its budget pace, and whether individual ads have been disapproved — so the attention list is complete rather than just the behavior-change signals.
prereq: pacing-currency-aware-minor-units, pacing-prorate-lifetime-budgets
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
----

## Why this is separate

The attention scan (`flag_accounts_needing_attention`) intentionally shipped with only the flags that
derive purely from two `cross_account_performance` reads plus the account-status label already on each
row (spend spike/collapse, cost-per-result / CPC / CTR degradation, stalled delivery, account-status
alerts). Two flag families from the original plan were deferred because each pulls in a **different,
heavier data surface** and would have oversized the first ticket:

- **Budget pacing off.** "Is this account on track to over/under-spend its budget for the period?" is
  a different question over different data (account spend cap, CBO campaign budgets, adset
  daily/lifetime budgets) — already owned by the sibling tool `pacing_report`. The clean design is to
  **merge `pacing_report`'s per-account over/under status into the attention list as a
  `budget_pacing_off` flag** (reuse, don't re-read budget config). That requires `pacing_report`
  landed, hence the prereq.

- **Ad-level creative / disapproval problems.** Detecting DISAPPROVED or active-but-not-delivering
  ads requires a **per-account ad-level fan-out** (each account × its ads), a materially larger read
  cost than the account-level scan. Worth doing, but it needs its own read-budget design (only fan
  out into ads for accounts already flagged? cap the ad reads? cache?).

## What it should deliver

- A `budget_pacing_off` flag on the attention output, sourced from `pacing_report` for the same scope
  and window — severity from the pacing variance (materially over cap = high; materially under =
  medium). No duplicate budget-config reads: call `pacing_report`, fold its status in.
- An optional, opt-in ad-level delivery-health flag (`ads_disapproved` / `ads_not_delivering`) that
  only fans out into the ads of accounts already surfaced by the cheap scan, so a full-fleet run
  doesn't pay 200× ad reads unconditionally. Reuse `monitor.py`'s `effective_status` / `DELIVERING`
  vocabulary (`monitor.py:66-67`).

## Open questions for the plan pass

- How to combine two tools (`flag_accounts_needing_attention` + `pacing_report`) without a circular
  dependency or a double fan-out — likely the attention tool gains an opt-in `include_pacing=True`
  that calls `pacing_report` once and joins.
- Ad-level read budget: gate ad fan-out behind "already flagged" and/or an explicit
  `include_ad_health` flag; document the worst-case read count.
