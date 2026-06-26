description: A shared safety framework was added so that other kinds of account changes (pausing, budgets, creating campaigns, audience swaps) can carry the same evidence, confidence rating, and automatic second-opinion check the existing pause/scale recommendations already carry — and a gate blocks an approved change that lacks that grounding.
prereq:
files: src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/authoring.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## What landed

The **shared grounding scaffold** so per-capability write tickets (enable/set-status, CBO budget,
authoring, rotation) can attach evidence/confidence/review uniformly. Adds **no new write capability**
and does **not** wire the existing builders — downstream per-capability tickets own that.

- `src/meta_ads_analysis/write_grounding.py` (pure): `attach_op_grounding` (computed band via
  `confidence.assess` / `abstain_confidence`, never free-typed) + `op_grounding_gap` (pure apply-gate
  helper).
- `review.py`: `review_ops_plan` / `review_authoring_plan` + `_review_plan_ops` / `_apply_op_verdict`
  — demote-only, idempotent, deep-copy (input never mutated), op-vocabulary verdicts (status +
  `review_verdict`, never `executable`/`rationale`).
- `control.apply_ops_plan` / `authoring.apply_authoring_plan`: opt-in apply-time guard
  (`guardrails.requires_grounding`) over `GROUNDING_REQUIRED_OPS` (= `SUPPORTED_OPS - {rename}`) /
  `GROUNDING_REQUIRED_KINDS` (= all `CREATE_KINDS`). Creates still forced PAUSED regardless.
- `docs/META_ACTION_WORKFLOW.md`: "Grounding on every write path" section.

## Review findings

Adversarial pass over the implement diff (3c1eaf4). Read the source diff first, then traced every
dependency (`confidence.assess`/`abstain_confidence`/`combine_bands`, `review_recommendation`,
`_direction_contradiction`, `_deepcopy_plan`, `_window_bounds`, `evidence_to_dict`).

**What was checked and found:**

- **Demote-only invariant (safety-critical) — VERIFIED.** `_apply_op_verdict` has no path that raises a
  band or promotes a status. DOWNGRADE caps both axes via `_min_band_name` (which `combine_bands`-floors
  against the revised band); INSUFFICIENT pins band/data_band to `abstain`; status only ever moves
  `approved→proposed`, guarded by `if op.get("status") == "approved"`. STANDS returns early untouched.
  Matches the action-plan `_apply_verdict` semantics minus the action-only `executable`/`verdict`/
  `rationale` keys (correctly omitted — op vocabulary).
- **Input immutability — VERIFIED.** `_review_plan_ops` operates on `copy.deepcopy(plan)`; tests confirm
  the source plan keeps its original band and gains no `review` key.
- **Idempotency — VERIFIED.** Skip-guard (`isinstance(op.get("review"), dict) and op["review"]`) fires
  on any prior review block, including a STANDS block. `twice == once` holds.
- **`direction` check no-op for ops — VERIFIED safe.** Ops carry no `action_type`;
  `_direction_contradiction` returns `None` (no crash). `VERDICT_REFUTED` is therefore unreachable for
  ops today — dead but harmless and future-proof.
- **Apply-gate semantics — VERIFIED.** `op_grounding_gap`: missing/blank band → block; `abstain` +
  cited sample (`sample_purchases`/`sample_spend is not None`, so `0.0` correctly counts as cited) →
  block; `abstain` + no sample (structural, e.g. safety PAUSE) → allow. Guard runs before
  `validate_op`; status-skip runs before the guard, so a review-demoted op is `skipped`, not `blocked`.
- **Purity / one-language — VERIFIED.** `write_grounding` imports only `confidence`; `review` imports
  `config` + `confidence`. No I/O / clock / network. No second confidence scale — every band routes
  through `confidence.assess` / `abstain_confidence` / `combine_bands`.
- **Window format — VERIFIED.** `YYYY-MM-DD..YYYY-MM-DD` is what `_window_bounds` parses
  (`partition("..")`); `REVIEW_MIN_WINDOW_DAYS=7`, so the 14-day test windows stand and the 3-day
  windows downgrade, exactly as the demote/idempotency/authoring tests assume.
- **Audit-log safety — VERIFIED.** Op-dict extras (`evidence`/`confidence`/`review`/`review_verdict`)
  are JSON-serializable and never reach the result log (`write_ops_results` serializes only `OpResult`
  fields). Pinned by `test_op_grounding_review_keys_are_audit_log_safe`.
- **Docs — VERIFIED accurate.** Confirmed against source: rotation really uses
  `plan["rotations"]`/`plan["items"]` (not `plan["ops"]`); the grounding-required sets and the
  `requires_grounding` opt-in guard are described faithfully.

**Two decisions the implementer flagged — both CONFIRMED acceptable:**
- *Opt-in `requires_grounding` (not unconditional).* Correct scoping: unconditional would block every
  currently-ungrounded approved write and break ~7 existing apply tests. The flag keeps legacy plans
  working and lets per-capability tickets flip it + attach blocks atomically. The hole it closes
  ("within a grounded plan, approve an op whose block was stripped") is the right one for a scaffold.
- *Apply-gate case (b) blocking `abstain`-with-cited-sample.* Acceptable: gated behind the opt-in,
  realizes the ticket's thin-data edge case at the apply layer, and preserves structural pauses.

**Minor — fixed inline (this pass):**
- Added `test_attach_op_grounding_no_evidence_keeps_full_evidence_keyset`. `write_grounding`'s
  `_empty_evidence_dict` hand-duplicates the `evidence_to_dict` key set, and its docstring promises
  keyset parity, but no test pinned it — a future field on `Evidence` would silently diverge the
  "no evidence" serialized shape. The new test asserts the no-sample evidence dict's keys equal a real
  `evidence_to_dict(...)`'s keys. Left the helper as an explicit literal (more readable than a
  positional empty-`Evidence` round-trip); the test now guards the invariant.

**Major — none filed.** The known gaps (builders/rotation not wired, op-level direction contradiction
not caught, `run_date` absent on control/authoring plans) are the explicit, documented scope boundary
of a scaffold ticket — the downstream per-capability tickets own them. They are not defects in this
change, so no new fix/plan tickets were spawned.

**Lint/type:** repo configures no linter or type checker (`pyproject.toml` has only pytest config;
`ruff`/`mypy`/`pyright` absent from `.venv`). Tests are the gate.

## How to validate

`.venv/bin/python -m pytest tests/ -q` → **228 passed** (227 from implement + 1 added this pass).

## End
