description: The automatic second-opinion check that catches "you're scaling something that's actually losing money" only works for revenue-goal accounts; this extends it to app-install accounts so a recommendation that contradicts its own install cost is also caught.
prereq:
files: src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Problem

`review._direction_contradiction` (`src/meta_ads_analysis/review.py:398`) only fires for ROAS-goal
accounts. It requires `policy["primary_goal"] == "roas"`, a numeric `target_roas`, and a cited
`metric_name == "blended_roas"`, then refutes:

- a scale (`increase_*_budget`, `consider_scale_budget`) whose cited ROAS is **below** target,
- an enable (`enable_ad`) whose cited ROAS is **below** target,
- a budget cut (`decrease_*_budget`) or pause (`pause_ad`) whose cited ROAS is comfortably (≥1.5×)
  **above** target.

Install-goal accounts (`primary_goal == "maximize_in_app_subscriptions"`, e.g. `pollen_sense`) get
**no** direction refutation. Their recommendations rest on `cost_per_app_install` — the *opposite
polarity* (lower is better). So the gate today will not refute a scale on an ad whose cost-per-install
is above target (scaling a loser), nor a pause on an ad whose cost-per-install is comfortably below
target (pausing a winner). This was a deliberate conservative skip at ship time
(`tickets/complete/5-adversarial-review-gate.md`, "Known gaps"); the policy already carries the
needed target. This ticket closes it.

## What the data supports

The install-goal policy (`config/meta_ads_accounts.json`, `pollen_sense.action_policy`) carries:

- `secondary_cost_per_app_install_target` (e.g. `3.0`) — the cost-per-install target. This is the
  faithful mirror of `target_roas` (an explicit goal target), and is the field this check uses.
- `pause_if_no_primary_and_secondary_cost_above` (e.g. `3.0`) — a *pause threshold*, the analogue of
  `pause_roas_floor`. The ROAS direction check does NOT key off `pause_roas_floor`, so for symmetry the
  install check does NOT key off this field either. (See "Design decision" below.)

The metric an install-goal action cites is `cost_per_app_install` with `metric_value` = the cost
(`actions._select_action_metric` at `src/meta_ads_analysis/actions.py:625`; the control-plane mirror is
`control._status_metric`). So a cost-polarity check is implementable from data already on the
action + policy.

## Design

Add an install-goal branch to the `direction` check, polarity-inverted from the ROAS branch.

Recommended shape — split `_direction_contradiction` into a dispatcher plus two pure sibling helpers
so neither polarity's logic is tangled with the other:

```python
_INSTALL_GOAL = "maximize_in_app_subscriptions"

def _direction_contradiction(*, action, evidence, policy) -> str | None:
    goal = policy.get("primary_goal")
    if goal == "roas":
        return _roas_direction_contradiction(action=action, evidence=evidence, policy=policy)
    if goal == _INSTALL_GOAL:
        return _install_direction_contradiction(action=action, evidence=evidence, policy=policy)
    return None
```

`_roas_direction_contradiction` is the existing body, moved verbatim (no behavior change).

`_install_direction_contradiction` (cost-per-install, lower-is-better):

- Read `target = _num(policy.get("secondary_cost_per_app_install_target"))`. If `None` (or `<= 0`,
  a nonsensical config), return `None` — never refute on a missing/ambiguous target.
- Require `evidence.get("metric_name") == "cost_per_app_install"` and a numeric
  `cost = _num(evidence.get("metric_value"))`; else return `None`.
- `action_type = str(action.get("action_type") or "")`.
- Scale-up — `action_type in _SCALE_ACTIONS` and `cost > target` → refute (scaling a loser). Strict
  `>` mirrors the ROAS scale branch's strict `<` (at-target is not refuted).
- Enable — `action_type in _ENABLE_ACTIONS` and `cost > target` → refute, with an "enabling" reason
  (mirrors the ROAS enable branch's distinct vocabulary).
- Pause / budget-cut — `action_type == "pause_ad"` or `action_type in _SCALE_DOWN_BUDGET_ACTIONS` and
  `cost <= target / _PAUSE_WINNER_MARGIN` → refute (pausing/cutting a winner). The inverted margin:
  ROAS fires at `roas >= target * 1.5`; the lower-is-better mirror is `cost <= target / 1.5`. Keep
  `<=` so the boundary is inclusive on the same side ROAS's `>=` is.

Reason strings should name the input the same way the ROAS branch does — e.g.:

- scale: `"recommendation contradicts its cited metric vs the account goal: scaling an entity whose
  cost/install $X.XX is above the $Y target"`
- enable: `"...enabling an ad whose cost/install $X.XX is above the $Y target"`
- pause: `"...pausing an entity whose cost/install $X.XX is comfortably below the $Y target"`
- budget cut: `"...cutting the budget of an entity whose cost/install $X.XX is comfortably below the
  $Y target"`

Format the cost as `${cost:.2f}` and the target as `${target:g}` (mirrors the `:g` ROAS target).

The verdict path is unchanged: the check returns a reason string, `review_recommendation` wraps it as
a `VERDICT_REFUTED` `_Finding` with `failed_input="direction"`, and `_resolve` / `_apply_verdict` /
`_apply_op_verdict` carry it through exactly as they do for ROAS. Refuted is a warning, not a band-cap
(it never sets `revised_band`).

### Design decision (settled — do not re-open)

Use `secondary_cost_per_app_install_target` as the sole target source; treat a missing/non-positive
value as no-fire. Rejected alternative: falling back to `pause_if_no_primary_and_secondary_cost_above`.
Rationale: the ROAS branch keys the direction check off `target_roas` (the goal target), not
`pause_roas_floor` (the pause threshold). `secondary_cost_per_app_install_target` is the goal-target
analogue; `pause_if_no_primary_and_secondary_cost_above` is the pause-threshold analogue. Keying off
the goal target keeps the two polarities symmetric and the check conservative. (Both fields are `3.0`
in the current config, so this choice is invisible in production today — it matters only for clarity
and future configs where they diverge.)

## Edge cases & interactions

- **Missing target** (`secondary_cost_per_app_install_target` absent / non-numeric / `<= 0`) → no
  fire. This is the guard that keeps the check conservative and keeps existing install-goal tests
  green when their policy carries no cost target.
- **Cold ad / no metric** — `cost_per_app_install` is `None` (zero installs) → `metric_value` is
  `None` → no fire (the same way the ROAS branch no-ops on a `None` ROAS).
- **Wrong cited metric** — an install-goal account whose action somehow cites `blended_roas` → the
  `metric_name == "cost_per_app_install"` guard means no fire (don't reason over the wrong polarity).
- **At-target boundary** — scale/enable at exactly `cost == target` stands (strict `>`); pause/cut at
  exactly `cost == target / 1.5` is refuted (inclusive `<=`). Pin both with boundary tests so a future
  `>=`/`<` slip can't silently change behavior.
- **Other goals / no goal** — `primary_goal` neither `"roas"` nor `maximize_in_app_subscriptions`
  (or `policy == {}`) → dispatcher returns `None`. Rotation/most ops carry no `action_type`, so even
  on an install-goal account the branch no-ops for them (`action_type == ""` matches no set).
- **All three plan surfaces** — the check is shared by `review_action_plan` (ad actions),
  `_review_plan_ops` (budget + enable ops), and `review_rotation_plan` (rotation items pass
  `action=item` with no `action_type`). Adding the install branch lights up install-goal direction
  refutation across action plans AND control/enable ops with no extra wiring. Confirm the budget-op
  and enable-op paths fire (they set `action_type`).
- **Demote-only invariant** — refuted only flips `executable`/`status` down and appends a factor; it
  never raises a band or promotes a status. The install branch must not regress this (it doesn't — it
  reuses the same `_Finding` → `_apply_*` machinery).
- **ROAS unchanged** — moving the ROAS body into `_roas_direction_contradiction` must be a pure
  refactor; all existing ROAS direction tests must stay green untouched.

## Tests (extend `tests/test_meta_ads_analysis.py`)

Mirror the existing `test_review_direction_contradiction_refutes` /
`test_review_scale_below_target_refutes` style (build `evidence` via `_review_evidence` with
`metric_name="cost_per_app_install"` and a numeric `metric_value`, `assess` the band, call
`review_recommendation` with `policy={"primary_goal": "maximize_in_app_subscriptions",
"secondary_cost_per_app_install_target": 3.0}`):

- **install scale above target → refuted** — `consider_scale_budget` / `increase_adset_budget`,
  `metric_value` (cost) `= 5.0 > 3.0` → `verdict == "refuted"`, `"direction" in failed_inputs`,
  reason names `"above the $3 target"`, `revised_band is None`.
- **install pause below target → refuted** — `pause_ad`, cost `= 1.5 <= 3.0/1.5 = 2.0` → refuted,
  reason names `"comfortably below"`.
- **install budget-cut below target → refuted** — `decrease_adset_budget`, cost `= 2.0` (== the 2.0
  margin boundary) → refuted (inclusive `<=` boundary pin).
- **install scale agreeing with target → stands** — `consider_scale_budget`, cost `= 2.0 < 3.0`
  (a genuine winner) → no direction finding.
- **install pause agreeing with target → stands** — `pause_ad`, cost `= 5.0` (a loser worth pausing)
  → no direction finding.
- **install scale at-target → stands** — `increase_adset_budget`, cost `= 3.0` exactly (strict `>`
  pin).
- **install missing target → no fire** — same `pause_ad`/`consider_scale_budget` but policy has no
  `secondary_cost_per_app_install_target` → no direction finding (the conservative guard).
- **install enable above target → refuted** — exercise the enable-op path: `build_enable_ads_plan`
  with `policy={"primary_goal": "maximize_in_app_subscriptions",
  "secondary_cost_per_app_install_target": 3.0}` and an ad whose computed `cost_per_app_install` is
  above 3.0 → op `review.verdict == "refuted"`, `"direction" in failed_inputs`, reason says
  "enabling".
- **ROAS regression sweep** — the existing ROAS direction tests must remain green unmodified.
- **Update `test_enable_ads_install_goal_not_direction_refuted`** (line ~3353): its comment ("install
  goal direction polarity is deferred to a follow-up") is now stale. The test's policy carries only
  `target_roas` (no `secondary_cost_per_app_install_target`), so it still asserts the correct *new*
  behavior — an install enable with no configured cost target is NOT refuted. Update the comment to
  reflect that it now pins the **missing-cost-target → no fire** guard (not a blanket "ROAS-only"
  deferral), and add a sibling test where the policy DOES carry
  `secondary_cost_per_app_install_target` and the cost is above it → refuted.

## Docs

Update the now-stale "deferred to a follow-up" notes:

- `src/meta_ads_analysis/review.py` — the `_direction_contradiction` docstring (line ~406, "install
  goal direction is intentionally not judged here") and the related module/section comments.
- `docs/META_ACTION_WORKFLOW.md` lines ~205-206 ("ROAS-only for now … deferred to a follow-up") and
  the surrounding enable/direction description — document the cost-polarity install branch.

## Acceptance criteria

- `_direction_contradiction` refutes install-goal scale/enable-above-target and pause/cut-below-target
  contradictions, with a clear, input-naming reason; ROAS behavior is byte-for-byte unchanged.
- Conservative throughout: fires only with a numeric `secondary_cost_per_app_install_target` and a
  cited `cost_per_app_install` metric; otherwise silent.
- All tests above pass; `python -m pytest tests/test_meta_ads_analysis.py` is green (stream with
  `2>&1 | tee /tmp/pytest.log`).
- The "Known gaps" note in `tickets/complete/5-adversarial-review-gate.md` is satisfied.

## TODO

- [ ] Refactor `_direction_contradiction` into a goal-dispatcher + `_roas_direction_contradiction`
      (verbatim move) + new `_install_direction_contradiction`; add `_INSTALL_GOAL` constant.
- [ ] Implement the cost-polarity branch (scale/enable above target; pause/cut below
      `target / _PAUSE_WINNER_MARGIN`); reuse `_PAUSE_WINNER_MARGIN`, `_SCALE_ACTIONS`,
      `_ENABLE_ACTIONS`, `_SCALE_DOWN_BUDGET_ACTIONS`.
- [ ] Add the install-goal direction tests and the enable-op test; update
      `test_enable_ads_install_goal_not_direction_refuted` and its comment.
- [ ] Update the `review.py` docstring/comments and `docs/META_ACTION_WORKFLOW.md`.
- [ ] Run the test suite (foreground, `tee`); confirm ROAS tests untouched and green.
