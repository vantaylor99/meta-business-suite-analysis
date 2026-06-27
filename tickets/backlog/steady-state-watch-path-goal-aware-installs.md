description: On install-focused accounts, the everyday "is this ad underperforming?" watch scan still judges every ad by purchase return-on-spend, so a perfectly healthy install ad (which makes almost no purchases by design) keeps getting flagged as a problem once it is past its first few days.
prereq:
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## Problem

`monitor.build_watch_report` grades every ad through `classify_ad(...)` (ROAS-only) on the
steady-state path (`src/meta_ads_analysis/monitor.py:817`). For an install-goal account
(`primary_goal == "maximize_in_app_subscriptions"`) an install ad books ~0 `purchase` actions by
design, so its ROAS reads ~0 and `classify_ad` flags it `urgent`/`underperforming` even when
cost-per-install is excellent.

This is the **same class of bug** as the `early-life-forced-decision-install-goal` ticket
(now complete) — but on the mature / steady-state path rather than the day-3 forced decision.
That ticket deliberately scoped itself to the early-life forced decision and made *only* the day-3
own-sample grade goal-aware (via `early_triage.classify_own_sample` + `monitor._forced_decision_install`).
Everything older than `early_life_max_age` and not on probation still falls through to the ROAS-only
`classify_ad` call at `monitor.py:817`.

## Scope / what "done" looks like

Make the steady-state watch grade goal-aware for install accounts the same way the early-life path
now is:

- An install-goal ad on the steady-state path should be graded on **cost-per-install vs the account's
  install-cost target** (`secondary_cost_per_app_install_target`, falling back to
  `pause_if_no_primary_and_secondary_cost_above`), not ROAS.
- Reuse the existing goal-aware machinery rather than duplicating thresholds: `early_triage`'s
  `goal_kind` / `classify_own_sample` already encode the install bar, and `actions._select_action_metric`
  (`src/meta_ads_analysis/actions.py:626`) already encodes the goal→metric selection used elsewhere.
  Prefer routing through one of those over re-deriving cost-per-install in `monitor`.
- The emitted row's `evidence.metric_name` should be `cost_per_app_install` (not `roas`) for install
  accounts, and `dollars_at_risk` / `confidence` should be consistent with how the early-life install
  branch already reports them (`monitor._forced_decision_install`).
- Install account with **no** configured install-cost target should degrade gracefully (keep running /
  abstain), mirroring `classify_own_sample`'s thresholds-None behavior — never crash, never guess.
- ROAS accounts must remain byte-for-byte unchanged on this path.

## Why this is its own ticket (not folded into the early-life fix)

`classify_ad` is also consumed by the watchlist accounting (`times_flagged`, `flaggable`,
`new_watchlist`) and the row ordering at `monitor.py:853`. Making the whole watch path goal-aware
touches the classification taxonomy (`urgent`/`underperforming`/`watch`/`ok`/`insufficient`) for the
install case, not just a single keep/kill, so it is a larger, higher-risk change than the day-3
forced decision and warrants its own design + test pass. Consider whether `classify_ad` itself should
become goal-aware (one engine, goal-parameterized) vs. a parallel install classifier — the early-life
work chose the latter (a separate `classify_own_sample`); weigh consolidating instead of growing a
third copy of the install bar.

## Tests to add

- Steady-state (age > `early_life_max_age`, not on probation) install ad with cheap cost-per-install
  → NOT flagged (today: wrongly flagged urgent/underperforming on ~0 ROAS).
- Same age, expensive cost-per-install → flagged.
- Install account with no install-cost target → degrades to keep/abstain, no crash.
- Regression: ROAS-account steady-state rows unchanged.
