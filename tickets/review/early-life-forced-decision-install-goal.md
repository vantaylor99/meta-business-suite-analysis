description: The day-3 keep-or-kill check for new ads on an install-focused account now grades the ad on cost-per-install instead of purchase return-on-spend, so a healthy install ad is no longer wrongly flagged for pausing.
files: src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/monitor.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What was wrong (and is now fixed)

`monitor._early_life_forced_decision` grades a probated ad's own life-to-date window with
`classify_ad(...)`, which is **ROAS-only**. On an install-goal account
(`primary_goal == "maximize_in_app_subscriptions"`) an install ad books ~0 `purchase` actions by
design, so its ROAS reads ~0. Once such an ad hit the decision age (day 3) **and** had spent ≥
`min_spend`, `classify_ad` returned `urgent` (ROAS below the pause floor), which the forced decision
mapped to `pause_candidate` — even when cost-per-install was excellent.

The fix makes the day-3 own-sample grade **goal-aware**: ROAS accounts still use `classify_ad`
(unchanged); install accounts grade the own window on **cost-per-install** against the account's
install-cost target.

## What changed

### `early_triage.py` — new public goal-aware own-sample classifier
- `OWN_SAMPLE_INSUFFICIENT` / `OWN_SAMPLE_KEEP` / `OWN_SAMPLE_PAUSE` constants and the
  `OwnSampleVerdict` dataclass (`verdict`, `kind`, `metric_name`, `metric_value`, `target`,
  `results`, `reasons`).
- `classify_own_sample(*, spend, purchase_value, purchases, app_installs, policy, roas_floor,
  roas_target, min_spend) -> OwnSampleVerdict`. Below `min_spend` → `OWN_SAMPLE_INSUFFICIENT` (caller
  defers to analogs). Above it, it builds a `_Sums` and reuses the existing
  `_goal_kind`/`_goal_thresholds`/`_GoalProfile`/`_is_struggling`/`_metric_value`/`_metric_name`
  helpers, so the install bar ("cost/install > target, or 0 installs on the spend") is **identical**
  to the analog engine's — no threshold numbers duplicated. `non_trivial_spend` is pinned to
  `min_spend` so the `spend >= min_spend` gate is the single significance threshold (no second,
  conflicting floor). Install account with no target install cost in policy → `OWN_SAMPLE_INSUFFICIENT`
  (defers to the analog path, which already degrades such accounts to keep).
- Public `goal_kind(policy)` wrapper (thin alias for the private `_goal_kind`) so the monitor branches
  without importing a private symbol.

### `monitor.py` — goal-aware forced-decision branch
- The own-sample-insufficient tail of `_early_life_forced_decision` was extracted into
  `_forced_decision_analog(...)` (returns `(row, close_action)`), shared by both the ROAS and install
  branches — behavior identical to before for ROAS.
- `_early_life_forced_decision` now branches on `goal_kind(policy)`: ROAS → the existing `classify_ad`
  path (byte-for-byte unchanged); install → `_forced_decision_install(...)`.
- `_forced_decision_install(...)` calls `classify_own_sample(...)` and maps:
  - `OWN_SAMPLE_INSUFFICIENT` → `_forced_decision_analog(...)` (analog verdict governs);
  - `OWN_SAMPLE_PAUSE` → `pause_candidate` row, `dollars_at_risk = round(spend, 2)`;
  - `OWN_SAMPLE_KEEP` → `watch`/`keep` row, `dollars_at_risk = 0.0`.
  It builds an install-flavored `Evidence(metric_name="cost_per_app_install", …)` (installs passed as
  `sample_purchases`) and a **direct-observation** confidence via `confidence.assess(...)` — `assess`
  is metric-agnostic, so installs-as-conversions yields the same `direct_observation` banding the ROAS
  branch produces. The `close_action` is always returned (a probation is owed a decision).

## How to validate / test cases (added, all passing)

Run: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -k "watch and (install or day3 or probation)"`
(8 selected, 8 pass). Full module: 346 pass. (No mypy/pyright/ruff is installed in this repo — pytest
is the only gate; `python -m py_compile`/import of both modules is clean.)

New tests (the `_watch_insight` helper gained an `installs=` param that emits a `mobile_app_install`
action — an `APP_INSTALL_KEYS` type):
- `test_watch_day3_probation_install_goal_cheap_installs_keep_and_close` — **the reported bug**:
  age-3 install ad on probation, `spend=$300`, 0 purchases, 200 installs → cost/install $1.50 ≤ $3.00
  → `classification="watch"`, `verdict="keep"`, `evidence.metric_name=="cost_per_app_install"`,
  `confidence.grounding_tier=="direct_observation"`, exactly one `close`. Explicitly asserts it is no
  longer `pause_candidate`.
- `test_watch_day3_probation_install_goal_expensive_installs_pauses_and_closes` — `spend=$300`, 50
  installs → cost/install $6.00 > $3.00 → `pause_candidate`, `direct_observation`, one `close`.
- `test_watch_day3_probation_install_goal_below_floor_analog_governs` — `spend=$60` (< `min_spend`
  100) → `OWN_SAMPLE_INSUFFICIENT` → analog path. Recovering install analogs → `keep_watch`
  (`correlational` grounding); non-recovering → `pause_candidate`. Either way the follow-up closes.
- **Regression guard**: the three existing ROAS forced-decision tests
  (`test_watch_day3_probation_own_sample_clears_floor_keep_and_close`,
  `…below_floor_pauses_and_closes`, `…still_below_floor_analog_governs`) still pass unchanged.

## Reviewer focus / things worth a hard look

- **Confidence banding for the keep case is `high`.** With `spend=$300 ≥ min_spend` and `installs=200
  ≥ 4 × CONFIDENCE_CONVERSIONS_FLOOR (25)`, `assess` lands the data band at `high`, so the combined
  band reads `high`. This is consistent with how the ROAS branch bands a strong own sample (its data
  axis is also driven by the conversion count), but a reviewer may want to sanity-check whether
  treating raw install count as the conversion-count signal for the `≥4×floor → high` knee is the
  intended grounding for installs (it is *quantity* of installs, not their quality). The tests only
  assert `grounding_tier == "direct_observation"` and `band != "abstain"`, not the exact band, so this
  is a judgment call left visible rather than pinned.
- **`dollars_at_risk` on an install pause = whole window spend.** `_dollars_at_risk` is ROAS-based and
  doesn't apply to installs, so the install pause uses `round(spend, 2)` and the keep uses `0.0`. The
  field is informational and has no test assertion; confirm that's acceptable for any downstream
  renderer/consumer that reads `dollars_at_risk` on install rows.
- **`reasons` wording** in `classify_own_sample` / `_forced_decision_install` is new prose; verify it
  reads correctly in the operator-facing row (e.g. "cost/install $6.00 over the $3.00 target on $300").

## Known gap — OUT OF SCOPE (file a follow-up)

The **normal (non-early-life) watch path** in `monitor.build_watch_report` (around
`src/meta_ads_analysis/monitor.py:646`) still calls `classify_ad` with ROAS for **every** ad,
including install-goal accounts. So a mature install ad (older than `early_life_max_age` and not on
probation) is still graded ROAS-only there — the same class of bug as this ticket, but on the
steady-state path. This predates this bug and is a larger change (making the whole watch path
goal-aware, not just the day-3 forced decision). It was deliberately left alone here per the ticket's
scope. **Recommend filing a `backlog/` ticket** for "make the steady-state watch path goal-aware for
install accounts."
