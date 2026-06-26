description: Make every account-changing operation (pausing, budgets, creating campaigns, audience swaps) carry the same proof-and-trust attachments that the pause/scale recommendations already carry — the facts behind it, a computed confidence band, and a pass through the automatic second-opinion check — so no write reaches "approved" without grounding.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## Why

LOCKED decision: **all writes** must carry an `Evidence` block + a **computed** `Confidence` band and
**pass `review.py`**, on top of the existing `propose -> approve -> validate_only -> execute` gate,
audit log, PAUSED-by-default, and FORBIDDEN_FRAGMENTS block. Today only the `actions.py` action plan
(`pause_ad`, `increase_adset_budget`, ...) attaches evidence/confidence and is reviewed via
`review.review_action_plan`. The **`control.py` ops pipeline** (`apply_ops_plan` —
`set_status`/`set_daily_budget`/`rename`/targeting/creative) and the **`authoring.py` pipeline**
(`apply_authoring_plan` — `create_*`) and **`rotation.py`** produce plans with NO `evidence`/
`confidence`/`review` blocks. This ticket builds the **shared scaffolding** so the per-capability
tickets (enable/set-status, CBO budget, authoring, rotation) can attach grounding uniformly instead
of each reinventing it.

This ticket does NOT add new write capabilities. It is the framework the downstream write tickets
build on.

## What exists to reuse (do NOT duplicate)

- `confidence.assess(evidence, tier, spend_floor, conversions_floor, recency_days, pvalue,
  causal_text)` and `confidence.abstain_confidence(...)` — the ONLY way to compute a band.
- `confidence.Evidence` dataclass + `evidence_to_dict` / `confidence_to_dict` serializers +
  `build_regenerating_query`.
- `actions.evaluate_action_confidence` / `_attach_confidence` / `_abstain_action` — the existing
  pattern for the action plan; mirror its shape, do not import action-plan-specific logic into the
  control/authoring layers (keep modules decoupled).
- `review.review_recommendation(...)` and `review.review_action_plan(...)` — the gate.
- `control.FORBIDDEN_FRAGMENTS`, `authoring._guard_params`, PAUSED-by-default in
  `authoring._build_create`, the `proposed->approved->validate->execute` status machine.

## IMPORTANT — verified facts about the existing gate (do not re-derive wrong)

- `review.review_recommendation` is **already generic**: its signature is keyword-only
  (`evidence:dict, confidence:dict, action:dict, policy:dict|None, spend_floor, conversions_floor,
  min_window_days, recency_stale_days, recency_days`). It reads `evidence` + `confidence` and only
  pulls `action.get("action_type")` for the `direction` check. **No refactor to decouple it from the
  action plan is required** — only VERIFY this and confirm an op dict (which has no `action_type`)
  flows through harmlessly (the `direction` check simply won't fire without `action_type` + a ROAS
  target; that is acceptable for ops, document it).
- `review._apply_verdict` mutates the passed dict and, on INSUFFICIENT/REFUTED, writes
  `action["executable"]`, `action["status"]`, `action["verdict"]`, and (on INSUFFICIENT)
  `action["rationale"]`. Op dicts use `status` (proposed/approved) — compatible — but have no
  `executable`/`rationale`/`verdict` keys today. **Decide and document ONE of:** (a) reuse
  `_apply_verdict` as-is and accept that it injects `executable`/`verdict`/`rationale` onto op dicts
  (then confirm `write_ops_results`/`write_authoring_results` ignore unknown keys — they serialize
  only op_id/status/request/response/reason, so extra keys ride along harmlessly), OR (b) add an
  op-shaped `_apply_op_verdict` that demotes `status` and sets a `non_executable`/`blocked` marker in
  the op's own vocabulary. Pin the chosen behavior with a test.

## What to build — a shared grounding helper for op/authoring plans

Add a small, reusable grounding layer (suggest extending `confidence.py` or a new tiny
`write_grounding.py`; choose and justify) exposing:

```python
def attach_op_grounding(op: dict, *, evidence: Evidence | None, tier, spend_floor,
                        conversions_floor, recency_days, causal_text) -> None:
    # Computes Confidence via confidence.assess (or abstain_confidence when evidence is None /
    # below floor) and writes serialized evidence_to_dict / confidence_to_dict onto the op dict
    # under "evidence" / "confidence". NEVER free-types a band.
```

Then make `review.py` able to review **op and authoring plans**, not just action plans:

- Add `review.review_ops_plan(plan, ...)` — iterates `plan["ops"]` (the control/authoring op key),
  skips ops with no `confidence` block (informational/structural), calls `review_recommendation` per
  op, and applies the demote-only verdict (per the `_apply_verdict` decision above). Idempotent
  skip-guard mirroring `review_action_plan` (an op already carrying a `review` block is left as-is).
- Add `review.review_authoring_plan(plan, ...)` — same, over the authoring `plan["ops"]`.
- The gate stays **demote-only and upstream of approval**: it may set the op non-executable, demote
  `status` approved→proposed, and lower the band — it must NEVER raise a band, promote status, or
  flip PAUSED-by-default. Pin this with tests for the op/authoring shape.
- NOTE: rotation plans do NOT use `plan["ops"]` (they use `plan["rotations"]` / `plan["items"]`).
  Rotation gets its OWN wrapper in the rotation ticket; do NOT try to make `review_ops_plan` cover
  rotation here.

### Enforce grounding at the gate, not just by convention

In `apply_ops_plan` and `apply_authoring_plan`, add a guard: an op whose `status == approved` but
which carries **no `confidence` block** (and is a capability that must be grounded — i.e. anything
that changes spend/delivery/structure) is treated as `blocked` with reason "approved write missing
required evidence/confidence." Decide the exact "grounding-required" set and document it (pure
`rename` and informational ops may be exempt — justify). This closes the hole where a hand-edited
plan could approve an ungrounded write.

## TODO

- VERIFY `review.review_recommendation` is already generic (reads evidence+confidence+`action_type`
  only); confirm an op dict without `action_type` flows through (direction check no-ops). Do NOT
  refactor coupling that does not exist.
- Decide `_apply_verdict` reuse vs `_apply_op_verdict` (see verified-facts section); implement and
  pin with a test.
- Implement `attach_op_grounding` (shared, computes band via `assess`/`abstain_confidence`,
  serializes onto the op).
- Add `review.review_ops_plan` / `review.review_authoring_plan` over `plan["ops"]` (skip ops without
  a confidence block; demote-only; idempotent skip-guard).
- Add the "approved write must carry confidence" guard to `apply_ops_plan` /
  `apply_authoring_plan` for the grounding-required op set; emit `blocked` with a clear reason.
- Tests (mock-only, FakeMetaReader/FakeClient): grounding attach computes (not free-types) a band;
  review demotes an over-claimed op band; an approved-but-ungrounded op is blocked; rename/
  informational exemption holds; demote-only invariants for the op/authoring shape; PAUSED-by-default
  untouched by the gate; the chosen `_apply_verdict` key behavior is asserted and audit-log-safe.
- Update `docs/META_ACTION_WORKFLOW.md`: every write path (ops + authoring + rotation) now carries
  evidence/confidence and is reviewed; document the grounding-required set.
- `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Ops with no natural metric** (e.g. `rename`, a structural `set_status PAUSED` for safety) — these
  can't ground on a performance metric. Decide: exempt from grounding-required, OR attach an
  `abstain_confidence` with a factor explaining "structural/no-metric op." Pick one and be consistent;
  do NOT let them silently carry a fabricated high band.
- **Evidence absent → must abstain, not fabricate** — when a caller can't supply a sample
  (`sample_purchases`/`sample_spend` None), `attach_op_grounding` must route through
  `abstain_confidence`, yielding an `abstain` band that the gate turns into `insufficient`
  (non-executable). Never default to `low`/`medium`.
- **Window format** — `review.py` parses `YYYY-MM-DD..YYYY-MM-DD`; any evidence built here must use
  that format or the `window_length` check silently no-ops. Pin with a test.
- **Gate must not promote** — an op arriving `status=approved` with a strong claimed band that the
  recompute lowers must end up demoted AND, if it lands on abstain, become non-executable. Confirm
  the gate cannot turn a `proposed` op into `approved`.
- **`direction` check on ops** — `review_recommendation`'s direction check needs `action_type` +
  `policy.target_roas`; op dicts lack `action_type`, so it won't fire for ops. Document that op-level
  direction-contradiction is therefore NOT caught at this layer (the per-capability tickets that know
  the semantic — e.g. budget scale-up vs ROAS — must supply an `action_type`-equivalent or their own
  direction guard).
- **Decoupling** — `review.py`, `confidence.py` must stay pure (no Meta/network/clock/IO per the
  determinism invariant). The grounding helper passes `recency_days` in; it must not call
  `datetime.now()` inside the pure modules. Live-state reads (to build evidence) happen in the
  caller, not in the pure layer.
- **Idempotency** — running the gate twice on a plan that already has `review` blocks must be a no-op
  (mirror `review_action_plan`'s skip-guard). Test re-review.
- **Audit-log compatibility** — adding `evidence`/`confidence`/`review` keys (and any
  `executable`/`verdict` injected by `_apply_verdict`) to op dicts must not break
  `write_ops_results` / `write_authoring_results` (they serialize op_id/status/request/response/
  reason). Confirm the extra keys ride along harmlessly.