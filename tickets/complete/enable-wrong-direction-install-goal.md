description: Verified the two new winner-side tests for the install-goal enable direction gate — they are correct, non-vacuous, and the full suite is green.
prereq:
files: tests/test_meta_ads_analysis.py
difficulty: easy
----
## Summary

Test-only completion ticket. The production gate (`_install_direction_contradiction` in
`src/meta_ads_analysis/review.py`, enable branch ≈ lines 493–497) shipped earlier via the
`review-gate-install-goal-direction` prereq. The implement stage added the two missing polarity-mirror
tests covering the "good ad stays trusted" side of the install-goal enable check:

- `test_enable_ads_install_goal_at_target_cost_enable_stands` — $120 / 40 installs = $3.00, target $3.00.
- `test_enable_ads_install_goal_below_target_cost_enable_stands` — $80 / 40 installs = $2.00, target $3.00.

Both assert `action_type == "enable_ad"`, `metric_name == "cost_per_app_install"`, the exact
`metric_value`, `verdict == "stands"` (and `!= "refuted"`), and `"direction" not in failed_inputs`.

## Review findings

**Diff read first (fresh eyes), then handoff.** Implement commit `4494857`: +47 lines, two tests, no
production change. Confirmed `review.py` was not touched.

- **Correctness of the production branch the tests target** — Read `review.py:466–508`. The enable branch
  is `if action_type in _ENABLE_ACTIONS and cost > target` (strict, lower-is-better cost-per-install
  polarity, the inverse of the ROAS mirror). Matches the implement handoff and ticket claims exactly.
- **Arithmetic / float precision** — $120/40 = 3.0 and $80/40 = 2.0 are both float-exact; no rounding
  risk in the `metric_value` assertions or the `cost > target` boundary.
- **Vacuousness (the key adversarial check)** — The tests are NOT no-ops. The sibling
  `test_enable_ads_install_goal_above_cost_target_is_refuted` proves the branch is reachable for this
  exact policy/shape and yields `refuted`; therefore the at-target test genuinely guards a `>` → `>=`
  slip (cost==target would flip to refuted), and the below-target test guards any over-broad refute.
- **Floor interaction** — 40 installs clears the conversions floor, so the verdict is `stands`, not the
  rank-3 `insufficient` that would otherwise mask the direction outcome. Confirmed by passing tests; the
  below-floor path stays pinned by `..._cold_ad_with_target_stays_insufficient_not_refuted`.
- **Selector-drift guard** — Each test asserts `metric_name == "cost_per_app_install"`; if the grounding
  selector regressed, `metric_value` would be `None` and the check would silently not fire — the explicit
  assertion catches that.
- **failed_inputs pollution** — `"direction" not in failed_inputs` catches a future change that appends a
  `direction` finding which loses the `max`-rank tiebreak yet still pollutes `failed_inputs`.
- **Edge / error / regression paths** — ROAS enable, scale, pause, and budget-cut paths are untouched by
  definition (no production edit); whole-suite sweep confirms no regression.
- **Docs** — No docs reference these specific test names; the production behavior they pin is unchanged,
  so nothing was stale to update.
- **Minor (checked, not flagged as a fix)** — The `verdict != "refuted"` assertion is redundant given the
  adjacent `== "stands"`; left as-is as intentional readability documentation, consistent with the
  refute sibling's style. Not worth an inline change.
- **Lint / type-check** — None configured in this repo: no `ruff`/`mypy`/`flake8` in `.venv`, none in
  `AGENTS.md` or `pyproject.toml` (`lint_vault` is an app feature, not source linting). Pytest is the
  gate.
- **Tests run** — `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **363 passed**.

**Major findings:** none — no new tickets filed.
**Minor findings:** one (redundant-but-intentional assertion); left as-is, no fix needed.

## Outcome

Verified and complete. No production code touched; no follow-up tickets required.
