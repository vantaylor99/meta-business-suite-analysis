description: The automatic second-opinion check that catches "you're scaling something that's actually losing money" only works for revenue-goal accounts; for app-install accounts it never fires, so a recommendation that contradicts its own install cost can still slip through.
prereq:
files: src/meta_ads_analysis/review.py, src/meta_ads_analysis/briefs.py, config/meta_ads_accounts.json, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Problem

The adversarial review gate's **direction** check (`review._direction_contradiction`) only fires for
ROAS-goal accounts: it requires `policy["primary_goal"] == "roas"`, a numeric `target_roas`, and a
cited `metric_name == "blended_roas"`. It refutes a scale whose cited ROAS is below target, and a
pause whose cited ROAS is comfortably (≥1.5×) above target.

Install-goal accounts (`primary_goal == "maximize_in_app_subscriptions"`, e.g. `pollen_sense`) get
**no** direction refutation. Their recommendations rest on `cost_per_app_install` (lower is better —
the opposite polarity from ROAS). So today the gate would NOT refute:

- a `consider_scale_budget` / `increase_adset_budget` on an ad whose cited cost-per-install is **above**
  the account's cost target (scaling a loser), or
- a `pause_ad` on an ad whose cited cost-per-install is comfortably **below** target (pausing a winner).

This was a deliberate conservative skip when the gate shipped (`adversarial-review-gate`) — better to
not fire than to fire with the wrong polarity — but it leaves roughly half the managed accounts
without the self-contradiction safety check.

## What the data supports

Install-goal account policy already carries the needed targets (see
`config/meta_ads_accounts.json`):

- `secondary_cost_per_app_install_target` (e.g. `3.0`)
- `pause_if_no_primary_and_secondary_cost_above` (e.g. `3.0`)

The metric the action cites for an install-goal account is `cost_per_app_install`
(`actions._select_action_metric`). So a cost-polarity direction check is implementable from data
already on the action + policy.

## Expected behavior

- For an install-goal account with a numeric cost-per-install target and a cited
  `metric_name == "cost_per_app_install"`:
  - a scale action whose cited cost-per-install is **above** target → `refuted` (scaling a loser);
  - a pause action whose cited cost-per-install is comfortably **below** target → `refuted`
    (pausing a winner), using a margin analogous to `_PAUSE_WINNER_MARGIN` but inverted for the
    lower-is-better polarity.
- ROAS-goal behavior is unchanged.
- Conservative throughout: fire only with a numeric target and the matching cited cost metric;
  otherwise stay silent (never refute on a missing/ambiguous target).

## Relationship to other work

- Related to `confidence-install-goal-significance` (backlog): that ticket fixes how the *confidence
  band* is grounded for install accounts (conversions floor counts purchases, not installs). This
  ticket is about the *direction* refutation, a separate check in the same gate. They can land
  independently, but doing the significance work first gives install-goal evidence blocks a sounder
  sample to reason over.
- The gate itself, its checks, and the conservative-skip rationale are documented in `review.py` and
  `docs/META_ACTION_WORKFLOW.md`.

## Acceptance criteria

- `_direction_contradiction` (or a sibling) refutes install-goal scale-above-target and
  pause-below-target contradictions with a clear, input-naming reason.
- Tests cover: install-goal scale-above-target → refuted; install-goal pause-below-target → refuted;
  install-goal call that agrees with its cost target → stands; missing target → no fire.
- The "Known gaps" note in the completed `adversarial-review-gate` review record is satisfied.
