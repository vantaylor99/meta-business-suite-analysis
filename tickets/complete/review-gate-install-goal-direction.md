description: The automatic second-opinion check that catches "you're scaling something that's actually losing money" now works for app-install accounts too, not just revenue-goal accounts — reviewed and shipped.
prereq:
files: src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What shipped

The review gate's `direction` check, previously ROAS-goal-only, now also fires for install-goal
accounts (`primary_goal == "maximize_in_app_subscriptions"`) on the inverted cost-per-install polarity
(lower-is-better). `_direction_contradiction` was refactored into a goal dispatcher plus two pure
sibling helpers — `_roas_direction_contradiction` (verbatim prior ROAS body) and
`_install_direction_contradiction` (the new branch). New constant `_INSTALL_GOAL`. The install branch:

- Target source `policy["secondary_cost_per_app_install_target"]`; missing / non-numeric / `<= 0` → no fire.
- Cited-metric guard requires `metric_name == "cost_per_app_install"` + numeric `metric_value`.
- Refutes: scale-up / enable with `cost > target` (strict); pause / budget-cut with
  `cost <= target / 1.5` (inclusive). The polarity-inverted mirror of the ROAS branch.

Because the check is shared, the install branch lights up across all three plan surfaces
(`review_action_plan`, `_review_plan_ops`, `review_rotation_plan`) with no extra wiring. Docs
(`docs/META_ACTION_WORKFLOW.md`) updated to describe the goal-aware, two-polarity behavior.

## Review findings

Adversarial pass over commit `f4e36d1`. Read the implement diff first, then traced every consumer.

### Verified correct (no change needed)
- **ROAS branch is a verbatim move.** The pre-ticket `_direction_contradiction` body is byte-identical
  to the new `_roas_direction_contradiction`; the full pre-existing ROAS regression sweep is green.
- **Polarity is a faithful mirror.** Strict `>` for scale/enable, inclusive `<=` for pause/cut at
  `target / 1.5`; at-target stands, neutral zone symmetric with ROAS. Boundary tests pin both edges.
- **Metric name & policy key are consistent** across `actions._select_action_metric`,
  `control._status_metric`, `briefs.py`, `normalize.py`, and `config/meta_ads_accounts.json`
  (`cost_per_app_install` / `secondary_cost_per_app_install_target`). The install metric is genuinely
  what every surface cites for install goals — the gate will fire on real plans.
- **Dispatcher is complete.** The system has exactly two `primary_goal` values (`roas`,
  `maximize_in_app_subscriptions`); both are routed, and `None`/other goals correctly return `None`.
  No goal is silently skipped.
- **Verdict precedence is sound.** `insufficient`(3) outranks `refuted`(2), so the refutation only
  becomes the headline verdict when the cited sample clears the spend floor (otherwise it stays
  `insufficient` with `"direction"` still recorded in `failed_inputs`). Confirmed via the enable test
  and the new budget tests. `_num` handles strings/None/empty robustly.
- **Refuted never sets `revised_band`** (warning, not band-cap) — confirmed on all tested surfaces.

### Minor — fixed inline this pass
- **Budget-op control surface had only transitive coverage.** `_budget_op`'s `action_type` +
  `_status_metric`'s `cost_per_app_install` + the dispatcher were each verified independently but no
  test drove the *composition* on an install goal. Added two end-to-end integration tests through
  `build_budget_plan`: `test_build_budget_plan_install_goal_refutes_scale_up_above_cost_target`
  (cost $4 > $3 → refuted, `metric_name`/`metric_value`/`action_type` asserted) and
  `..._refutes_cutting_a_clear_winner` (cost $1.50 ≤ $2 margin → refuted). Both pass; this closes the
  largest "Known gaps" item the implementer flagged.
- **Stale comments** on `_SCALE_ACTIONS` and `_SCALE_DOWN_BUDGET_ACTIONS` still described ROAS-only
  behavior while their siblings (`_ENABLE_ACTIONS`, `_PAUSE_WINNER_MARGIN`) had been de-staled. These
  sets are now consumed by both polarity branches — rewrote both comments to be goal-aware.

### Checked, out of scope — no change (deliberately, with reason)
- **`control.build_pause_ads_plan` pause op sets no `action_type`** (and selects ROAS-only via
  `roas_below`), so the pause-winner direction check never fires on that *ops* surface. This is
  pre-existing, symmetric (it affects ROAS identically), and harmless: that builder by construction
  only pauses losers, so no self-contradicting pause can be generated there for the gate to catch. The
  install pause-winner path is exercised on the action-recommendation surface (which does carry
  `action_type="pause_ad"`) via the standalone `review_recommendation` tests. Not this ticket's scope.
- **`target <= 0` guard asymmetry.** The install branch guards `target <= 0`; the ROAS branch guards
  only `target is None`. This is an intentional, safe divergence (a 0/negative cost target would make
  every scale refute). A 0/negative `target_roas` is a pre-existing minor quirk, out of scope here.
- **Install ops cap the conversion sample at `purchases`, not installs**, so the band caps at `low` —
  documented known behavior; the refutation still surfaces because it outranks a downgrade. Unchanged.
- **No e2e test on the ad-action recommendation surface for install** remains. Lower risk than the
  budget-op gap (the action plan is the standalone-tested path over goal-agnostic plumbing); left as-is.

### Tests / lint
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **301 passed** (299 at the implement
  commit; +2 added this review). System `python` lacks deps — only `.venv/bin/python` runs the suite.
- **Lint: none configured** in the repo (`pyproject.toml` has only `[tool.pytest.ini_options]` /
  `[tool.setuptools*]`; no ruff/flake8/pylint/tox). Consistent with the prior review-gate ticket.

### Major findings warranting new tickets
None.

## Acceptance criteria (status)

- [x] `_direction_contradiction` refutes install scale/enable-above-target and pause/cut-below-target;
      ROAS behavior unchanged (verbatim move + green ROAS sweep).
- [x] Conservative: fires only with a numeric positive `secondary_cost_per_app_install_target` and a
      cited `cost_per_app_install`; otherwise silent.
- [x] `pytest tests/test_meta_ads_analysis.py` green (301 passed).
- [x] Budget-op install composition now covered end-to-end (review addition).
- [x] Comments and docs reflect the goal-aware, two-polarity reality.
