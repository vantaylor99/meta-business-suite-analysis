description: For app-install accounts, the go-live (duplicate-ad authoring) and audience-rotation paths now count installs — not purchases — when grading how confident a recommendation is, so a decision backed by real install volume no longer reads as low confidence, matching what the action-plan and enable/budget paths already do.
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py
----

## What landed

Two grounded **write** producers were grounding their significance sample on the **purchase** count
unconditionally, even though they already pick their **metric** goal-aware (both reuse
`control._status_metric`). For a `maximize_in_app_subscriptions` account — which produces ~0 purchases
but real `app_installs` — that mismatch pinned the confidence band at `low`/`abstain` even when the
decision was backed by real install volume. The fix makes the **sample** goal-aware too by reusing the
already-landed `control._status_sample_conversions` (the natural sibling of `_status_metric`).

- **`authoring.py`** `_attach_duplicate_grounding` (go-live / scale-out): present-row branch sample
  `_num(row.get("purchases"))` → `_status_sample_conversions(row, goal)`; import + docstring updated.
- **`rotation.py`** `_attach_rotation_grounding` (fatigue / audience-swap): same one-line present-row
  sample swap; function-body import extended (kept in-body to dodge the `control → rotation` circular
  import); docstring updated.
- **`monitor.py`** — deliberately untouched: `classify_ad` is ROAS-centric (sample = purchases already
  agrees with its ROAS metric); the install-goal early-life path is already goal-aware via
  `_forced_decision_install`. The mature install-goal-on-ROAS concern is a separate *classification*
  feature parked in `backlog/monitor-steady-state-install-goal-classification.md`.

## Review findings

### What was checked

- **Implement diff, fresh eyes** (`git show 7d72e46`) before reading the handoff. Both source edits are
  byte-minimal and mirror the prereq control-ops fix (`control.py:1382-1398`) exactly — same
  present-row/None-row/zero structure, `sample_spend` untouched, `goal` already in scope in both
  signatures.
- **`_status_sample_conversions` contract** (`control.py:619-639`): install goal → `app_installs`,
  ROAS/default/unknown → `purchases`, keying on the literal `"maximize_in_app_subscriptions"` string
  only. The two new call sites use it identically to control.
- **Completeness — no missed present-row grounding site.** Audited every `_status_metric(` caller
  (`grep` → control.py:803/1382, authoring.py:305/353, rotation.py:186) and every `sample_purchases=`
  assignment. Result: all three **present-row** groundings (control, authoring, rotation) now use
  `_status_sample_conversions`; authoring.py:305 is the netnew `_status_metric(None, …)` → zero-sample
  branch (goal-independent, correctly untouched). monitor.py / experiment.py / knowledge_provenance.py /
  early_triage.py sites are ROAS-centric or non-`_status_metric` producers, out of scope.
- **Untouched branches** (None/structural → no sample; absent-row → zero sample) confirmed
  goal-independent in both modules — the goal-aware selector is reached only on the present-row branch.
- **Tests + lint.** `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **372 passed**
  (370 at handoff + 2 added below). No ruff/mypy/black configured (pyproject declares only
  `[tool.pytest.ini_options]`), so there is no separate lint/type step. No pre-existing failures; no
  `.pre-existing-error.md` needed.

### What was found and done

- **Minor — under-pinned deliberate asymmetry (fixed inline).** The handoff flagged that the
  no-goal-but-installs-present case (where `_status_metric` falls through to `cost_per_app_install` but
  the sample deliberately stays on `purchases`, for parity with control/actions) was pinned only for
  control, not for authoring/rotation. Added two parity tests mirroring
  `test_enable_ads_no_goal_installs_present_keeps_sample_on_purchases`:
  - `test_build_duplicate_ad_plan_no_goal_installs_present_keeps_sample_on_purchases`
  - `test_rotation_no_goal_installs_present_keeps_sample_on_purchases`

  Both assert `metric_name == cost_per_app_install` while `sample_purchases == 30.0` — locking the
  intentional metric/sample disagreement so a future naive "fix" that makes the sample track the metric
  is caught. Behavior was already correct; this only closes the regression-coverage gap.

- **No major findings.** No new fix/plan/backlog tickets filed. The handoff's other noted gaps were
  reviewed and judged not worth acting on in this pass:
  - *Apply-time gate end-to-end for the install path* — the gate trusts the cited band/sample and never
    recomputes from `purchases`; existing gate tests cover the mechanism on ROAS/zero/thin samples and
    the install path's band is identical in kind. Belt-and-suspenders only.
  - *Serialized key name `sample_purchases`* — correctly left as-is; the rename to `sample_conversions`
    is owned by `confidence-sample-conversions-rename` (lists this ticket as a prereq).
  - *Rotation band caps at `medium`* — correct (correlational tier per `ROTATION_EVIDENCE_TIER`), not a
    bug; the test asserts `== medium`, not `high`.

### Verdict

Implementation is correct, minimal, and complete; coverage now includes the previously under-pinned
asymmetry. Done and verified.
