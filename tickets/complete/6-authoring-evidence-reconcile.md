description: Proposals to create a new campaign, ad set, or ad now also carry the facts and confidence justifying why they're worth building and pass an automatic second-opinion check — while still always being created switched-off so they never spend on their own.
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/write_grounding.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What landed

`authoring.py` was reconciled onto the grounded-write scaffold (`write_grounding` + `review`) without
rewriting the create mechanics. Every authoring op now carries an `evidence` + **computed** `confidence`
block, every plan opts into `guardrails.requires_grounding`, and every plan is run through
`review.review_authoring_plan` before it is returned. PAUSED-by-default, `_guard_params` /
`FORBIDDEN_FRAGMENTS`, and the create-only scope are untouched.

Three grounding shapes (see `build_*` in `authoring.py`):

- **Duplicate / scale-out** (`build_duplicate_ad_plan`) — evidence is the *source ad's* own metric over
  the window, read via `control.fetch_entity_metrics`. Proven winner → real computed band (executable);
  undelivered source → cited-zero sample → abstain → blocked.
- **Net-new spending create** (`build_video_ad_plan`, hand-authored campaign/ad set/ad) — cites a *zero*
  sample → abstain → review marks it `insufficient` → the apply-time gate blocks an **approved** net-new
  create. Conscious override = drop `requires_grounding` (or ground via a duplicate). Mirrors the cold-ad
  enable boundary.
- **Lookalike** (`build_lookalike_plan`) — seed size/quality is not a ROAS/conversions metric, so it
  cites *no* sample (a structural abstain). Structural abstain is gate-**allowed** (audiences are inert:
  no status, not in `PAUSED_KINDS`, never spend).

## How to validate

- `.venv/bin/python -m pytest tests/ -q` → **258 passed**.
- No standalone lint tooling is configured in this project (no ruff/mypy/flake8 in the venv or in
  `pyproject.toml`); `python -m py_compile` on the touched modules passes.

## Review findings

Adversarial pass over commit `802a58e` (implement). Read the full diff fresh before the handoff.

### Checked — and OK

- **Safety invariants intact.** `_build_create` still hardcodes `status=PAUSED` for every
  `PAUSED_KINDS` kind regardless of any review verdict (verified the high-confidence-`stands` duplicate
  still creates PAUSED). The `_guard_params` / `FORBIDDEN_FRAGMENTS` Advantage+/Meta-AI block fires even
  on a well-grounded create. Create-only scope (no delete/archive) unchanged.
- **Gate logic matches the three grounding shapes.** Traced `write_grounding.attach_op_grounding`
  (sample present → `assess`; no sample → `abstain_confidence`, never free-typed) and
  `op_grounding_gap` (cited-zero abstain → blocked; structural abstain → allowed; missing confidence →
  blocked). Net-new and undelivered-duplicate cite a zero sample (blocked); lookalike cites `None`
  (allowed). Correct.
- **Producer/gate parity.** The plan carries `run_date` + `account_action_policy`, so `review`'s recency
  (`_recency_days_from_window`) and band recompute (`_recompute_band` → `confidence.assess`) reproduce
  the producer's inputs (`CREATE_SPEND_FLOOR == MIN_WASTE_SPEND`, the gate's default) — no spurious drift
  downgrade. The proven-winner case recomputes the same band → `stands`.
- **Demote-only review gate** confirmed in `review.py` (`_apply_op_verdict` only lowers a band / demotes
  approved→proposed; never raises/promotes). Idempotency verified (already-reviewed ops are skipped).
- **`metric` normalization.** `fetch_entity_metrics` maps `ad_id`→`id` and extracts `purchases` from
  `actions`, so the duplicate's `id`-match and `sample_purchases` are correct.
- **Callers / surface.** Only `cli.py` calls the builders, and all three CLI mains were updated with
  `--date-from`/`--date-to` + `run_date`. `__main__` dispatch unchanged. No MCP/server tool wraps these
  builders. `as_reader` is idempotent, so the duplicate builder's double-wrap is a harmless no-op.
- **Serialization / audit-log safety** tested (grounded plan round-trips; result log keeps only
  op_id/kind/status/created_id — grounding keys do not leak).

### Found and fixed (minor — fixed in this pass)

- **Missing edge-case test.** The proven-winner duplicate was tested, but its documented symmetric
  safety case — duplicating an *undelivered* source (no insights row → cited-zero abstain → blocked at
  apply) — had no test asserting the block; only a shape-only test exercised that branch. Added
  `test_build_duplicate_ad_plan_abstains_when_source_undelivered` (258 passing).

### Found — noted, not fixed (out of scope / pre-existing / by-design)

- **Hand-authored net-new campaign/ad-set plans are grounding-gated only if they set
  `guardrails.requires_grounding`.** There are no `create-campaign` / `create-adset` CLI proposers
  (the implementer deliberately did not invent commands), so a hand-authored plan that omits the
  guardrail is created without grounding enforcement. PAUSED-by-default still holds for those, so there
  is no spend risk. This matches the implementer's documented scope interpretation; a future ticket
  could add grounded proposers if those create paths are wanted.
- **A hand-edited, fabricated confidence band can bypass the apply-time gate.** `op_grounding_gap`
  trusts the stored band (only `abstain`-with-sample and missing-confidence fail closed); the *review*
  gate is what recomputes from evidence. This is a pre-existing property of the shared `write_grounding`
  scaffold (the `guarded-write-evidence-scaffold` prereq), not introduced here — and builders always run
  review, which would demote a drifted band. PAUSED still holds regardless. No change.
- **`--validate-only` of a net-new abstain op is also blocked** (the grounding gap is checked before the
  execute/validate branch in `apply_authoring_plan`). Consistent with the scaffold and safe (the
  override path lets an operator validate); flagged as a minor UX note, not a defect.
- **Builders are now mildly impure** (default window via `control._resolve_grounding_window` →
  `date.today()`); documented in the handoff, mirrors `control.build_budget_plan`, no live Meta calls.
- **`authoring` imports private helpers from `control`** (`_resolve_grounding_window`, `_status_metric`,
  plus `fetch_entity_metrics`, `resolve_action_policy`). Acyclic (`control` never imports `authoring`),
  matches the existing cross-module-private style. Acceptable.

### Major findings

None — no new fix/plan/backlog tickets filed.

## Out of scope (unchanged, confirmed)

No delete/archive. PAUSED-by-default (`PAUSED_KINDS` + hardcoded `status=PAUSED`). Meta-AI/Advantage+
block. The review gate stays demote-only and never touches PAUSED.

## End
