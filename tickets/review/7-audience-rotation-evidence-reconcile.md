description: Audience rotation (the safe swap of which saved audiences an ad set targets) now records the facts and confidence behind each change and passes the automatic second-opinion check, like every other account-changing action. This handoff is for reviewing that change.
prereq:
files: src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/write_grounding.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What landed (reconcile-only — no rotation behavior changed)

Attached grounding (`evidence` + computed `confidence` + `review`) to the rotation family and wired a
**dedicated, key-aware** review gate. The rotation arithmetic, the pre-write live re-read + drift
guard, and the "Advantage off only / FORBIDDEN_FRAGMENTS" safety are all untouched.

- **`review.review_rotation_plan(plan, …)`** (+ `_rotation_items`, `_ROTATION_PLAN_ITEM_KEYS`).
  Dispatches on `plan_type` → the correct item key (`audience_rotation`→`rotations`,
  `advantage_disable`→`items`, `adset_rename`→`renames`), falling back to first-present key. It
  **never** reads `plan["ops"]`. Per item it reuses the exact same `review_recommendation` core and the
  demote-only `_apply_op_verdict` applier (refutation logic not forked). Idempotent; returns a new plan
  (input never mutated); demote-only.
- **`rotation._attach_rotation_grounding`** + `ROTATION_EVIDENCE_TIER = correlational`. Each rotation
  item cites the ad set's own window performance (sample + `regenerating_query`) and gets a computed,
  correlational-capped band; no row → zero sample → abstain; `metrics_by_id is None` → structural
  abstain. `build_rotation_plan` now takes `metrics_by_id`/`goal`/`policy`/`date_from`/`date_to`/
  `recency_days`/`run_date`, attaches grounding to every rotation, sets `run_date` +
  `account_action_policy` on the plan, and returns `review.review_rotation_plan(plan)`.
- **`rotation._attach_advantage_disable_grounding`**. Each advantage-disable item is a **structural
  abstain** (named ad set, no cited sample); `build_advantage_disable_plan` returns the reviewed plan.
- **Renames** are exempt (no band attached); `review_rotation_plan` passes them through untouched.
- **CLI `propose_rotation_main`** gained `--date-from`/`--date-to`, resolves the window via
  `control._resolve_grounding_window`, reads per-ad-set metrics via `control.fetch_entity_metrics`
  (`level="adset"`), and threads them into `build_rotation_plan`. `propose_disable_advantage` /
  `propose_renames` needed no CLI change (grounding is internal / exempt).
- Docs: `docs/META_ACTION_WORKFLOW.md` updated (rotation grounding subsection + corrected the
  "own review wrapper" note to name `review_rotation_plan`).

## How to validate

`.venv/bin/python -m pytest tests/ -q` → **267 passed** (9 new). New tests (all mock-only):

- `test_rotation_fatigued_adset_carries_correlational_capped_confidence` — strong sample → `medium`
  (NOT high), tier `correlational`, review `stands`.
- `test_rotation_thin_sample_abstains_and_is_flagged_insufficient` — below-floor sample → `abstain`,
  `review_verdict == "insufficient"`.
- `test_rotation_review_iterates_rotations_not_ops` — **pins the #1 failure mode**: every
  `plan["rotations"]` item actually receives a `review` block.
- `test_rotation_review_demotes_overclaimed_band` — hand-inflated `high` over a `medium` sample →
  `downgrade`; input plan not mutated.
- `test_rotation_causal_claim_is_downgraded` — `causal_flag` cause-claim → `downgrade` to `low`,
  `"causal"` in `failed_inputs`.
- `test_advantage_disable_item_attaches_structural_abstain` — `abstain` with no cited sample, review
  `stands` (NOT refuted for "contradicting its metric").
- `test_rename_plan_passes_through_review_without_fabricated_band` — no `confidence`/`review` added.
- `test_rotation_review_is_idempotent` — second review is a no-op.
- `test_rotation_high_confidence_still_blocks_on_live_targeting_drift` — drift precedence: a
  confidently-grounded approved rotation is still `blocked` at execute when live targeting drifted.

## Known gaps / where to focus review (treat my tests as a floor)

1. **No apply-time grounding gate on rotations — by design, but confirm it's the intended contract.**
   `apply_rotation_plan` was deliberately left unchanged (ticket: "drift validation unchanged",
   edge-case: "grounding/review runs at propose; drift runs at execute"). So a thin/abstain rotation is
   *flagged* `insufficient` and demoted approved→proposed by review, but **not hard-blocked by
   `write_grounding.op_grounding_gap` at apply time** the way `apply_ops_plan` blocks a grounding-required
   op. Consequence: if an operator manually re-sets a thin rotation's status to `approved` after review,
   `apply_rotation_plan` will execute it (it checks only `status == approved` + no-drift). Control/
   authoring instead enforce `op_grounding_gap` when `guardrails.requires_grounding` is set. **Decide:**
   is propose-time demotion sufficient for rotation, or should `apply_rotation_plan` also consult
   `op_grounding_gap` for `rotations` items (would add apply-time logic the ticket said to leave
   unchanged)? I judged propose-time enforcement correct per the ticket; flag if you disagree.

2. **Function-level `from .control import _status_metric` inside `_attach_rotation_grounding`.** Needed
   because `control` imports `rotation` at module load (top-level import would be circular). It's a
   private symbol; `authoring` imports the same one at top-level. Confirm this call-time import + private
   reuse is acceptable, or prefer a small local metric-picker duplicate.

3. **`metric_name` inconsistency in the structural-abstain path.** `metrics_by_id is None` cites
   `metric_name="audience_fatigue"`; the with-metrics path cites the goal metric (`blended_roas` /
   `cost_per_app_install`). Harmless (no sample → abstain either way), but a reviewer may want one
   consistent name.

4. **Recency consistency producer↔gate.** Producer `recency_days` (from CLI `_resolve_grounding_window`)
   and the gate's recency (re-derived from `plan["run_date"]` + `evidence["window"]`) only agree when
   `run_date`/window align. The CLI sets both from the same resolver, and tests use aligned values — but
   there's no test for a deliberate mismatch (would the gate spuriously downgrade?).

5. **CLI metric-fetch path is not unit-tested.** Tests exercise `build_rotation_plan` with injected
   `metrics_by_id`; the live `fetch_entity_metrics(level="adset")` wiring in `propose_rotation_main` has
   no mock-client test. Consider a builder+fake-reader test if you want that path pinned.

6. **Reversibility is documented, not enforced.** Docs note the results log captures the prior audience
   set so a rotation can be reversed (itself another rotation); no code asserts the audit trail is
   complete.

## End
