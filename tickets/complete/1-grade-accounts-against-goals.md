---
description: A new read tool grades each managed ad account against its own cost-per-lead / ROAS goal and returns a one-call on-goal / watch / pause-candidate verdict for the whole portfolio. Reviewed, tests + docs corrected, shipped.
prereq:
files: src/meta_ads_analysis/goal_grading.py, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: medium
---

## What shipped

A new discovery read tool `grade_accounts_against_goals` (the **eighth** discovery tool) that joins
each account's real efficiency (`cross_account_performance`'s native `cost_per_result` / `roas`) to
its configured goal (`action_policy` in `config/meta_ads_accounts.json`) and returns a per-account
verdict, a portfolio `counts` rollup, and a `pause_candidates` shortlist — in one call.

Built in the established three-layer split (pure engine `goal_grading.py` → orchestration
`account_discovery.grade_accounts_against_goals` → MCP wiring in `mcp_server.py`). See the implement
commit `abb1ff9` for the full design; this ticket is the review pass over it.

## Review findings

Adversarial pass over the implement diff (`abb1ff9`), read before the handoff summary. Scrutinized
for SPP, DRY, correctness, boundary semantics, error handling, type safety, resource cleanup, doc
currency, and test completeness (happy path, edges, error paths, regressions, interactions).

### Correctness — checked, nothing found

- **Verdict guard order** in `grade_against_goal`: (1) no bar → `no_goal_thresholds`, (2) `value is
  None` or sub-`min_spend_before_pause` → `insufficient_data`, (3) classify, (4) grace softening.
  Order is correct — the zero-results / cheap-but-zero trap is genuinely guarded (a zero-result
  account reaches `insufficient_data`, never `pause_candidate`). Verified engine + orchestration.
- **Boundary semantics**: `value == target` → `on_goal`; `value == pause` (cost) → `watch`; `value
  == pause_roas_floor` → `watch`. Consistent between the both-bars and single-partial-bar branches.
- **Partial-threshold branches** (only-target / only-pause, both metrics): only-target can confirm
  `on_goal` but never escalate to `pause_candidate`; only-pause can escalate but never confirm
  `on_goal`. Grace softening can only fire on a real `pause_candidate`, which requires a pause bar —
  so it correctly never fires in an only-target branch. Sound.
- **`not_applicable` override**: `select_goal_metric` forces `cost_per_result` for a lead-gen
  account even when a stray ROAS bar/row value is present — the single most load-bearing claim,
  test-covered at engine + orchestration level (`..._ignores_row_roas`).
- **`_num` rejects `bool`** (an `int` subclass) so `True`/`False` never grade as `1.0`/`0.0`. Malformed
  `evaluation_start_date` / `as_of` handled (former → mature, latter → clean whole-call `ValueError`).
- **The registry join** (`_normalize_ad_account_id` adds the `act_` prefix, so config
  `"103014553"` matches the row's `act_103014553`) is exercised end-to-end through the real
  registry-JSON parse path.

### Design boundaries — reviewed, all deliberate, no tickets filed

The implementer's "known gaps" were each confirmed to be documented, intentional scope decisions —
not defects: registry loaded twice per call (cheap JSON read), install/subscription
cost-per-install grading (a plan-level backlog follow-up), `level` fixed to `"account"` (matches the
`cross_account_performance` contract), and `pause_candidates` sorted by `account_id` for determinism
rather than worst-first (a documented product decision, not a bug). No new fix/backlog tickets warranted.

### Minor findings — FIXED INLINE

1. **Stale docs.** `README.md` and `docs/META_API_SETUP.md` enumerated "**seven** discovery tools"
   and described `rank_accounts` as the last one — the new eighth tool was undocumented. Updated both:
   the `.mcp.json` tool inventory now reads "eight discovery tools" and lists
   `grade_accounts_against_goals`, and each doc gained a parallel prose section describing the tool's
   metric selection, native-currency (no-FX) grading, open-reads scope, `insufficient_data` guard, and
   grace window. `config.py`'s "sixth discovery tool" comment refers to `pacing_report` specifically
   and remains correct.
2. **Untested load-bearing claim: "thresholds compared native-to-native, no FX."** Every original
   orchestration test used USD accounts, so a regression that reached for the `*_normalized` twin
   instead of the native metric would have gone uncaught. Added two tests:
   - `test_grade_accounts_thresholds_are_native_not_fx_normalized` — a EUR account whose native
     `cost_per_result` is 38 EUR sits under its 40 EUR pause bar (→ `watch`); if graded on the
     USD-normalized twin (38 × 1.08 = 41.04) it would cross the bar and misread as `pause_candidate`.
     Locks native grading.
   - `test_grade_accounts_grades_currency_missing_from_fx_table` — a JPY account (JPY absent from
     `config/fx_rates.json`) is still graded on its native metric, and the no-FX entry propagates into
     `errors` non-fatally. Proves grading is FX-independent (the other half of the claim).

## Validation

- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **650 passed** (was 648; +2 new
  review tests). `py_compile` clean on all three changed sources.
- No `mypy`/`ruff` configured (`pyproject.toml` dev extra is `pytest` only) — pytest is the project's
  only automated check. `.pre-existing-error.md` NOT written (no failures observed, pre-existing or otherwise).
- Doc-only edits to `README.md` / `docs/META_API_SETUP.md` cannot affect tests; suite re-run green after them.

## Downstream (unchanged from implement handoff)

`portfolio-digest` (backlog) is intended to consume this tool's per-account verdicts + `counts`
rollup. Install/subscription cost-per-install grading remains a separate backlog follow-up.

## End
