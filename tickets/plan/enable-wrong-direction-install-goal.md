description: The new "you're turning on a known loser" warning only works for revenue-goal accounts; for app-install accounts, turning on an ad whose install cost is above the account's target still slips through without a warning.
prereq: enable-wrong-direction-refutation, review-gate-install-goal-direction
files: src/meta_ads_analysis/review.py, src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py
----
## Problem

`enable-wrong-direction-refutation` adds a direction-refutation for re-enabling an ad whose cited ROAS
is below the account `target_roas`, but it is **ROAS-only** by deliberate scope: the enable op now
carries `action_type == "enable_ad"`, yet `review._direction_contradiction` fires the enable branch
only when `primary_goal == "roas"`, there is a numeric `target_roas`, and the cited metric is
`blended_roas`.

Install-goal accounts (`primary_goal == "maximize_in_app_subscriptions"`) ground their enable on
`cost_per_app_install` (lower-is-better, the opposite polarity from ROAS). So enabling an ad whose
cited cost-per-install is **above** the account's cost target is the same wrong-direction
self-contradiction, but it is not refuted today. It is partially mitigated (install enables cap at the
`low` band on the purchase-conversion sample), but it is not *actively* refuted the way a below-target
ROAS enable is.

## Why it depends on `review-gate-install-goal-direction`

That ticket introduces the cost-per-install polarity comparison into `_direction_contradiction` for
**scale/pause** actions (cost above target → refute scale; cost comfortably below target → refute
pause). Once that polarity machinery exists, the enable branch should reuse it: an install-goal enable
whose cited `cost_per_app_install` is above target → `refuted`, with an enable-specific reason. Building
it before that ticket lands would duplicate the cost-polarity logic.

## Expected behavior

- Install-goal account, numeric cost-per-install target, cited `metric_name == "cost_per_app_install"`,
  `action_type == "enable_ad"`: a re-enable whose cited cost-per-install is **above** target →
  `refuted` (enabling a known loser), reusing the install-goal cost-polarity comparison.
- ROAS-goal enable behavior unchanged.
- Conservative: fire only with a numeric target and the matching cited cost metric; otherwise stay
  silent.

## Acceptance criteria

- The enable branch of `_direction_contradiction` (or its install-goal sibling) refutes install-goal
  above-target enables with a clear, input-naming reason.
- Tests: install-goal above-target enable → refuted; install-goal at-/below-target enable → stands;
  missing target → no fire; ROAS enable behavior unchanged.
