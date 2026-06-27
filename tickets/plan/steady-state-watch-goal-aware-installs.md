description: On install-goal accounts, the everyday "is this ad underperforming?" watch scan still grades every ad by purchase ROAS — so a healthy mature install ad (which makes almost no purchases by design) keeps getting flagged as urgent once it's past its first few days. Make the steady-state watch goal-aware (grade install ads on cost-per-install), the way the early-life day-3 check already is.
prereq:
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## Problem

`monitor.classify_ad` (the steady-state watch path behind `build_watch_report`, ~`monitor.py:817`) is
ROAS-only: it classifies urgent / underperforming / ok against `roas_floor` / `roas_target` and derives
`dollars_at_risk` from ROAS. For an install-goal account (`primary_goal == "maximize_in_app_subscriptions"`),
a healthy mature install ad books ~0 purchase ROAS **by design**, so once it ages past the early-life grace
window it gets wrongly flagged `urgent`/`underperforming` on a metric that doesn't describe its goal — a
false-positive pause signal.

Same **class** of bug as the completed `early-life-forced-decision-install-goal` fix, but on the
**mature / steady-state** path rather than the day-3 forced decision. That work made *only* the day-3 grade
goal-aware (`early_triage.classify_own_sample` + `monitor._forced_decision_install`, ~`monitor.py:550`);
everything older than `early_life_max_age` and not on probation still falls through to the ROAS-only
`classify_ad`.

## Investigate FIRST (may narrow or eliminate the change)

- Do install-goal accounts actually run `build_watch_report` in practice, or is the watch scan
  ROAS-account-only by deployment convention? If the latter, this is a **documentation fix**, not code.
  Resolve this before designing the code change.

## Scope / what "done" looks like (if code is needed)

- An install-goal ad on the steady-state path is graded on **cost-per-install vs the account's install-cost
  target** (`secondary_cost_per_app_install_target`, falling back to
  `pause_if_no_primary_and_secondary_cost_above`), not ROAS.
- **Reuse** the existing goal-aware machinery, don't duplicate thresholds: `early_triage`'s
  `goal_kind` / `classify_own_sample` already encode the install bar, and `actions._select_action_metric`
  (~`actions.py:626`) already encodes goal→metric selection. Route through one of those.
- Emitted row `evidence.metric_name` = `cost_per_app_install` (not `roas`) for install accounts;
  `dollars_at_risk` / `confidence` consistent with the early-life install branch
  (`monitor._forced_decision_install`).
- Install account with **no** configured install-cost target degrades gracefully (keep running / abstain),
  mirroring `classify_own_sample`'s thresholds-None behavior — never crash, never guess.
- ROAS accounts unchanged byte-for-byte on this path.

## Key design decision (resolve in plan)

`classify_ad` is also consumed by watchlist accounting (`times_flagged`, `flaggable`, `new_watchlist`) and
row ordering (~`monitor.py:853`), so making the watch path goal-aware touches the classification taxonomy
(urgent / underperforming / watch / ok / insufficient) for the install case — a larger, higher-risk change
than the day-3 keep/kill. **Decide: make `classify_ad` itself goal-aware (one engine, goal-parameterized)
vs. a parallel install classifier.** Early-life chose a parallel path (`classify_own_sample`); weigh
consolidating into one engine vs. growing a third copy of the install bar.

## Edge cases & interactions

- Watchlist accounting / ordering must stay coherent for install rows (the new metric feeds
  `times_flagged` / `flaggable` / ordering correctly).
- Early-life (probation) install ads already route to `_forced_decision_install` — don't double-handle;
  only mature steady-state ads are in scope.
- No install-cost target → abstain / keep, no crash.
- ROAS-account regression: steady-state rows byte-for-byte identical.
- Determinism: pass `as_of` / recency in (no clock in logic), matching the existing no-`datetime` test style.

## Tests

- Steady-state (age > `early_life_max_age`, not on probation) install ad, cheap cost-per-install → NOT
  flagged (today: wrongly urgent on ~0 ROAS).
- Same age, expensive cost-per-install → flagged.
- Install account with no install-cost target → degrades to keep / abstain, no crash.
- Regression: ROAS-account steady-state rows unchanged.

## Provenance

Consolidation of two overlapping review-spawned backlog tickets
(`monitor-steady-state-install-goal-classification` + `steady-state-watch-path-goal-aware-installs`), both
carved out of `goal-aware-grounding-other-producers` (monitor was deliberately left ROAS-only there because
its steady-state sample already agrees with its ROAS metric; this classification gap is the separate,
larger feature). This is the last open corner of the goal-awareness arc.
