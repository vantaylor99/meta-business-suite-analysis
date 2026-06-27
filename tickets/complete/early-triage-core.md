description: Built and reviewed the early-life ad-triage engine that grades a struggling brand-new ad against how similar past ads on the same account behaved at the same age, so a genuinely-bad ad is told apart from a slow-starting eventual winner.
prereq:
files: src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/storage.py, tests/test_meta_ads_analysis.py
----

## Summary

Pure early-life ad-triage engine plus its DuckDB data seam. `triage_ad(...)` grades a struggling
brand-new (≈ day 1–3) ad against the account's own history of comparable new ads at the same age,
returning one of `not_struggling` / `abstain_keep` / `keep_watch` / `pause_candidate` (or `None` when
the ad is missing or past the early-life window). Clock-free (`as_of` is passed in). All SQL is
isolated in `DuckDBHistoryProvider`; the engine only ever sees `list[AdHistory]`. Confidence is built
through the shared `confidence.analog_confidence` (correlational tier, capped at medium). CLI/monitor
wiring and follow-up rendering are the sibling `early-triage-monitor-integration` ticket.

Build + tests: `python -m pytest tests/test_meta_ads_analysis.py` → **318 passed** (was 316;
+2 review tests). pytest is the only configured gate (no ruff/mypy/pyright in this repo).

## Review findings

**Diff reviewed first, with fresh eyes** (commit `c894691`): `early_triage.py` (new, 625 lines),
`confidence.analog_confidence`, the `config.EARLY_LIFE_*` block, and the 15 implementer tests — then
the handoff summary.

### Correctness — no bugs found
- **Verdict ladder & ordering** verified: missing/too-old → `None`; install-no-target abstain → before
  the struggling short-circuit; `not_struggling` short-circuits before any analog work; analog count
  gates `abstain_keep` vs `keep_watch`/`pause_candidate`. The `rate >= recovery_rate` keep boundary is
  inclusive, consistent with the spec.
- **Analog matching** (`_is_analog`): the three result-presence branches (both-have / both-zero /
  mismatch) are each correct; `_ratio_within` is a symmetric closed band `[tol, 1/tol]`; the
  "struggling-through-age" requirement and the `non_trivial_spend` floor are applied to candidates too.
- **Population accounting**: matched analogs must be old enough to judge (`last_age >= recovery_horizon`);
  too-short-lived matches are excluded entirely (don't read as "stayed bad"); `recovered ⊆ matched`;
  rate is over the whole matched population (survivorship-correct). `matched_ids` is sorted →
  deterministic verdict (confirmed by the equality test).
- **Goal mapping** matches `actions._select_action_metric` / `_should_pause_ad` exactly:
  `maximize_in_app_subscriptions` → `cost_per_app_install`, `roas` → `blended_roas`. `INSTALL_GOAL`
  sentinel matches the real `pollen_sense` policy.
- **Recovery window** `[age+1 .. horizon]` is cumulative and excludes the early struggling days — a
  documented design choice, not a defect.
- **Age** is purely `(as_of - first_seen).days` with clock-skew clamped to 0; no system clock anywhere.

### Resource cleanup / error handling / type safety
- `DuckDBHistoryProvider` uses `with storage.connect(...)` (connection closed on exit) and calls the
  idempotent `initialize_database` so a brand-new DB yields `[]` rather than erroring — acceptable on a
  read path. `_number`/`_float`/`_as_date` all coerce defensively (None/""/bad strings → safe default).
- Install-goal-without-target degrades to `abstain_keep` instead of crashing — verified by test.

### Tests — gaps closed inline (minor)
The implementer flagged two untested paths; both are now covered (218 → 318 suite still green):
- **`test_early_triage_result_presence_mismatch_is_not_an_analog`** — triaged-has-results vs
  zero-result candidates, and the mirror, both yield no analogs (`abstain_keep`). Closes the explicitly
  flagged `_is_analog` mismatch-branch gap.
- **`test_early_triage_ratio_tolerance_band_is_inclusive`** — analogs at exactly 2.0× / 0.5× spend
  match; a hair past either bound is excluded. Confirms the closed-interval boundary.

### Observations (left as-is; not defects — sanity-checked, not re-litigated)
- **`analog_confidence(recovered=...)` is an unused parameter.** Intentional API symmetry (the caller
  folds `recovered` into `factors`; the band depends only on population size). Left in place to avoid
  churning the integration ticket's call site — a documented smell, not a bug.
- **Install goal uses one threshold for both "struggling" and "recovered"** (the target), so it lacks
  the ROAS floor↔target dead-zone. Correct given install policy exposes only a target; noted for the
  operator. The `EARLY_LIFE_MIN_SPEND = $10` floor means a $5/day install ad isn't graded until ~age 2
  — a deliberate, configurable floor the implementer already flagged for operator confirmation.
- **`not_struggling` wording** is emitted even when the real reason is "below the $10 spend floor"
  (metric reads `n/a`). Cosmetic; the verdict and routing are correct (leave to normal flow).
- **`group_histories` would sum duplicate `(ad_id, report_date)` rows.** This mirrors the existing
  `analyze._summarize_ad` behavior (it also sums all per-ad rows without per-date dedup), and normalize
  emits one row per ad-day, so it is consistent with the codebase — not a regression.

### Deferred to the integration ticket (out of scope here, correctly)
- ROAS computed as `purchase_value/spend` directly rather than via `analyze._reliable_roas` (tracking
  confidence not honored); `AdDailyPoint.results` carried but unused by any decision; rendering of
  `analog_basis`/`reasons`/`evidence` and routing `pause_candidate` through the guarded propose flow.
  These are explicitly the `early-triage-monitor-integration` ticket's responsibility.

No major findings → no new fix/plan tickets filed. No pre-existing test failures observed; no
`.pre-existing-error.md` filed.
