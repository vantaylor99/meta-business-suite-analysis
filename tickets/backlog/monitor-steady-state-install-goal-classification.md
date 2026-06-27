description: The daily "watch" scan that flags underperforming ads grades every ad on return-on-ad-spend (ROAS). For app-install accounts — which earn installs, not direct purchase revenue — a healthy mature ad can show near-zero ROAS and get wrongly flagged as urgent. Investigate whether the watch scan should grade install-goal accounts on install cost instead.
files: src/meta_ads_analysis/monitor.py, tests/test_meta_ads_analysis.py
----

## Concern

`monitor.classify_ad` (the steady-state watch path behind `build_watch_report`) is ROAS-centric: it
classifies urgent / underperforming / ok purely against `roas_floor` / `roas_target` and derives
`dollars_at_risk` from ROAS. For a `maximize_in_app_subscriptions` (app-install) account, a healthy
mature install ad books ~0 purchase ROAS **by design**, so once it ages past the early-life grace
window it could be classified `urgent`/`underperforming` on a metric that does not describe its goal —
a potential false-positive pause signal.

This is deliberately **out of scope** for `goal-aware-grounding-other-producers`, which fixed only the
*significance-sample* mismatch on the authoring/rotation write paths. monitor's steady-state sample
(`purchases`) already AGREES with its steady-state metric (ROAS), so it has no sample/metric mismatch
bug — the gap here is a different, larger one: goal-aware *classification* (metric + thresholds), not
sample grounding.

## What is already handled (do not duplicate)

monitor is **partly** goal-aware already: the early-life forced-decision path routes
`goal_kind(policy) == "install"` to `_forced_decision_install`, which grades the young ad on
cost-per-install and grounds the sample on installs (`monitor.py:550`). So brand-new install ads are
covered; the open question is only the **mature** (post-early-life) steady-state watch.

## To investigate before designing

- Do install-goal accounts actually run `build_watch_report` in practice, or is the watch scan
  ROAS-account-only by deployment convention? (If the latter, this may be a documentation fix, not a
  code change.)
- If install accounts do run it: should `classify_ad` gain a goal-aware cost-per-install
  floor/target path (mirroring `_forced_decision_install`), and what policy keys supply those bars
  (`pause_if_no_primary_and_secondary_cost_above`, etc.)?
- Expected behavior: a healthy mature install ad (good cost-per-install, ~0 ROAS) should NOT be
  flagged urgent on an install-goal account; a genuinely expensive-per-install ad still should be.

## Provenance

Carved out of `goal-aware-grounding-other-producers` (the monitor decision documented there: monitor
stays ROAS/spend-only because its steady-state sample already agrees with its ROAS metric; this
deeper classification gap is a separate feature).
