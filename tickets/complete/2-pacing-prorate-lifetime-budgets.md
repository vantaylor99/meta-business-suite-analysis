description: The budget-pacing tool now gives lifetime-budget accounts a real over/under/on-track verdict by working out how much of the fixed budget should have been spent by now, instead of giving up on them.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md
difficulty: hard
----

## Summary

`pacing_report` used to classify any lifetime-only account `budget_not_projectable`. It now
**prorates** each ACTIVE lifetime pot across the inclusive-day overlap of the entity's own
`start_time..stop_time` schedule with the reporting window and folds that additively into the daily
period-budget math, so lifetime and mixed accounts earn `over`/`under`/`on_track`. Daily-only output
is byte-identical.

The implementation shipped in commit `3f73886` (`ticket(implement): pacing-prorate-lifetime-budgets`).
This review pass verified the math, closed two documented test gaps, and brought the docs current.

## Review findings

### What was checked
- **Read the implement diff first, fresh** (`account_discovery.py`, `mcp_server.py`, tests) before the
  handoff summary. Traced `summarize_account_budget` → `lifetime_pacing` → `pacing_report` →
  `classify_pacing` end to end.
- **Correctness of the proration + projection math** — hand-derived every e2e fixture.
- **Ratio bounds** — `overlap_full ≤ schedule_total` (both inclusive-day), so each pot's prorated
  contribution never exceeds the pot; `overlap_todate ≤ overlap_full` (as_of ≤ date_to), so
  `expected_to_date ≤ period_budget`. No blow-ups beyond the inherent projection behavior.
- **Byte-identical daily path** — the `else` branch keeps the literal `active_daily * total_days` +
  `project_spend`; the guard test `test_pacing_report_end_to_end_statuses_rollup_and_shortlists` is
  unedited and green.
- **Short-circuit ordering** in `classify_pacing` (`not_started` → `account_inactive` → `no_budget_set`
  → `budget_not_projectable` → verdict) against the relaxed projectability guard.
- **Currency/units** — lifetime entities carry major units out of `summarize_account_budget`
  (`_minor_to_major`), consistent with `active_daily`; normalized twins convert via the FX table.
- **Docs** — searched the repo for stale `budget_not_projectable` / "not projected" / proration prose.

### Findings & disposition

**Sound, no bug found.** The generalized projection is clean: `variance_pct` reduces to
`spend_to_date / expected_to_date − 1` in *both* the daily and combined paths (verified algebraically),
so the combined form is a strict, consistent generalization of the daily projection rather than a
divergent formula. Ratios are bounded, the CBO precedence and ACTIVE-gating are preserved, and the
`not_started` / `account_inactive` short-circuits correctly render the proration computed-then-ignored
(harmless) rather than leaking a verdict.

**Minor — fixed inline (test coverage).** The handoff flagged two reachable branches with no dedicated
test. Added `test_pacing_report_lifetime_residual_and_short_circuit_branches` covering, end-to-end
through `pacing_report`:
  - a **started-window** pure-lifetime account whose schedule overlaps the window but has not *started*
    as of `as_of` → `period_budget > 0` yet `expected_to_date == 0` → projection `None` →
    `budget_not_projectable` (the `expected_to_date == 0 with period_budget > 0` case previously only
    exercised at the `lifetime_pacing` unit level);
  - a control account with the same schedule but already started → real `over` verdict;
  - a **non-ACTIVE account** (status 3 = `UNSETTLED`) with a fully-projectable lifetime schedule →
    `account_inactive` wins, `variance_pct` is `None` (no verdict leaks), while `projected_spend` is
    still computed and reported as context — matching the pre-existing daily paused-account behavior
    (confirmed this is not new/regressed behavior).

**Minor — fixed inline (docs).** `README.md` still described lifetime-only accounts as
`budget_not_projectable` and called proration "a deliberate non-goal"/"future work". Rewrote that
paragraph to describe the proration (`lifetime × overlap ÷ schedule_total`, folded additively) and the
residual `budget_not_projectable` cases, and corrected the projected-spend formula prose (daily form
vs. the schedule-aware `spend × period_budget ÷ expected_to_date`). The `summarize_account_budget` /
`pacing_report` / `classify_pacing` docstrings and the `mcp_server.py` LLM tool description were
already updated by the implementer and verified accurate.

**Not touched (out of scope / correct as-is).**
  - `tickets/complete/3-mcp-pacing-report.md` mentions the old behavior but is an archived record of
    prior work — not a live doc; left as historical.
  - **Account-level spend vs. per-entity schedules** — a mixed/multi-lifetime account still has one
    account-level `spend_to_date` driving one blended `variance_pct`; spend cannot be attributed to
    individual entities. This is the intended account-level design (per the source ticket) and is a
    pre-existing limitation (before this ticket *all* lifetime was ignored, so any daily+lifetime
    account already blended spend). A mixed account carrying an *open-ended* lifetime campaign will
    project daily-only while its `spend_to_date` includes the open-ended campaign's spend, which can
    tilt the verdict; this is inherited, not introduced, and is bounded by the account-level design.
    Noted, not fixed — no behavior change warranted here.
  - **Timezone** — window and schedule bounds are naive calendar days (`str(value)[:10]`), consistent
    with `pacing_period` and the rest of the tool. Intentional simplification, unchanged.

### Lint / tests
- No mypy/ruff/flake8 configured in `pyproject.toml` (only `[tool.pytest.ini_options]`); ran an
  `ast.parse` syntax smoke check on both touched Python files (OK).
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **602 passed** (was 601; +1 review
  test). `-k "pacing or lifetime"` → 22 passed. (`python` is not on PATH — use `.venv/bin/python`.)

## Acceptance (all met)
- Lifetime-only account with an overlapping schedule → real `over`/`under`/`on_track` grounded in the
  prorated expectation, entering status_counts / shortlists / normalized totals.
- Daily accounts unaffected — byte-identical e2e guard test unedited & green.
- Residual `budget_not_projectable` cases (open-ended / non-overlapping / not-yet-started / cap-only)
  now covered end-to-end; short-circuit interactions covered.
- Docs (README, docstrings, MCP tool description) reflect proration and the residual cases.
