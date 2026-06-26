description: When an operator approves an audience rotation that the system flagged as having no supporting evidence, the change is still sent to Meta — unlike every other account change, which is hard-blocked at the last step. Decide whether rotation should get that same final safety block, and if so add it.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/write_grounding.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Background

Ticket `audience-rotation-evidence-reconcile` (sequence 7) attached `evidence` + computed
`confidence` + a `review` block to the rotation family (`build_rotation_plan` /
`build_advantage_disable_plan`) and added the key-aware `review.review_rotation_plan` gate, to bring
rotation in line with the grounding/second-opinion pattern used by control and authoring. Tests pass
and the propose-time wiring is correct.

The implementer explicitly flagged (handoff "Known gaps" #1) that `apply_rotation_plan` was left
without an apply-time grounding gate, and asked the reviewer to confirm whether propose-time demotion
is sufficient. Review found it is **not** sufficient — see below.

## The problem: rotation grounding currently has no enforcement teeth

The second-opinion pattern has two enforcement points:

1. **Propose time** — `review_*` lowers a band and demotes `status` `approved`→`proposed`. A demoted
   op is then skipped by apply (apply only sends `status == "approved"`).
2. **Apply time** — `apply_ops_plan` / `apply_authoring_plan`, when the plan's
   `guardrails.requires_grounding` is set and the op is in `GROUNDING_REQUIRED_OPS`, call
   `write_grounding.op_grounding_gap(confidence, evidence)` and **block** an approved write that is
   ungrounded or rests on an `abstain` band *with a cited sample* (thin data). This is the
   fail-closed defense that survives a human (or hand-edited plan) re-approving a bad write.

For the **rotation family**, point (1) is a structural no-op: every rotation item is built with
`status = PROPOSED_STATUS` (see `rotation.build_rotation_plan` ~line 301 and
`build_advantage_disable_plan` ~line 552). `_apply_op_verdict` only demotes `approved`→`proposed`, so
with nothing ever starting `approved`, the review changes only the stored `band` + a `review_verdict`
marker. It never blocks anything.

Point (2) does not exist for rotation: rotation plans do **not** set `guardrails.requires_grounding`,
and `apply_rotation_plan` (rotation.py ~line 394) consults only `status == "approved"` + the
live-targeting drift guard. It never calls `op_grounding_gap`.

**Consequence:** rotation grounding is purely advisory. An operator who reads `rotation_plan.json`,
sees a rotation, and sets its `status` to `"approved"` — without noticing `review_verdict:
"insufficient"` — will have it executed, even when the ad set had **no delivery in the fatigue
window** (cites a zero sample → `abstain`). That is precisely the "rotating on no evidence of
fatigue" case the grounding work set out to prevent, and it is the case an equivalent control
`set_status` op is hard-blocked from at apply time. This is an asymmetry with the rest of the system
and undercuts the ticket's stated goal ("passes the automatic second-opinion check, like every other
account-changing action").

Covered today only at propose time by
`test_rotation_adset_with_no_window_row_cites_zero_sample_and_abstains` and
`test_rotation_thin_sample_abstains_and_is_flagged_insufficient` — both assert the band/verdict, not
any apply-time block (there is none to assert).

## Decision required

Pick one (this is a design call, hence a fix ticket rather than an inline change to the apply path the
prior ticket deliberately froze):

- **(A) Add apply-time enforcement (recommended for consistency).** Set
  `guardrails.requires_grounding: True` on the rotation plan, and in `apply_rotation_plan` call
  `op_grounding_gap(rotation.get("confidence"), rotation.get("evidence"))` for each approved rotation
  **before** computing/sending targeting; on a non-`None` gap, append a `blocked` `RotationResult`
  and skip. Order it relative to the existing live-drift block deliberately (drift-first keeps the
  existing precedence test green; document whichever order is chosen). Note `op_grounding_gap` already
  *allows* a structural abstain (no cited sample), so the Advantage-Audience-disable structural
  abstains in `apply_advantage_disable_plan` are unaffected if that path is also wired — but a
  zero-sample rotation (sample cited) is correctly blocked.
- **(B) Accept advisory-only grounding for rotation and document it as deliberate.** Justify on the
  grounds that rotation is reversible and Advantage-off-only, so the safety bar is lower. If chosen,
  update the docstring on `build_rotation_plan` (which currently says an over-claimed/below-floor
  rotation is "demoted ... before it reaches the operator" — the *demoted* half is a no-op since items
  are never built `approved`) and `docs/META_ACTION_WORKFLOW.md` to state plainly that rotation
  grounding is advisory and there is no apply-time block, so a future reader does not assume parity
  with control/authoring.

## Requirements

- A clear decision recorded in the ticket, with rationale.
- If (A): apply-time block wired for the rotation family; structural abstains (Advantage-disable) must
  still execute; a zero/thin-sample approved rotation must be `blocked` at apply with a reason; the
  existing live-drift precedence behavior preserved. New tests: approved-but-abstain rotation blocked
  at execute; structural-abstain disable still executes; drift-still-takes-precedence unchanged.
- If (B): docs + docstrings corrected so the advisory-only contract is explicit; add a test asserting
  the documented behavior (approved abstain rotation *does* execute) so the decision is pinned.
- Whichever path: the rotation arithmetic and the live-targeting drift guard stay unchanged.
