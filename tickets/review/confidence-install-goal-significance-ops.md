description: When you turn an app-install ad on/off or move its budget, the system now measures how sure it is by counting installs (which those accounts actually produce) instead of purchases (which they almost never have), so a well-backed decision is no longer stuck at "low confidence."
prereq: confidence-install-goal-significance
files: src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What landed (implement → review handoff)

Both `control.py` grounding producers now select their **significance sample** by account goal, mirroring
what the action plan already does. Previously the metric was goal-aware (`_status_metric` →
`cost_per_app_install` for install goals) but the *sample* was always `purchases`, so a
`maximize_in_app_subscriptions` account (purchases ≈ 0) was structurally pinned at `low`/`abstain` no
matter how much install volume backed the decision.

### Code changes (`src/meta_ads_analysis/control.py`)

- **New helper `_status_sample_conversions(metrics_row, goal)`** (sibling of `_status_metric`,
  control.py ~619): returns `app_installs` for `goal == "maximize_in_app_subscriptions"`, else
  `purchases`. Keys on the literal goal string ONLY — same as the action plan's selector. Returns
  `_num(...)` (`float | None`).
- **`_attach_status_grounding`** gained a keyword param `sample_conversions: float | None`. The
  **present-row branch** now cites `sample_purchases=sample_conversions` (was
  `_num(metrics_row.get("purchases"))`). **Both `metrics_row is None` branches are unchanged** — they
  still cite `None` (structural) / `0.0` (cold) regardless of goal. The sample is chosen by the CALL
  SITE (not inside the helper) precisely because this function grounds two callers with different
  metrics.
- **Enable call site** (`build_enable_ads_plan`, ~822): passes
  `sample_conversions=_status_sample_conversions(metrics_row, goal)` → agrees with the goal-aware metric.
- **`roas_below` pause call site** (`build_pause_plan`, ~1279): passes
  `sample_conversions=_num((metrics_row or {}).get("purchases"))` → agrees with the **hardcoded**
  `blended_roas` metric. **Byte-identical to pre-change behaviour for every goal** (present-row branch
  only runs when `metrics_row` is not None, so `(metrics_row or {})` == `metrics_row` there).
- **`_attach_budget_grounding`** present-row sample (~1391): now
  `sample_purchases=_status_sample_conversions(row, goal)`. It already had `goal` and a goal-aware
  metric, so metric and sample now agree.
- Comment/docstring updates only: the `_attach_status_grounding` "purchases/spend sample" →
  "conversions/spend sample" doc line, the `_attach_budget_grounding` "'9 purchases over 5 days'" →
  "'9 conversions over 5 days'" doc line, plus a new doc paragraph explaining the `sample_conversions`
  param. **The `sample_purchases` Evidence field / JSON key is intentionally NOT renamed** (that rename
  is owned by `confidence-sample-conversions-rename`).

## How to validate / test

`.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **361 passed** (no ruff/mypy/pyright is
configured; pyproject declares only pytest). `tests/` contains only this one file.

### Tests added (8) — all in `tests/test_meta_ads_analysis.py`

- `test_enable_ads_install_goal_zero_purchases_real_installs_clears_floor` — **core fix**: install goal,
  `purchases=0`, 120 installs → sample = installs → band `> low`.
- `test_enable_ads_roas_goal_ignores_app_installs_decoy` — ROAS goal with a 999-install decoy → sample
  stays purchases (30), band medium (decoy ignored).
- `test_enable_ads_install_goal_cold_ad_abstains_and_gate_blocks` — install-goal cold enable
  (`metrics_row=None`, `cold_cites_zero=True`) → cited zero → abstain → apply-time gate **blocks**.
- `test_enable_ads_no_goal_installs_present_keeps_sample_on_purchases` — intentional asymmetry: no goal →
  metric `cost_per_app_install` but sample = purchases (parity with action plan).
- `test_attach_status_grounding_none_row_ignores_sample_conversions` — **unit-level guard**: both
  `metrics_row is None` branches produce identical output for `sample_conversions ∈ {None, 0.0, 999.0}`
  (proves the None branches are goal-independent / untouched).
- `test_pause_roas_below_grounds_on_purchases_regardless_of_installs` — `roas_below` pause with an
  install decoy → sample stays purchases (4), metric `blended_roas`.
- `test_build_budget_plan_install_goal_grounds_sample_on_installs` — **core fix on budget surface**:
  install goal, no purchases, 120 installs → sample = installs → band `> low`, stands.
- `test_build_budget_plan_roas_goal_ignores_app_installs_decoy` — ROAS budget move with install decoy →
  sample stays purchases (120).
- `test_build_budget_plan_install_goal_no_installs_thin_row_abstains_and_blocks` — present row but
  `app_installs` absent + spend < floor → sample `None` while row present, sample still cited via
  `sample_spend` → abstain WITH cited sample → gate **blocks** (same shape as today's purchases-None
  present-row case).

### Existing tests updated (3) — these encoded the **pre-fix `low` band** and now read its post-fix value

- `test_enable_ads_install_goal_grounds_on_cost_per_install`: band `low → medium`; added an explicit
  `sample_purchases == 40.0` assertion.
- `test_enable_ads_install_goal_no_cost_target_not_direction_refuted`: band `low → medium` (verdict
  assertions unchanged).
- `test_enable_ads_install_goal_above_cost_target_is_refuted`: band `low → medium` (the refute verdict
  is direction-based and unaffected; only the band moved).
- Doc-only: `_bud_install_insights` docstring no longer claims "install ops cap at low band."

## Reviewer focus / known gaps & decisions to scrutinize

- **Sample/metric agreement per call site.** Confirm the three call sites each pass a sample that
  agrees with their metric: enable → goal-aware installs vs goal-aware metric ✅; `roas_below` pause →
  purchases vs hardcoded `blended_roas` ✅ (must NOT switch to installs even for an install-goal
  account); budget → goal-aware installs vs goal-aware metric ✅.
- **Structural-pause "install-goal" edge case (ticket listed it).** I did NOT add a
  `build_pause_plan(..., install goal)` test because `build_pause_plan` has **no `goal`/`policy`
  parameter** and the structural-pause path never consults the goal — it routes through the
  `metrics_row is None, cold_cites_zero=False` branch, which is untouched by the helper. Coverage is
  instead: the existing `test_pause_structural_abstains_but_gate_allows_safety_pause` (gate allows) PLUS
  the new `test_attach_status_grounding_none_row_ignores_sample_conversions` (proves the None branch is
  goal-independent). If a reviewer prefers an explicit end-to-end install-goal structural-pause test,
  that would require threading a goal into `build_pause_plan`, which is out of scope here. Flagging so
  it's a conscious decision, not an oversight.
- **Budget cold-row (`row is None`) for an install goal** is also not a standalone test: that branch
  cites `0.0/0.0` directly (not via the helper), so it is goal-independent — the enable cold test and
  the None-row unit test cover the equivalent shape. Confirm you agree this is sufficient.
- **`sample_purchases` key now sometimes holds installs.** The serialized JSON key / Evidence field is
  still named `sample_purchases` (rename deferred to `confidence-sample-conversions-rename`). Operator
  wording is correct regardless because `confidence.py` renders the sample as "conversions" (landed by
  the prereq `confidence-install-goal-significance`). So a downstream consumer reading the raw
  `sample_purchases` key on an install-goal op now gets an install count — intentional, but worth a
  scan for any consumer that assumes that key is literally purchases.
- **No subscription count threading.** `fetch_entity_metrics` rows carry no separate
  subscriptions/`results` count, so the install-goal selector collapses to `app_installs` (the
  action plan's subscriptions-first ladder cannot be replicated here). This is the plan-sanctioned
  default; threading a subscription count is explicitly a separate, larger ticket.
- **Band-arithmetic assumptions in new tests:** `CONFIDENCE_CONVERSIONS_FLOOR = 25`; ≥ floor →
  medium, ≥ 4× floor (100) → high; recent window (default enable window / the 1-day budget window) does
  not round down. If a reviewer changes the floor or window resolution, the `> Band.low` assertions
  still hold but the exact medium/high pins may shift.
