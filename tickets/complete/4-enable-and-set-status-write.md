description: Turning an ad on or off now has to carry the numbers and a confidence rating behind it, and pass an automatic second-opinion check, before an operator can approve it — so no one can flip an ad live (or off) on a hunch with no evidence.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What shipped

The two `set_status` builders in `control.py` — `build_enable_ads_plan` (CLI `propose-enable-ads`)
and `build_pause_plan` (CLI `propose-pause-ads`) — are now grounded producers reconciled onto the
shared write-grounding scaffold. Each `set_status` op carries an `evidence` block + a **computed**
`confidence` band, the plan sets `guardrails.requires_grounding: true`, and the plan is run through
`review.review_ops_plan` before it is returned. The enable/pause asymmetry at the no-data boundary
(enable a cold ad → cites a zero sample → abstain → gate **blocks**; structural/safety pause → cites
no sample → structural abstain → gate **allows**) is enforced by `write_grounding.op_grounding_gap`.
No new op or capability was added — the existing reversible `set_status` controls were reconciled, per
the LOCKED scope.

Code landed in commit `59ee6ec` (runner error-recovery commit); three later tickets landed on top of
it green. The review pass below treats the integrated, test-passing code.

## Review findings

Reviewed the full diff of `59ee6ec` (control.py, cli.py, tests, docs) against `write_grounding.py`,
`review.py`, `confidence.py`, `actions.py`, and `apply_ops_plan`'s gate wiring, with fresh eyes before
the handoff summary.

**Correctness / asymmetry crux — checked, sound.** Traced the enable cold-ad path
(`cold_cites_zero=True`, `metrics_row=None`) end to end: cites `sample_*=0.0` → `attach_op_grounding`
sees a sample (`0.0 is not None`) → `assess` abstains (below floor) → `op_grounding_gap` keys "block"
on *any* cited sample including zero → write blocked even when approved. The structural-pause path
(`cold_cites_zero=False`, no metric) cites `sample_*=None` → `op_grounding_gap` treats it as a
structural abstain → allowed. The cited-zero→block vs no-sample→allow distinction is the intended
boundary and is faithfully enforced. Verified `set_status ∈ GROUNDING_REQUIRED_OPS` so the gate
actually fires.

**Recency faithfulness — checked, sound.** Builder computes `recency_days = run_dt - window_end` and
stores `plan["run_date"]` + `evidence["window"]`; `review._recency_days_from_window` re-derives the
same value from those fields, so the gate's band recompute matches the producer's.

**Provenance — checked.** Confirmed `59ee6ec` carries the actual code (the `2790a47` "implement"
commit only moved ticket files); three downstream tickets are integrated on top, suite green.

**Only two set_status op producers — confirmed.** Grep over `src/` shows lines 772 (enable) and 1226
(pause) in control.py are the only control-ops `set_status` constructors. `actions.py:442` builds an
action-plan recommendation (different shape, its own grounding via `_attach_confidence`), not a
control op — separate, intact path.

**Tests — extended.** Implementer's 8 tests cover the happy path, cold-ad block, thin-new-ad abstain,
overclaim demotion, idempotency, and the pause structural-allow boundary. Gaps found and **fixed
inline**:
- Added `test_enable_ads_install_goal_grounds_on_cost_per_install` — the install-goal branch of
  `_status_metric` (cost-per-install) reached through the enable builder was entirely untested; it now
  pins metric selection, the `cost_per_app_install` computation in `fetch_entity_metrics`, and the
  resulting `low`/`stands` behavior.

**Docs — checked, one drift fixed inline.** `docs/META_ACTION_WORKFLOW.md` § "Enabling and pausing
ads" accurately describes the evidence/confidence, the asymmetry, the direction-check note, and
re-read drift. The `fetch_entity_metrics` docstring had not been updated for the new
`app_installs`/`cost_per_app_install` return keys — corrected.

**Systemic limitation — noted, not a regression, out of scope.** For install-goal accounts the sample
strength is judged on `purchases` (the conversion sample), not `app_installs`, so an install enable
caps at `low` regardless of install volume. This is the **same** convention `actions.evaluate_action_
confidence` already uses repo-wide (`sample_purchases=total_purchase_count` for all goals), so it is
pre-existing and consistent, not introduced here. Left as-is.

**Major finding — filed, not fixed inline.** The review gate's `direction` check never fires on
enables (ops carry no `action_type`), so re-enabling an ad whose cited ROAS is clearly below the
account target on a strong sample is *not* actively refuted — only prevented from being
over-confident. The implementer flagged this as an accepted interaction and asked the reviewer to
decide. It is a real semantic gap but needs a product/semantics decision (is a below-target enable
"wrong-direction"? hard refute vs band-cap? install-goal handling?), so it is routed to
`tickets/backlog/enable-wrong-direction-refutation.md` rather than patched inline. Not a regression —
pre-grounding, enables had no direction check at all.

**Empty categories.** No findings in: type safety (helpers are fully annotated and pure), resource
cleanup (no I/O or handles introduced; reads go through the reader provider), circular imports
(`control` → `review` verified clean — `review` is pure), or input mutation (`review_ops_plan`
deep-copies; verified by `test_review_ops_plan_demotes_overclaimed_enable` asserting the input plan is
untouched).

## Validation

`.venv/bin/python -m pytest tests/ -q` → **279 passed** (was 278; +1 inline test). No type
checker / linter is configured in `pyproject.toml`.
