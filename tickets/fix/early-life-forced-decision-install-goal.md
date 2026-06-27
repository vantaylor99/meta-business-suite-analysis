description: On an install-focused ad account, the automatic "day-3 keep-or-kill" check can wrongly flag a healthy new ad for pausing because it grades the ad on purchase return-on-spend (which install ads don't generate) instead of on cost-per-install.
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Symptom

For an **install-goal** account (`primary_goal == "maximize_in_app_subscriptions"`), the early-life
day-3 forced decision can flag a perfectly healthy new ad as an early **pause candidate**.

## Root cause

`monitor._early_life_forced_decision` takes an "own sample clears the significance floor" shortcut
that calls `classify_ad(spend=..., roas=m.get("roas"), results=m.get("purchases"), ...)`. `classify_ad`
is **ROAS-only** — it has no notion of the account goal. So when a probated ad reaches the decision age
(`age >= EARLY_LIFE_DECISION_AGE`, default day 3) **and** has spent at least `min_spend` in the watch
window (so `classify_ad` does not return `insufficient`), the forced decision is made on purchase ROAS:

- An install-goal ad books few/zero `purchase` actions by design, so `m["roas"]` is ~0.
- ~0 ROAS is below the pause floor → `classify_ad` returns `urgent` → the forced decision maps that to
  `pause_candidate` and closes the probation follow-up.

This happens even when the ad's **cost-per-install is excellent**. The branch deliberately overrides
the grace window, so the ad does not get the usual young-ad protection either — a healthy install ad
that merely spent fast in its first three days is force-flagged for pausing.

The data needed to do this correctly is already present: `fetch_entity_metrics` returns
`app_installs` and `cost_per_app_install` in the same window row (`m`), and the early-life engine
(`early_triage.triage_ad`) is already goal-aware (`_goal_kind` / `_goal_thresholds` /
`cost_per_app_install`). Only the forced-decision "own sample" shortcut bypasses it.

## Scope / when it bites

- Only **install-goal** accounts. ROAS-goal accounts are unaffected (the ROAS shortcut is correct
  there).
- Only at/after the decision age **and** only when the ad has cleared `min_spend` in the watch window
  (a low-spend install ad falls through to the goal-aware analog path, which is already correct — see
  `test_watch_day3_probation_still_below_floor_analog_governs`).
- Note the broader context: the watch monitor's *normal* (non-early-life) path is also ROAS-centric
  for install accounts — `build_watch_report` calls `classify_ad` with ROAS for every ad. That is a
  pre-existing limitation, but the early-life forced decision makes it sharper because it (a) overrides
  the grace protection and (b) is sold as a goal-aware triage. Decide whether the fix is local to the
  forced-decision shortcut or whether the monitor's own-sample evaluation should become goal-aware more
  broadly.

## Expected behavior

At the decision age, an install-goal ad whose own sample is strong on the **install** metric
(cost-per-install at/below the policy target) should be **kept**, not force-flagged as a pause
candidate. The forced keep/kill must use the account goal's metric — either by routing the install-goal
own-sample decision through the goal-aware engine, or by computing a goal-aware own-sample verdict from
`m["cost_per_app_install"]` against the install target — consistent with `early_triage`'s thresholds.

## Reproduction (suggested test)

Add an install-goal forced-decision test mirroring `test_watch_day3_probation_own_sample_clears_floor_keep_and_close`:
an age-3 install ad on probation, spend ≥ `min_spend`, **zero purchases but cheap installs**
(cost-per-install below the `secondary_cost_per_app_install_target`). Today it returns
`pause_candidate`; it should return a keep. Fixtures `_install_ad` / `_INSTALL_POLICY` already exist in
`tests/test_meta_ads_analysis.py`.
