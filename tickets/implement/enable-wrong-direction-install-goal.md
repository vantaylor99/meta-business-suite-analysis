description: Add the missing test that proves turning on a genuinely cheap app-install ad is NOT flagged as a known loser — the warning code already exists, but nothing yet pins the "good ad stays trusted" side of it.
prereq:
files: src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py
difficulty: easy
----
## Context — the production code already landed

This ticket was planned on the assumption that the install-goal *enable* direction-refutation still had
to be built on top of `review-gate-install-goal-direction`. During planning it turned out the prereq
already shipped it: `review-gate-install-goal-direction` reused the `_ENABLE_ACTIONS` constant (added by
`enable-wrong-direction-refutation`) inside `_install_direction_contradiction`. So the refutation is
**live in production code today**:

`src/meta_ads_analysis/review.py` `_install_direction_contradiction` (≈ lines 466–508):

```python
target = _num(policy.get("secondary_cost_per_app_install_target"))
if target is None or target <= 0:
    return None
if evidence.get("metric_name") != "cost_per_app_install":
    return None
cost = _num(evidence.get("metric_value"))
if cost is None:
    return None
action_type = str(action.get("action_type") or "")
...
if action_type in _ENABLE_ACTIONS and cost > target:          # <-- the enable branch this ticket wanted
    return (
        f"recommendation contradicts its cited metric vs the account goal: enabling an ad "
        f"whose cost/install ${cost:.2f} is above the ${target:g} target"
    )
```

The dispatcher `_direction_contradiction` routes `primary_goal == "maximize_in_app_subscriptions"` to
this sibling. The branch uses strict `cost > target` (the scale-up polarity), names its inputs, and is
gated on a numeric positive `secondary_cost_per_app_install_target` + a cited `cost_per_app_install`.

**Therefore: do NOT modify `review.py`.** This ticket is a test-only completion. If, while writing the
tests, you find the branch does *not* behave as described above, stop and re-open the design — but the
expectation is that all four acceptance criteria already pass except the one missing winner/boundary
test below.

## What already has test coverage

- `test_enable_ads_install_goal_above_cost_target_is_refuted` (tests, ≈ line 3592): install goal,
  `secondary_cost_per_app_install_target=3.0`, computed cost/install $12.50 > $3 → `refuted`, reason
  contains `"enabling"`, `"12.50"`, `"$3 target"`; band left at `medium` (warning, not a band-cap).
- `test_enable_ads_install_goal_no_cost_target_not_direction_refuted` (≈ line 3472): install goal with
  only a `target_roas` and **no** cost target → check has nothing to fire against → `stands`.
- ROAS enable behavior (`test_enable_ads_below_target_roas_strong_sample_is_refuted`,
  `..._exactly_at_target_roas_stands`, `..._above_target_roas_stands`,
  `..._roas_goal_without_target_does_not_refute`) — unchanged, all green.

## The gap to close

There is **no** test for the winner side of the install enable check: an install-goal enable with a
cost target *configured* whose cited cost-per-install is **at or below** target must **stand** (NOT be
refuted). This is the polarity mirror of `test_enable_ads_exactly_at_target_roas_stands`, and it guards
against a future `>` → `>=` slip silently starting to refute genuinely cheap re-enables.

Add (names indicative):

- `test_enable_ads_install_goal_at_target_cost_enable_stands` — strict-`>` boundary. Build an enable op
  whose computed cost/install lands **exactly on** the configured target (e.g. spend $120 / 40 installs
  = $3.00, `secondary_cost_per_app_install_target=3.0`). Assert `op["action_type"] == "enable_ad"`,
  `op["evidence"]["metric_name"] == "cost_per_app_install"`, `op["evidence"]["metric_value"] == 3.0`,
  `op["review"]["verdict"] != "refuted"` (expect `"stands"`), and `"direction" not in
  op["review"]["failed_inputs"]`.
- `test_enable_ads_install_goal_below_target_cost_enable_stands` — a clear winner (e.g. spend $80 / 40
  installs = $2.00 < $3 target). Same assertions; cost well under target → stands, not refuted.

Use the existing `_enable_client(insights)` helper and the `mobile_app_install` action shape from
`test_enable_ads_install_goal_above_cost_target_is_refuted` (40 installs clears the conversions floor so
the verdict is `stands`, not `insufficient` — keep the sample at/above that so the floor never masks the
result you are asserting).

## Edge cases & interactions

- **Floor must not mask the verdict.** Keep the install sample at or above the count used in the refute
  test (40 installs) so the band clears `abstain`/below-floor. A below-floor sample would yield
  `insufficient` (rank 3 > refuted/stands) and the test would assert the wrong thing — that path is
  already pinned by `test_enable_ads_install_goal_no_cost_target_not_direction_refuted` /
  `..._cold_ad_with_target_stays_insufficient_not_refuted`, so do not re-test it; here you specifically
  want a grounded sample so the *direction* outcome is what's observed.
- **Exactly-at-target is the live boundary.** The branch is strict `cost > target`, so cost == target
  must stand. This is the single most important assertion — it's what stops a `>=` regression.
- **Reason-string polarity.** A standing enable must not emit any direction reason; assert `"direction"`
  is absent from `failed_inputs` (don't just check the headline verdict — a future change could append a
  `direction` finding that loses the `max`-rank tiebreak yet still pollutes `failed_inputs`).
- **No production code touched** → ROAS enable, scale, pause, and budget-cut paths are untouched by
  definition; the full pre-existing sweep must stay green.
- **Cited-metric guard interaction.** These tests rely on the install-goal enable grounding on
  `cost_per_app_install` (established by `test_enable_ads_install_goal_grounds_on_cost_per_install`); if
  that selector ever changed, `metric_value` would be `None` and the direction check would silently not
  fire — so assert `metric_name == "cost_per_app_install"` explicitly in each new test.

## TODO

- [ ] Add `test_enable_ads_install_goal_at_target_cost_enable_stands` (cost == target → stands).
- [ ] Add `test_enable_ads_install_goal_below_target_cost_enable_stands` (cost < target → stands).
- [ ] Place both next to `test_enable_ads_install_goal_above_cost_target_is_refuted` (≈ line 3592) for
      readability as a refute/stands pair.
- [ ] Do NOT modify `src/meta_ads_analysis/review.py` (verify the existing branch behaves as documented;
      if it does not, halt and re-open design rather than editing).
- [ ] Run `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` — full suite must be green
      (system `python` lacks deps; only `.venv/bin/python` runs the suite). Stream with `2>&1 | tee` if
      needed.
- [ ] Hand off to review with the new test names and the "production code already landed via prereq,
      test-only completion" framing.
