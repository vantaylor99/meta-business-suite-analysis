description: For app-install accounts, the go-live (duplicate-ad authoring) and audience-rotation paths now count installs — not purchases — when grading how confident a recommendation is, so a decision backed by real install volume no longer reads as low confidence. Review that this change matches what the action-plan and enable/budget paths already do.
prereq: confidence-install-goal-significance-ops
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What landed

Two grounded **write** producers were still grounding their significance sample on the **purchase**
count unconditionally, even though they already pick their **metric** goal-aware (both reuse
`control._status_metric`). For a `maximize_in_app_subscriptions` account — which produces ~0 purchases
but real `app_installs` — that mismatch pinned the confidence band at `low`/`abstain` even when the
decision was backed by real install volume. The fix makes the **sample** goal-aware too, by reusing
the already-landed `control._status_sample_conversions` (the natural sibling of `_status_metric`,
operating on the identical `fetch_entity_metrics` row shape).

Changes (present-row branch only in both producers):

- **`authoring.py`** — `_attach_duplicate_grounding` (go-live / scale-out path):
  - Added `_status_sample_conversions` to the existing `from .control import ( … )` block.
  - Present-row (`else`) branch: `sample_purchases=_num(row.get("purchases"))` →
    `sample_purchases=_status_sample_conversions(row, goal)`. `sample_spend` untouched.
  - Docstring updated to say the present-row sample is the goal-aware conversion count.
- **`rotation.py`** — `_attach_rotation_grounding` (fatigue / audience-swap path):
  - Extended the **function-body** import (kept in-body to dodge the `control → rotation` circular
    import): `from .control import _status_metric` → `from .control import _status_metric, _status_sample_conversions`.
  - Present-row (`else`) branch: same one-line sample swap. `sample_spend` untouched.
  - Docstring bullet updated to name the goal-aware conversion sample.
- **`monitor.py`** — **NO change** (deliberate). `classify_ad` is ROAS-centric: its sample (`results`
  = purchases) already agrees with its ROAS metric, so it does not exhibit this bug. The install-goal
  early-life path is already goal-aware (`_forced_decision_install`). The separate "mature install-goal
  ad graded on ~0 ROAS by `classify_ad`" concern is a *classification* feature, parked in
  `backlog/monitor-steady-state-install-goal-classification.md` (not this ticket).

Untouched on purpose (all goal-independent, mirroring the control ops fix's `metrics_row is None`
branches): authoring `_attach_netnew_grounding` (zero), `_attach_lookalike_grounding` (None/structural),
rotation `metrics_by_id is None` (None/structural) and `row is None` (zero) branches.

## How to validate

- Full suite: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` — **370 passed** at handoff
  (was the same count + the 7 new tests below; no pre-existing failures, no `.pre-existing-error.md`
  needed). No ruff/mypy/black configured (pyproject declares only `[tool.pytest.ini_options]`), so
  there is no separate lint/type step.

### New tests (the testing floor — extend, don't trust as exhaustive)

Authoring (duplicate):
- `test_build_duplicate_ad_plan_install_goal_grounds_on_installs` — install goal, `purchases=0` +
  `app_installs=120`: asserts `metric_name == cost_per_app_install`, `sample_purchases == 120.0`, band
  `> low`, verdict `stands`.
- `test_build_duplicate_ad_plan_roas_goal_ignores_app_installs_decoy` — ROAS goal with a 999-install
  decoy: asserts `sample_purchases == 60.0` (purchases, decoy ignored) — proves the ROAS path is
  byte-identical to before.
- `test_build_duplicate_ad_plan_install_goal_no_row_still_cites_zero_and_abstains` — no source row +
  install goal: asserts zero sample / abstain / insufficient (the no-row branch is goal-independent).

Rotation:
- `test_rotation_install_goal_grounds_on_installs_clears_low` — install goal, `purchases=0` +
  `app_installs=120`: asserts `metric_name == cost_per_app_install`, `sample_purchases == 120.0`, band
  `> low` AND `== medium` (correlational cap — never high), verdict `stands`. Added a
  `_rotation_install_row` helper next to the existing `_rotation_metric_row`.
- `test_rotation_roas_goal_ignores_app_installs_decoy` — ROAS goal + 999-install decoy: asserts
  `sample_purchases == 120.0` (purchases), band `medium`.
- `test_rotation_install_goal_no_window_row_still_cites_zero_and_abstains` — `metrics_by_id={}` + install
  goal: zero sample / abstain (zero branch goal-independent).
- `test_rotation_install_goal_structural_abstain_when_no_metrics` — `metrics_by_id=None` + install goal:
  `sample_purchases is None` / abstain (structural branch goal-independent).

## Reviewer focus / known gaps

- **Sample/metric agreement** is the core invariant: install goal → `cost_per_app_install` +
  `app_installs`; ROAS/default → ROAS + `purchases`. Both directions are pinned by tests above. Verify
  no other present-row grounding site was missed.
- **No-goal-but-installs-present asymmetry is intentional and untested here.** `_status_sample_conversions`
  keys only on the literal `"maximize_in_app_subscriptions"` string, so an account with **no** goal but
  installs present (where `_status_metric` falls through to `cost_per_app_install`) keeps the sample on
  `purchases` — a deliberate metric/sample disagreement matching control/actions. Control pins this
  (`test_enable_ads_no_goal_installs_present_keeps_sample_on_purchases`); I did **not** add the
  equivalent for authoring/rotation. A reviewer may want a parity test there. Behavior is correct as-is;
  it is just under-pinned in these two modules.
- **Apply-time gate carry-through** (authoring `op_grounding_gap`, rotation `op_grounding_gap`) trusts the
  cited band/sample and never recomputes from `purchases`, so the install sample carries through. I did
  **not** add a new apply-time end-to-end test for the *install-goal* duplicate/rotation through the gate
  — existing gate tests cover the mechanism on ROAS/zero/thin samples, and the install path's band is
  identical in kind. A reviewer who wants belt-and-suspenders could add one (e.g. approve an install-goal
  duplicate with a healthy install sample and assert it executes through `apply_authoring_plan`).
- **Serialized key name unchanged.** The evidence field/JSON key is still `sample_purchases` — do NOT
  rename it here. The rename to `sample_conversions` is owned by `confidence-sample-conversions-rename`,
  which lists THIS ticket as a prereq (runs after). Renaming here would thrash the same lines.
- **Rotation band cap.** Rotation grounds at `correlational`, so even a healthy install sample caps at
  `medium`. The test asserts `== medium`, not `high` — correct, not a bug.
