description: A shared safety framework was added so that other kinds of account changes (pausing, budgets, creating campaigns, audience swaps) can carry the same evidence, confidence rating, and automatic second-opinion check the existing pause/scale recommendations already carry — and a gate blocks an approved change that lacks that grounding.
prereq:
files: src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/authoring.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## What landed

This ticket built the **shared grounding scaffold** so the per-capability write tickets
(enable/set-status, CBO budget, authoring, rotation) can attach evidence/confidence/review uniformly.
It adds **no new write capability** and does **not** wire the existing builders — that is downstream.

### New module — `src/meta_ads_analysis/write_grounding.py` (pure: no I/O / clock / network)
- `attach_op_grounding(op, *, evidence, tier, spend_floor, conversions_floor, recency_days, causal_text=None)`
  — attaches a serialized `evidence` block + a **computed** `confidence` band onto an op dict.
  Computes via `confidence.assess` when a sample is present, `confidence.abstain_confidence` when
  `evidence is None` or no sample (`sample_purchases`/`sample_spend` both `None`). A below-floor sample
  yields `abstain` (assess does this naturally) — **never** a defaulted `low`/`medium`.
- `op_grounding_gap(confidence, evidence) -> str | None` — pure gate helper. Returns a block reason
  when a grounding-required approved write is inadequately grounded: (a) no confidence block, or
  (b) `abstain` band **with a cited sample**. A structural `abstain` (band abstain, **no** sample) is
  allowed (honest no-metric op, e.g. a safety PAUSE).

### `review.py` — op/authoring review (pure, demote-only, upstream of approval)
- `review_ops_plan(plan, …)` / `review_authoring_plan(plan, …)` — iterate `plan["ops"]`, review only
  ops with a `confidence` block (informational/structural-no-band ops pass through), idempotent
  skip-guard (an op already carrying `review` is left as-is), deep-copy so the input is never mutated.
- `_apply_op_verdict(op, result)` — **chosen option (b)**: applies the verdict in the op's OWN
  vocabulary. Downgrade lowers the band; insufficient/refuted demote `status` `approved→proposed`
  (never the reverse) and set a `review_verdict` marker. It does **not** inject `executable`/`rationale`
  (those are action-plan vocab). The effective non-executable demotion for an op is `approved→proposed`,
  because the apply loops only send `status == approved` ops.

### Apply-time guard — `control.apply_ops_plan` / `authoring.apply_authoring_plan`
- When `plan["guardrails"]["requires_grounding"]` is truthy, an **approved** grounding-required op is
  blocked via `op_grounding_gap` before any write is built/sent.
- Grounding-required sets: `control.GROUNDING_REQUIRED_OPS = SUPPORTED_OPS - {"rename"}` (rename is
  cosmetic → exempt); `authoring.GROUNDING_REQUIRED_KINDS = all CREATE_KINDS` (every create is
  structural). Creates remain forced `PAUSED` regardless — grounding gates whether a create is *sent*,
  never the PAUSED-by-default safety.

### Docs
- `docs/META_ACTION_WORKFLOW.md` gained a "Grounding on every write path (ops, authoring, rotation)"
  section: the scaffold, the demote-only op review, the grounding-required set, and the
  `requires_grounding` apply guard.

## Verified facts confirmed (no wrong re-derivation)
- `review.review_recommendation` **is already generic** (keyword-only; reads `evidence`+`confidence`
  and only `action.get("action_type")` for the direction check). No decoupling refactor was needed. An
  op dict (no `action_type`) flows through harmlessly — the `direction` check simply never fires.
- Adding `evidence`/`confidence`/`review`/`review_verdict` keys to op dicts is **audit-log-safe**:
  `write_ops_results`/`write_authoring_results` serialize only their `OpResult`/`AuthoringResult`
  fields (op_id/status/request/response/reason[/kind/created_id]); op-dict extras never reach the
  result log (pinned by `test_op_grounding_review_keys_are_audit_log_safe`). `write_plan` JSON-dumps
  the whole plan, and the extra keys are plain JSON — fine.

## KEY DECISION / TRADEOFF the reviewer should scrutinize

**The apply guard is opt-in per plan via `guardrails.requires_grounding`, not unconditional.**
- Why: making it unconditional would block every currently-ungrounded approved write — including the
  output of `build_pause_plan` / `build_enable_ads_plan` / the authoring builders, which this ticket
  is explicitly NOT supposed to wire (that is the per-capability tickets' job) — and would break ~7
  existing apply tests (`test_apply_ops_enable_ad_and_budget_cap`,
  `test_apply_targeting_ops_…`, `test_apply_authoring_forces_paused_…`, the video/creative apply
  tests). The flag keeps legacy/ungrounded plans working and lets the per-capability tickets flip the
  flag and attach blocks together.
- The cost: until a per-capability ticket sets `requires_grounding: true`, **no production plan
  triggers the guard** — the framework is present but dormant. A hand-editor could also strip the flag.
  The hole it *does* close is "within a grounded plan, approve an op whose block was removed/never
  attached." Reviewer: confirm this scoping is the right call vs. breaking existing write flows now.

**Apply-guard case (b) — blocking an `abstain` band with a cited sample — goes slightly beyond the
locked text** (which mandates only "no confidence block"). It realizes the ticket's edge case
("abstain band that the gate turns into insufficient/non-executable") at the apply layer while
preserving structural pauses. Reviewer: confirm acceptable, or narrow to (a)-only.

## Known gaps (this is a starting point, not a finish line)
- **Builders not wired.** `build_pause_plan`, `build_enable_ads_plan`, the authoring `build_*` helpers,
  and all rotation builders do NOT yet call `attach_op_grounding` or set `requires_grounding`. Downstream
  per-capability tickets own that.
- **Rotation not covered.** Rotation plans use `plan["rotations"]`/`plan["items"]`, not `plan["ops"]`.
  Its review wrapper + grounding belong to the rotation ticket; `review_ops_plan` deliberately does not
  try to cover it.
- **Op-level direction-contradiction is NOT caught here** (ops carry no `action_type`, so the
  `direction` check no-ops). Documented; per-capability tickets that know the semantic (e.g. budget
  scale-up vs ROAS target) must supply an `action_type`-equivalent or their own direction guard.
- **`recency_days` for op/authoring review** comes from `plan.get("run_date")` (control/authoring plans
  don't currently set `run_date`, only `generated_at`). When absent, `assess` rounds the band down
  (recency unknown) — conservative, but per-capability tickets may want to add `run_date`.

## How to test / validate
Run: `.venv/bin/python -m pytest tests/ -q` → **227 passed**.

New tests (the floor — extend, don't trust as exhaustive):
- `test_attach_op_grounding_computes_band_never_free_types` — band == an independent `assess` call.
- `test_attach_op_grounding_abstains_when_evidence_absent` / `_below_floor_abstains_not_low` — no
  fabricated low/medium.
- `test_review_ops_plan_demotes_overclaimed_band` — hand-inflated high → downgrade; input unmutated.
- `test_review_ops_plan_skips_ops_without_confidence_block` — informational pass-through.
- `test_review_ops_plan_is_idempotent` — re-review is a no-op.
- `test_review_ops_gate_only_demotes_never_promotes` — approved→proposed on insufficient; never
  promoted; band never raised; no `executable` key injected.
- `test_apply_ops_blocks_approved_ungrounded_write` — missing block → blocked; rename exempt;
  grounded → executes.
- `test_apply_ops_grounding_guard_inert_without_flag` — legacy plans unaffected.
- `test_apply_ops_blocks_thin_abstain_but_allows_structural_abstain` — case (b) + structural exemption.
- `test_apply_authoring_blocks_ungrounded_and_keeps_paused` /
  `test_review_authoring_plan_demote_only_and_paused_preserved` — authoring shape + PAUSED-by-default
  untouched by the gate.
- `test_op_grounding_review_keys_are_audit_log_safe` — JSON-serializable; extras don't leak to the log.

Suggested adversarial angles for review:
- Confirm `review_ops_plan`/`review_authoring_plan` truly cannot raise a band or promote a status under
  any verdict combination (the demote-only invariant is the safety-critical property).
- Confirm the window format `YYYY-MM-DD..YYYY-MM-DD` is what `review.py` parses (the short-window
  downgrade in the idempotency/authoring tests depends on it — a wrong format would silently no-op).
- Confirm `write_grounding.py` / `review.py` import nothing impure (purity invariant) and that there is
  no new confidence scale (one language only).

## End
