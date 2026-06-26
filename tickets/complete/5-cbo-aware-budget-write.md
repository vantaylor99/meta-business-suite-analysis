description: Budget changes now work when the budget lives at the campaign level (Meta's campaign-budget-optimization) instead of the ad set, can lower budgets as well as raise them (with a floor so they can't be cut to near-zero), and every budget move must carry evidence, a confidence rating, and pass the automatic second-opinion check.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/cli.py, pyproject.toml, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## What landed

CBO-aware, grounded, reversible budget writes. Three coupled deliverables ("CBO-aware budget +/-",
the CBO gap-fix, and budget decreases) all landed and reviewed. Full suite **250 passed** (249 from
implement + 1 review-added test).

Implementation summary (verified accurate against the diff):

- **CBO detection** — `control.classify_adset_budget` is the single shared classifier
  (`adset_level` / `cbo_active` / `broken`), called by both the ops path (`build_budget_plan`,
  `_build_budget_request`) and the actions path (`_populate_budget_params_from_live_state`), so a
  fixture classifies identically on both (parity contract, asserted by
  `test_actions_ops_cbo_classification_parity`).
- **Campaign redirect** — a CBO ad set yields a non-executable ad-set pointer op (`cbo_detected`) +
  an actionable campaign op carrying its OWN campaign-level evidence. Re-read at execute time blocks a
  CBO ad-set op even if it was approved (drift guard).
- **Decrease** — `_capped_budget_request` selects the cap by sign of `(new − current)`: increases use
  the unchanged op-param `max_increase_percent` (default 20); decreases use a separate
  `MAX_BUDGET_DECREASE_PERCENT` (default 50) AND the absolute `MIN_DAILY_BUDGET_CENTS` floor (100).
- **Grounding + direction** — every budget op carries `evidence` + a computed `confidence` band;
  below-floor samples abstain and are hard-blocked at apply. Budget ops set an `action_type` so
  `review._direction_contradiction` refutes both scaling up below target and cutting a clear winner.

## Review findings

### Scope of the review
Read the full implement diff (`7b11031`) with fresh eyes before the handoff, then traced every helper
it calls: `apply_ops_plan` gate ordering (status → grounding gap → `validate_op` → `_build_request`),
`_build_budget_request` (both levels, CBO re-detect, lifetime guard), `_capped_budget_request` (sign
selection, cap-then-floor ordering), `classify_adset_budget`, `build_budget_plan`,
`_attach_budget_grounding`/`fetch_entity_metrics` (metric → evidence wiring),
`review._review_plan_ops`/`_direction_contradiction`/`_apply_op_verdict`, and the actions-path
`_populate_budget_params_from_live_state`/`fetch_live_adset_state`/`ADSET_STATE_FIELDS`. Confirmed the
test fakes (`_ControlFakeClient`, `FakeMetaReader`) are non-vacuous and exercise real classification.

### Correctness / safety — checked, no major issues
- **Apply-time gating is sound.** A CBO/broken/lifetime ad-set op that is force-approved is blocked at
  `_build_request` (re-read), not mis-applied. The pointer op double-protects (grounding gate, then CBO
  block). Decrease cap-then-floor ordering is correct; increase never consults the floor (always above a
  positive current). `new == current` is a safe no-op write. `validate_op` runs before
  `_build_budget_request`, so the `int(_num(...))` of `daily_budget_cents` is guarded.
- **Direction tag uses the propose-time snapshot** while the real cap is enforced against the apply-time
  re-read — confirmed intentional and safe (the snapshot only picks the `action_type` label that review
  judges; the binding cap is the live re-read).
- **Parity contract holds** — both paths share `classify_adset_budget`; `ADSET_STATE_FIELDS` includes
  `campaign_id`/`daily_budget` so the real actions flow populates what the classifier reads.

### Minor findings — FIXED in this pass
1. **Stale docstring/comment in `review.py`.** `review_ops_plan`'s docstring and the inline comment at
   the `action=op` call still asserted "ops carry no `action_type`, so the direction check cannot fire
   here." Budget ops now DO set `action_type` and the direction check fires on them — the implementer
   updated the `_SCALE_ACTIONS` comment but missed these two. Rewrote both to state that budget ops are
   the exception that fires the check.
2. **Missing end-to-end write test for the campaign level — the core CBO-redirect deliverable.** The
   only campaign-level apply test blocked (lifetime). No test executed a successful campaign daily-budget
   write through `apply_ops_plan → _update_entity → update_campaign`. Added
   `test_apply_ops_campaign_daily_budget_executes` covering an increase (5000→5500) and a decrease
   (5000→4000), each asserting the exact `("campaign", "c1", {"daily_budget": ...}, False)` write.

### Observations — left as-is (deliberate, documented; not defects)
- **Gate asymmetry: abstain is hard-blocked at apply, refuted is not.** A below-floor (abstain) op is
  blocked at apply even if force-approved (grounding gap). A *refuted* op (e.g. scale-up below target) is
  marked `review_verdict: refuted` and demoted approved→proposed, but if a human re-approves it the apply
  path does NOT re-check the verdict and it executes. This is consistent with the established gate
  philosophy (data-sufficiency = hard gate; direction = advisory + demote + human-final-authority) and
  matches how `set_status` ops behave. Not a regression — budget ops are the first ops to carry a
  direction check at all. Flagging so it is a conscious contract, not an accident. If a future ticket
  wants refuted ops hard-blocked at apply, that is a deliberate gate-policy change, not a bug fix.
- **Direct campaign target with no budget** produces an op that looks actionable at propose but blocks at
  apply ("campaign has none"). Safe; only a mild propose-time UX gap.
- **Redundant ad-set read in the actions path** (`_populate_budget_params_from_live_state` re-reads via
  `classify_adset_budget` after `_maybe_add_live_adset_state` already read) — intentional, guarantees
  byte-identical classification with the ops path over saving one mocked read.

### MAJOR finding → no new ticket needed (already tracked downstream)
- **Config-constant overlap with ticket `write-config-registry-controls` (sequence 8, in `implement/`).**
  This ticket added `MIN_DAILY_BUDGET_CENTS` / `MAX_BUDGET_DECREASE_PERCENT` to `config.py` so it is
  self-contained. Ticket 8 (line 51) still says "Add" the same two constants, which would redefine them.
  No new ticket filed because: (a) ticket 8 already anchors both constants to "wired by
  cbo-aware-budget-write" and its agent will read the current `config.py`; (b) the implementer left a
  loud reconciliation NOTE beside the constants telling ticket 8 to detect-and-reconcile, not duplicate;
  (c) a Python module-level redefinition is last-wins, not an error — low severity. Ticket 8's
  implement-stage agent must reconcile to these definitions and only add the registry
  `max_budget_decrease_percent` field + reader-backend default. Left ticket 8 unedited per "touch only
  your own files."

### Lint / tests
- No linter or type-checker configured (`pyproject.toml` declares only pytest) — tests are the gate.
- `.venv/bin/python -m pytest tests/ -q` → **250 passed** (after the review edits).
- `propose-budget --help` parses (mutually-exclusive `--adset-id`/`--campaign-id`, required
  `--daily-budget-cents`); entry point registered in `pyproject.toml`.
- No `.pre-existing-error.md` written — no unrelated failures surfaced.

## End
