description: Rotation now gets the same final safety block every other account change already has — an operator can no longer push an audience swap that the system flagged as having no evidence of fatigue.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md, AGENTS.md
difficulty: medium
----
## What changed

The rotation family (`audience_rotation`, `advantage_disable`) now enforces the same **hard
apply-time grounding gate** that `apply_ops_plan` / `apply_authoring_plan` already enforce. Previously
rotation grounding was purely advisory: every rotation item starts `proposed`, and `review` only
demotes `approved`→`proposed`, so the propose-time review never actually blocked anything. An operator
who set a rotation's `status` to `"approved"` despite a `review_verdict: "insufficient"` could push a
swap on an ad set with **no delivery in the fatigue window** — exactly the case grounding exists to
stop. That hole is now closed at the last step (apply), symmetric with every other write.

### Implementation (rotation.py)

- Imported `op_grounding_gap` alongside `attach_op_grounding` (line 30).
- Set `"requires_grounding": True` in the `guardrails` dict of **both** `build_rotation_plan` (~353)
  and `build_advantage_disable_plan` (~594).
- `apply_rotation_plan`: reads `require_grounding` from `plan["guardrails"]`; the gate runs **after**
  the two live-drift blocks (live-included-changed, advantage-now-on) and **before**
  `compute_new_targeting`. On a non-`None` gap it appends a `blocked` `RotationResult` and `continue`s.
- `apply_advantage_disable_plan`: same gate, after the "already off" skip, before
  `compute_new_targeting`.
- Rotation arithmetic (`compute_new_targeting`, `_rotate_forward`) and the live-drift guard are
  **byte-for-byte unchanged**.

### Gate semantics (from `write_grounding.op_grounding_gap`, unchanged)

- No/blank confidence band → **blocked** ("missing required evidence/confidence").
- `abstain` band **with a cited sample** (thin or zero) → **blocked** ("insufficient data").
- `abstain` band with **no** cited sample (structural abstain) → **allowed** — an honest "no metric to
  cite". Advantage-disable items are structural abstains by design, so they still execute. Rotation
  plans built with no `metrics_by_id` are structural abstains too, so the existing no-metrics tests
  stay green.

### Ordering: drift-first

Drift checks run before the grounding gate. A rotation that is *both* thin-sample and live-drifted
reports the **drift** reason, not the grounding reason — a stale plan must be re-proposed regardless of
band, and this preserves the existing precedence behavior.

## Why this is effective in production (verified, not just in tests)

- `cli.py` `propose_rotation_main` (~513-552) builds `metrics_by_id` from `fetch_entity_metrics` and
  passes it to `build_rotation_plan`, so real plans carry **cited** samples (real / thin / zero). An
  ad set with no delivery → zero sample → abstain-with-sample → blocked at apply.
- `apply_rotation_main` reads the plan JSON (which now serializes `guardrails.requires_grounding:
  true`) straight into `apply_rotation_plan`, so the flag is honored end-to-end.

## Use cases / validation

All run with `.venv/bin/python -m pytest` (no `python` on PATH; repo uses `.venv`). No type
checker/linter is configured (the `lint_vault` entry point is an unrelated vault-content CLI).

New tests (tests/test_meta_ads_analysis.py, rotation section, ~after line 2888):
- `test_apply_rotation_blocks_approved_thin_sample_at_execute` — `metrics_by_id` thin sample
  (`purchases=9, spend=40`), approved, `execute=True` → `blocked`, reason contains "insufficient
  data", `client.updates == []`.
- `test_apply_rotation_blocks_approved_zero_sample_at_execute` — `metrics_by_id={}` (omits the ad
  set → zero sample cited) → approved rotation `blocked` at execute.
- `test_apply_rotation_drift_takes_precedence_over_grounding` — thin-sample + live-drifted → reason is
  the **drift** reason ("Live included audiences changed"), NOT "insufficient data". Pins drift-first.
- `test_apply_advantage_disable_structural_abstain_still_executes` — approved Advantage-disable
  (structural abstain) with `requires_grounding` set → still `executed`, AA written off.

Regression floor (must stay green — they do): the existing no-metrics structural-abstain tests
`test_apply_rotation_execute_writes_full_targeting_for_approved_only`,
`test_apply_rotation_dry_run_does_not_write`,
`test_apply_rotation_validate_only_sends_validate_flag_and_does_not_execute`,
`test_apply_rotation_blocks_when_live_targeting_drifted`,
`test_apply_advantage_disable_preserves_audiences_and_turns_off_aa`,
`test_rotation_plan_disable_flag_writes_advantage_off_on_apply`.

Results:
- `pytest -k "rotation or advantage_disable"` → **22 passed, 253 deselected**.
- full `tests/test_meta_ads_analysis.py` → **275 passed**.
- No `.pre-existing-error.md` written — nothing failed.

## Docs updated

- `rotation.py` docstrings: `build_rotation_plan` (reworded the over-stated "demoted" claim → marked
  insufficient at propose + hard-blocked at apply), `apply_rotation_plan` and
  `apply_advantage_disable_plan` (added the gate + drift-first ordering bullets).
- `docs/META_ACTION_WORKFLOW.md`: the apply-time grounding paragraph (~42), the rotation grounding
  section intro, the Rotations / Advantage-disable / Drift-precedence bullets.
- `AGENTS.md`: the `audience_rotation` and `advantage_disable` write-catalog rows — removed the
  "advisory only / no apply-time grounding gate" wording, state the gate now applies (structural-abstain
  Advantage-disable still allowed).

## Honest gaps / things for the reviewer to probe

- **New-tests-only assert `execute=True`.** The gate sits *before* the `validate_only` / dry-run
  branches, so a thin/zero approved rotation is also blocked in those modes (matches `control`'s gate
  placement). I did not add explicit dry-run/validate-mode block tests — worth a glance if you want
  that pinned. (The intent is that it blocks in all three modes; confirm you agree that's desirable —
  control behaves the same way.)
- **Old plan JSON on disk is grandfathered.** A `rotation_plan.json` generated before this change has
  no `requires_grounding`, so `require_grounding` reads False and the gate is inert for it (graceful,
  mirrors `test_apply_ops_grounding_guard_inert_without_flag`). Only newly-proposed plans get the flag.
  No migration was done; flag whether that's acceptable or whether `apply_rotation_plan` should default
  the flag on for rotation plans regardless of what the JSON says.
- **`set_creative_features` is still advisory** (unchanged, out of scope) — only the rotation family
  was brought to parity in this ticket.
- Per the implement-stage charter, treat these tests as a floor, not a ceiling: the adversarial pass
  may want a multi-item plan mixing one structural-abstain rotation (allowed) and one cited-abstain
  rotation (blocked) in the same `apply_rotation_plan` call to confirm per-item isolation.
