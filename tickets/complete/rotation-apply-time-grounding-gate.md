description: Rotation now gets the same final safety block every other account change already has — an operator can no longer push an audience swap that the system flagged as having no evidence of fatigue.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md, AGENTS.md
difficulty: medium
----
## What shipped

The rotation family (`audience_rotation`, `advantage_disable`) now enforces the same **hard
apply-time grounding gate** that `apply_ops_plan` / `apply_authoring_plan` already enforce. Both
builders set `guardrails.requires_grounding: True`; both appliers read that flag and run
`write_grounding.op_grounding_gap(confidence, evidence)` per item — **after** the live-drift blocks,
**before** `compute_new_targeting`. A non-`None` gap appends a `blocked` `RotationResult` and
`continue`s.

Gate semantics (unchanged, shared with control/authoring):
- missing/blank confidence band → **blocked** ("missing required evidence/confidence")
- `abstain` band **with a cited sample** (thin or zero) → **blocked** ("insufficient data")
- `abstain` band with **no** cited sample (structural abstain) → **allowed** (honest "no metric to
  cite" — advantage-disable items, and rotation plans built with no `metrics_by_id`)

Drift-first ordering: a rotation that is both thin-sample and live-drifted reports the **drift**
reason, not the grounding reason — a stale plan must be re-proposed regardless of band.

## Review findings

**Verdict: implementation is correct, symmetric with `apply_ops_plan`, and well-documented. No major
findings; no new tickets filed. Three test gaps closed inline (minor).**

### What was checked

- **Diff read fresh before the handoff.** `git show 4886763` over `rotation.py`, tests, docs, AGENTS.md.
- **Gate logic & semantics.** Re-read `write_grounding.op_grounding_gap` — the three branches
  (no-band → block, abstain+sample → block, structural abstain → allow) behave exactly as the rotation
  appliers rely on. Non-dict `confidence`/`evidence` handled gracefully (block / treated as empty).
- **Parity with control.** `control.apply_ops_plan` (control.py:511–524) reads
  `guardrails.requires_grounding` and runs the gate **before** the `validate_only`/dry-run branch.
  Rotation matches: gate sits before the validate/dry-run branches, so it blocks in **all three** modes
  (execute, validate_only, dry_run) — the implementer's flagged "only execute is tested" gap is now
  pinned (see new dry-run test).
- **Build paths.** `_attach_rotation_grounding` cites a real sample when a metrics row exists, a **zero**
  sample when the ad set has no row, and **no** sample when `metrics_by_id is None` (structural).
  `_attach_advantage_disable_grounding` always cites no sample → structural abstain → gate-allowed by
  construction. Both builders set `requires_grounding: True`. Verified.
- **Type safety / resource cleanup / DRY.** `(plan.get("guardrails") or {}).get(...)` tolerates a
  missing/None guardrails block. The shared check lives once in `op_grounding_gap`; the three apply
  loops differ in surrounding drift/shape logic, so further extraction would over-couple — DRY is at
  the right level. No I/O or resources to clean up in the gate path.
- **Docs.** Read every touched doc against the new reality: `rotation.py` docstrings
  (`build_rotation_plan`, `apply_rotation_plan`, `apply_advantage_disable_plan`),
  `docs/META_ACTION_WORKFLOW.md` (apply-time-grounding paragraph + rotation section + drift-precedence
  bullet), AGENTS.md write-catalog rows. All accurate. Grepped the repo for stale
  "advisory"/"no apply-time grounding"/old-fix-ticket references — the only remaining "advisory" is
  correctly about `set_creative_features` (still advisory, out of scope).
- **Lint + tests.** No linter is configured for this package (`lint_vault` is an unrelated vault-content
  CLI entry point; no ruff/flake8/black config in `pyproject.toml`). Full
  `tests/test_meta_ads_analysis.py` → **278 passed** (was 275; +3 added this pass). No
  `.pre-existing-error.md` — nothing failed.

### Findings & disposition

- **MINOR (fixed inline) — no positive-case test for a *cited, above-floor* rotation.** The existing
  `test_apply_rotation_execute_writes_full_targeting_for_approved_only` builds with `metrics_by_id=None`
  (structural abstain), so it only proved structural-abstain executes — not that a rotation with a real
  above-floor sample (computed *non-abstain* band) passes the gate. A gate bug that over-blocked every
  grounded rotation would have slipped through. Added
  `test_apply_rotation_cited_above_floor_band_executes_through_gate` (purchases=120, spend=2400 →
  non-abstain band → executes).
- **MINOR (fixed inline) — gate-in-dry-run unproven.** Implementer flagged that new tests only assert
  `execute=True`. Added `test_apply_rotation_blocks_approved_thin_sample_in_dry_run` — a thin-sample
  approved rotation is `blocked` with `execute=False` and never reaches the `dry_run` targeting record
  (`results[0].targeting is None`), pinning gate-before-mode-branch parity with control.
- **MINOR (fixed inline) — per-item isolation unproven.** Implementer suggested a multi-item mix. Added
  `test_apply_rotation_gate_isolates_per_item_blocked_does_not_stop_allowed` — one structural-abstain
  rotation (allowed → executed) and one cited-thin rotation (blocked) in a single
  `apply_rotation_plan` call; the blocked item's `continue` does not abort the loop, and only the
  allowed item is written.
- **ACCEPTED (no action) — old plan JSON on disk is grandfathered.** A `rotation_plan.json` generated
  before this change has no `requires_grounding`, so the gate reads inert for it. This is **deliberate
  parity** with control (`test_apply_ops_grounding_guard_inert_without_flag`), the window is narrow
  (plans are run-date-keyed and regenerate daily), and defaulting the flag on only for rotation would
  break the symmetry that is the whole point of this ticket. Not a defect.
- **OUT OF SCOPE (unchanged) — `set_creative_features` remains advisory.** Only the rotation family was
  brought to parity in this ticket; the creative-features advisory gap is a separate concern and was not
  touched. Docs reflect this correctly.

### Empty categories

- **No major findings** → no new fix/plan/backlog tickets filed.
- **No regressions** → the six listed regression-floor tests stay green inside the 278-pass run.
- **No pre-existing failures** → `.pre-existing-error.md` not written.

## Validation

- `tests/test_meta_ads_analysis.py` → **278 passed** (`.venv/bin/python -m pytest`; repo has no
  `python` on PATH).
- `pytest -k "rotation or advantage_disable"` → **24 passed** (was 22; +2 of the 3 new tests fall in
  this filter).
