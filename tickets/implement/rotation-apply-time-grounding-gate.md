description: Rotation changes the system flags as having no evidence of audience fatigue can still be pushed to Meta if an operator approves them; every other account change is hard-blocked at the last step. Add that same final safety block to rotation.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md, AGENTS.md
difficulty: medium
----
## Decision (from the fix stage): Option (A) — add apply-time enforcement

The rotation family gets the same hard apply-time grounding gate that `apply_ops_plan` /
`apply_authoring_plan` already enforce. Rationale:

- **Propose-time demotion is structurally a no-op for rotation.** Every rotation item is built with
  `status = PROPOSED_STATUS`, and `review._apply_op_verdict` only demotes `approved`→`proposed`. With
  nothing ever starting `approved`, the review pass only edits the stored `band` + a `review_verdict`
  marker — it never blocks anything. So today rotation grounding is purely advisory.
- **This is an asymmetry the grounding work explicitly set out to remove.** An equivalent control
  `set_status` op that cites a zero/thin sample → abstain is *hard-blocked* at apply
  (`test_apply_ops_blocks_thin_abstain_but_allows_structural_abstain`). The stated goal of the upstream
  ticket (`audience-rotation-evidence-reconcile`) was that rotation "passes the automatic
  second-opinion check, like every other account-changing action." An operator who sets a rotation's
  `status` to `"approved"` without noticing `review_verdict: "insufficient"` can currently push a swap
  on an ad set with **no delivery in the fatigue window** — precisely the case grounding exists to stop.
- **The gate already exists and is the right shape.** `write_grounding.op_grounding_gap` distinguishes
  a *structural* abstain (no sample cited — allowed, an honest "no metric to cite") from a *thin/zero*
  abstain (sample cited but below the significance floor — blocked). Reusing it is low-risk and keeps
  the pure layers pure.
- Reversibility (the Option-B argument) does **not** make rotating on no evidence of fatigue correct;
  the fatigue grounding exists exactly so the swap is not made without evidence. Option B rejected.

### Why this is safe for existing tests (verified in the fix stage)

The existing `apply_rotation_plan` execute / dry-run / validate tests build plans with **no
`metrics_by_id`**, so each item is a *structural* abstain (`evidence.sample_purchases` and
`sample_spend` are both `None` → `op_grounding_gap` returns `None` → **allowed**). They keep executing.
The live-drift test (`test_apply_rotation_blocks_when_live_targeting_drifted`) is likewise a structural
abstain that the gate allows, and stays `blocked` by the drift guard. Only a rotation built with
`metrics_by_id` that yields a **cited** zero/thin sample (abstain *with* a sample) becomes newly
blocked — the bug case.

### Ordering: drift-first

Place the grounding gate **after** the two live-drift blocks (live-included-changed,
advantage-now-on) and **before** `compute_new_targeting`. Drift-first preserves the existing
precedence test and is the right priority anyway (a drifted plan is stale and must be re-proposed
regardless of band). Document the chosen order in the docstring.

## Architecture

`apply_rotation_plan` (rotation.py ~394) and `apply_advantage_disable_plan` (~611) currently consult
only `status == APPROVED_STATUS` + the live re-read drift guards. Mirror the control pattern:

```
require_grounding = bool((plan.get("guardrails") or {}).get("requires_grounding"))
...
for rotation in plan["rotations"]:
    if rotation.status != APPROVED_STATUS: -> skipped
    <live re-read + drift block(s)>                 # unchanged, runs first
    if require_grounding:                            # NEW
        gap = op_grounding_gap(rotation.get("confidence"), rotation.get("evidence"))
        if gap is not None: -> RotationResult(adset_id, "blocked", reason=gap); continue
    new_targeting = compute_new_targeting(...)       # unchanged
    <validate_only / dry_run / execute>              # unchanged
```

Set `guardrails.requires_grounding: True` in `build_rotation_plan`'s plan dict (~347) **and** in
`build_advantage_disable_plan`'s plan dict (~572). Wiring both gives full rotation-family parity;
because Advantage-disable items are structural abstains by design, the gate **allows** them (they still
execute), satisfying the "structural abstains must still execute" requirement. Add the same gate block
to `apply_advantage_disable_plan` after its "already off" skip + live re-read, before
`compute_new_targeting`.

`op_grounding_gap` and `attach_op_grounding` both live in `write_grounding.py`; rotation.py currently
imports only `attach_op_grounding` (line 30) — add `op_grounding_gap` to that import.

The rotation arithmetic (`compute_new_targeting`, `_rotate_forward`) and the live-targeting drift guard
stay byte-for-byte unchanged.

## Reference points

- Control pattern to mirror: `control.apply_ops_plan` lines 509–524 (`require_grounding` flag +
  `op_grounding_gap` gate before the write), guardrail set at `control.py` ~1264
  (`"requires_grounding": True`).
- Gate semantics: `write_grounding.op_grounding_gap` (write_grounding.py:115) — no-confidence →
  block; abstain **with** cited sample → block; structural abstain (no sample) → allow.
- Existing control gate tests to model the new rotation tests on:
  `test_apply_ops_blocks_approved_ungrounded_write`,
  `test_apply_ops_blocks_thin_abstain_but_allows_structural_abstain`
  (tests/test_meta_ads_analysis.py ~3613, ~3649).
- Rotation test fixtures: `_three_adset_partition()` (~2406), `_FakeClient` (~2414),
  `_rotation_metric_row(...)` (~2726), `_ROTATION_WINDOW` (~2722). A thin sample is
  `purchases=9, spend=40`; a zero sample is achieved by passing a `metrics_by_id` that omits the
  approved ad set's id (the `row is None` branch cites a 0.0/0.0 sample → abstain *with* sample).
- Existing tests that must stay green (no-metrics structural abstains):
  `test_apply_rotation_execute_writes_full_targeting_for_approved_only` (~2476),
  `test_apply_rotation_dry_run_does_not_write` (~2465),
  `test_apply_rotation_validate_only_sends_validate_flag_and_does_not_execute` (~2599),
  `test_apply_rotation_blocks_when_live_targeting_drifted` (~2494),
  `test_apply_advantage_disable_preserves_audiences_and_turns_off_aa` (~2692).

## Docs / docstrings to correct

- `build_rotation_plan` docstring (rotation.py ~248): the line saying an over-claimed/below-floor
  rotation is "demoted ... before it reaches the operator" overstates the *demote* half (items never
  start `approved`, so review only marks/relabels). Reword to: review marks it insufficient at propose
  time **and** the apply-time grounding gate hard-blocks an approved-but-abstain (cited-sample)
  rotation.
- `apply_rotation_plan` docstring (~402) and `apply_advantage_disable_plan` docstring (~619): add a
  bullet documenting the grounding gate and the drift-first ordering.
- `docs/META_ACTION_WORKFLOW.md`: the rotation paragraph (~43–47) says the rotation family "do **not**
  set that flag — their review is advisory ... no apply-time grounding block yet (rotation's gap is
  tracked in `fix/rotation-apply-time-grounding-gate`)". Update to state rotation now sets
  `requires_grounding` and is hard-blocked at apply like ops/authoring. Also update the rotation
  grounding section (~232+).
- `AGENTS.md` write-tool-catalog rows for `audience_rotation` and `advantage_disable`: remove the
  "Review is advisory only — no apply-time grounding gate (open: fix/rotation-apply-time-grounding-gate)"
  / "reviewed (advisory only)" wording; state the apply-time gate now applies (structural-abstain
  Advantage-disable still allowed).

## TODO

### Phase 1 — implementation
- [ ] Add `op_grounding_gap` to the `from .write_grounding import ...` line in rotation.py.
- [ ] Set `"requires_grounding": True` in the `guardrails` dict of `build_rotation_plan` and of
      `build_advantage_disable_plan`.
- [ ] In `apply_rotation_plan`: read `require_grounding` from `plan["guardrails"]`; after the drift
      block(s) and before `compute_new_targeting`, when `require_grounding`, call
      `op_grounding_gap(rotation.get("confidence"), rotation.get("evidence"))` and append a `blocked`
      `RotationResult` + `continue` on a non-`None` gap.
- [ ] Apply the same gate in `apply_advantage_disable_plan` (after the "already off" skip, before
      `compute_new_targeting`).
- [ ] Confirm the rotation arithmetic and drift guard are untouched.

### Phase 2 — docstrings & docs
- [ ] Correct the `build_rotation_plan`, `apply_rotation_plan`, `apply_advantage_disable_plan`
      docstrings.
- [ ] Update `docs/META_ACTION_WORKFLOW.md` (rotation paragraph + rotation grounding section).
- [ ] Update the `audience_rotation` and `advantage_disable` rows in `AGENTS.md`'s write-tool catalog.

### Phase 3 — tests (add to tests/test_meta_ads_analysis.py, rotation section)
- [ ] `test_apply_rotation_blocks_approved_thin_sample_at_execute`: build a plan with
      `metrics_by_id` giving a thin sample (`purchases=9, spend=40`), approve a rotation, `execute=True`
      → status `blocked`, reason contains "insufficient data", `client.updates == []`.
- [ ] `test_apply_rotation_blocks_approved_zero_sample_at_execute`: `metrics_by_id` that omits the
      approved ad set's id (zero sample cited → abstain) → approved rotation `blocked` at execute.
- [ ] `test_apply_rotation_drift_takes_precedence_over_grounding`: a thin-sample (would-be
      grounding-blocked) rotation that is ALSO live-drifted → reason is the **drift** reason, not the
      grounding reason (pins drift-first ordering).
- [ ] `test_apply_advantage_disable_structural_abstain_still_executes`: approved Advantage-disable item
      (structural abstain) with `requires_grounding` now set → still `executed`, AA written off.
- [ ] Sanity: re-run the existing rotation execute/dry-run/validate/drift tests — they must stay green
      (no-metrics structural abstains are allowed by the gate).

### Phase 4 — validation
- [ ] `python -m pytest tests/test_meta_ads_analysis.py -k "rotation or advantage_disable" 2>&1 | tee /tmp/rot.log`
- [ ] Full suite if time permits: `python -m pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/all.log`
- [ ] Type check if the project runs one (see AGENTS.md).
