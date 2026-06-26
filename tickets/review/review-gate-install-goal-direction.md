description: The automatic second-opinion check that catches "you're scaling something that's actually losing money" now works for app-install accounts too, not just revenue-goal accounts — so a recommendation that contradicts its own install cost is caught the same way.
prereq:
files: src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What shipped

The review gate's `direction` check previously fired only for ROAS-goal accounts. It now also fires
for install-goal accounts (`primary_goal == "maximize_in_app_subscriptions"`) on the opposite
polarity — cost-per-install, where **lower is better**.

`review._direction_contradiction` was refactored into a goal **dispatcher** plus two pure sibling
helpers, so neither polarity's logic is tangled with the other:

- `_direction_contradiction` — reads `policy["primary_goal"]` and routes to one sibling, returning
  `None` for every other goal (or no goal / `policy == {}`).
- `_roas_direction_contradiction` — the **verbatim** prior ROAS body (no behavior change).
- `_install_direction_contradiction` — the new cost-polarity branch (inverted from ROAS).

New module constant `_INSTALL_GOAL = "maximize_in_app_subscriptions"`.

The install branch (`src/meta_ads_analysis/review.py`):
- Target source: `policy["secondary_cost_per_app_install_target"]` (the goal-target analogue of
  `target_roas`). Missing / non-numeric / `<= 0` → **no fire** (the conservative guard). Deliberately
  does NOT fall back to `pause_if_no_primary_and_secondary_cost_above` (that is the pause-threshold
  analogue of `pause_roas_floor`, which the ROAS branch also ignores — symmetry preserved). Both are
  `3.0` in the live config today, so the choice is invisible in production.
- Cited-metric guard: requires `evidence["metric_name"] == "cost_per_app_install"` and a numeric
  `metric_value`; else no fire.
- Refutes (all reuse `_SCALE_ACTIONS` / `_ENABLE_ACTIONS` / `_SCALE_DOWN_BUDGET_ACTIONS` /
  `_PAUSE_WINNER_MARGIN`):
  - **scale-up** (`increase_*_budget`, `consider_scale_budget`) with `cost > target` — strict `>`,
    mirroring ROAS's strict `<` (at-target stands).
  - **enable** (`enable_ad`) with `cost > target` — "enabling" reason vocabulary.
  - **pause / budget-cut** (`pause_ad`, `decrease_*_budget`) with `cost <= target / 1.5` — inclusive
    `<=`, the inverted mirror of ROAS's `roas >= target * 1.5`.
- Reason strings name the input the same way the ROAS branch does; cost as `${cost:.2f}`, target as
  `${target:g}` (e.g. `"...scaling an entity whose cost/install $5.00 is above the $3 target"`).

The verdict path is unchanged: the check returns a reason string → `review_recommendation` wraps it as
a `VERDICT_REFUTED` `_Finding` with `failed_input="direction"` → `_apply_verdict` /
`_apply_op_verdict` carry it through exactly as for ROAS. Refuted is a **warning, not a band-cap**
(never sets `revised_band`). Because the check is shared, the install branch lights up across all three
plan surfaces with no extra wiring: `review_action_plan` (ad actions), `_review_plan_ops` (budget +
enable ops), `review_rotation_plan` (rotation items carry no `action_type` → no-op).

Docs updated (`docs/META_ACTION_WORKFLOW.md`): the "ROAS-only … deferred to a follow-up" enable note
(~L198–206), the budget-op direction note (~L367–373), and the no-`action_type` parenthetical (~L136)
now describe the goal-aware, two-polarity behavior. The `_direction_contradiction` docstring and the
`_ENABLE_ACTIONS` / `review_ops_plan` comments were de-staled to drop the "install not judged" wording.

## Use cases to validate

Polarity is the thing to scrutinize — lower-is-better is easy to get backwards. Boundary direction
(strict `>` for scale/enable, inclusive `<=` for pause/cut) is pinned by tests; confirm the asserts
match the intent below.

- **Scale a loser** → refuted. Install account, `consider_scale_budget` / `increase_adset_budget`,
  cited cost/install **above** target. (`test_review_install_scale_above_target_refutes`)
- **Pause/cut a winner** → refuted. `pause_ad` / `decrease_adset_budget`, cost **at or below**
  `target / 1.5`. (`test_review_install_pause_below_target_refutes`,
  `..._budget_cut_below_target_refutes_at_margin_boundary` — boundary at exactly `target/1.5 = 2.0`)
- **Scale a winner / pause a loser** → stands (agrees with goal).
  (`test_review_install_scale_agreeing_with_target_stands`, `..._pause_agreeing_with_target_stands`)
- **At-target scale** → stands (strict `>`). (`test_review_install_scale_at_target_stands`, cost == 3.0)
- **No cost target configured** → no fire (the conservative guard).
  (`test_review_install_missing_target_does_not_fire`)
- **Enable-op surface, cost target set, cost above target** → op refuted with an "enabling" reason.
  (`test_enable_ads_install_goal_above_cost_target_is_refuted`, via `build_enable_ads_plan` → 12.5 cost)
- **Enable-op surface, NO cost target (only `target_roas`)** → stands — renamed from the old
  `..._not_direction_refuted`, now pins the missing-cost-target guard, not a blanket ROAS-only skip.
  (`test_enable_ads_install_goal_no_cost_target_not_direction_refuted`)
- **ROAS regression sweep** — every pre-existing ROAS direction test is unmodified and green
  (`test_review_direction_contradiction_refutes`, `test_review_scale_below_target_refutes`,
  `test_enable_ads_below_target_roas_strong_sample_is_refuted`, `..._exactly_at_target_roas_stands`,
  `..._above_target_roas_stands`, `..._roas_goal_without_target_does_not_refute`).

Validation run (foreground, streamed): `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py`
→ **299 passed** (was 291 at HEAD; +8 new). Note: the system `python` lacks deps (`duckdb`) — the
test suite only runs under `.venv/bin/python`.

## Known gaps / where to push as a reviewer

My tests are a floor, not a ceiling. Specifically:

- **No end-to-end `review_action_plan` test on an install goal.** The install branch is exercised
  standalone (via `review_recommendation`) and on the **enable-op** surface (`review_ops_plan` via
  `build_enable_ads_plan`). The ad-action plan surface and the control **budget-op** surface
  (`control._budget_op` → `decrease_adset_budget`/`increase_adset_budget`) share the same
  `review_recommendation` core, so they are covered by transitivity — but there is no integration test
  that drives a real install-goal *action plan* or *budget op* end-to-end and asserts the demotion
  (`executable=False`, `status` approved→proposed). A reviewer wanting belt-and-suspenders could add
  one mirroring `test_review_gate_only_ever_demotes` with an install policy.
- **Pause-below-target via the control surface** is only tested through the standalone helper, not
  through an actual `set_status`=PAUSED control op carrying `action_type="pause_ad"`. Worth a glance to
  confirm the pause control op actually sets `action_type` on the install path (the ROAS pause path
  does; I did not re-verify the install pause op wiring beyond the shared core).
- **`secondary_cost_per_app_install_target` vs `pause_if_no_primary_and_secondary_cost_above`** are
  both `3.0` in `config/meta_ads_accounts.json` today, so the deliberate "use the goal target, not the
  pause threshold" decision is untested against divergent values in production data — only against
  unit-test policies. The decision is documented in the implement ticket's "Design decision (settled)"
  section; flag if you disagree with the choice rather than the code.
- **No linter run** — none is configured in the repo (consistent with the prior review-gate ticket,
  `tickets/complete/5-adversarial-review-gate.md`).

## Acceptance criteria (status)

- [x] `_direction_contradiction` refutes install scale/enable-above-target and pause/cut-below-target,
      with input-naming reasons; ROAS behavior byte-for-byte unchanged (verbatim move + green ROAS sweep).
- [x] Conservative: fires only with a numeric positive `secondary_cost_per_app_install_target` and a
      cited `cost_per_app_install`; otherwise silent.
- [x] All listed tests pass; `pytest tests/test_meta_ads_analysis.py` green (299 passed).
- [x] The "Known gaps" note in `tickets/complete/5-adversarial-review-gate.md` is closed by this work
      (the archived ticket itself is left untouched as a historical record).
