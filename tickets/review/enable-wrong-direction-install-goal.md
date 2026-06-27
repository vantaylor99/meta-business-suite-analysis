description: Verify the two new winner-side tests for the install-goal enable direction gate are correct and complete.
prereq:
files: tests/test_meta_ads_analysis.py
difficulty: easy
----
## Summary

This was a test-only completion ticket. Production code (`_install_direction_contradiction` in
`src/meta_ads_analysis/review.py`) was already live via the `review-gate-install-goal-direction` prereq.
The ticket's only job was adding the two missing polarity-mirror tests for the "good ad stays trusted"
side of the install-goal enable check.

## What was added

Two new tests placed immediately after `test_enable_ads_install_goal_above_cost_target_is_refuted`
(≈ line 3619 in the updated file):

1. **`test_enable_ads_install_goal_at_target_cost_enable_stands`** — strict boundary: spend $120,
   40 installs → cost/install = $3.00, target $3.00. Branch is `cost > target` (strict), so exactly on
   the threshold must stand. Asserts `verdict == "stands"` and `"direction" not in failed_inputs`.

2. **`test_enable_ads_install_goal_below_target_cost_enable_stands`** — clear winner: spend $80,
   40 installs → cost/install = $2.00, target $3.00. Asserts same conditions.

Both tests pin `metric_name == "cost_per_app_install"` explicitly to guard against silent selector drift.
Both use 40 installs (same as the refute test) so the conversions floor is cleared and the direction
outcome — not the sample-floor — is what's observed.

## No production code was touched

`review.py` was not modified. The full test suite (363 tests) is green.

## Use cases for reviewer testing

- Boundary regression: change `>` to `>=` in `_install_direction_contradiction` — the at-target test
  must flip to `refuted`, confirming the guard catches that slip.
- Winner coverage: any change that causes cost/install < target to refute must be caught by the
  below-target test.
- Existing refute test and all ROAS/scale/pause/budget-cut paths are untouched and green.

## Known gaps / flags for reviewer

None. This was narrow and self-contained — two assertions over an already-shipped branch.
