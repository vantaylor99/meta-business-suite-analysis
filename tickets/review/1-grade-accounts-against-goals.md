---
description: Review a new read tool that grades each managed ad account against its own cost-per-lead / ROAS goal and returns a one-call on-goal / watch / pause-candidate verdict for the whole portfolio.
prereq:
files: src/meta_ads_analysis/goal_grading.py (new), src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
---

## What shipped

A new discovery read tool `grade_accounts_against_goals` that joins two things that already existed
but were never connected: each account's **real efficiency** (`cross_account_performance`'s native
`cost_per_result` / `roas`) and each account's **configured goal** (its `action_policy` in
`config/meta_ads_accounts.json`). One call returns a per-account verdict, a portfolio `counts`
rollup, and a `pause_candidates` shortlist.

Built in the established three-layer split:

- **Pure engine** — `src/meta_ads_analysis/goal_grading.py` (NEW). Clock-free, reader-free,
  config-free. `select_goal_metric(policy)` + `grade_against_goal(*, metric, value, spend, policy,
  as_of)` returning a `GoalGrade` dataclass. Verdict constants `GOAL_ON` / `GOAL_WATCH` /
  `GOAL_PAUSE_CANDIDATE` / `GOAL_INSUFFICIENT` / `GOAL_NO_THRESHOLDS` / `GOAL_NO_CONFIG`.
- **Orchestration** — `account_discovery.grade_accounts_against_goals(reader, *, date_from,
  date_to, account_ids=None, as_of=None)`. A pure post-processor over `cross_account_performance`
  (inherits fan-out, per-account error isolation, determinism). `as_of=None` → today (UTC) is the
  single clock touch.
- **MCP wiring** — added to `DISCOVERY_TOOL_DESCRIPTIONS` + `build_discovery_tools` in
  `mcp_server.py` (now 8 discovery tools).

## Verdict logic (what to check)

- **Metric selection** (`select_goal_metric`): `roas_role == "not_applicable"` → ALWAYS
  `cost_per_result` (even if a ROAS bar is present); else ROAS-based goal (`primary_goal == "roas"`
  OR a `target_roas`/`pause_roas_floor` present) → `roas`; else → `cost_per_result`.
- **Order of guards** in `grade_against_goal`: (1) no bar for the chosen metric →
  `no_goal_thresholds`; (2) `value is None` OR `spend < min_spend_before_pause` (absent → 0 → no
  floor) → `insufficient_data`; (3) classify against present bars; (4) grace softens a
  `pause_candidate` → `watch`.
- **cost_per_result** (lower better): `<= target` on_goal, `<= pause` watch, `> pause`
  pause_candidate. **roas** (higher better): `>= target` on_goal, `>= pause` watch, `< pause`
  pause_candidate. Partial bars grade on what's present (documented in `reasons`, never crash).
- **Grace**: keyed off an optional `evaluation_start_date` (ISO) inside `action_policy` +
  `evaluation_grace_days`; `in_grace` iff `(as_of - start).days < grace_days`. No launch date → mature.
- **Native currency**: thresholds compared native-to-native, NO FX (goals are stated in the
  account's own currency — [[currency-precision-low-priority]]).

## Output shape

Per account: `{account_id, ad_account_id, name, currency, metric, value, target, pause_threshold,
spend, in_grace, verdict, reasons}`. Top level: `{date_from, date_to, as_of, accounts, counts,
pause_candidates, errors, note?}`. `counts` always carries all six verdict buckets (zeroed when
none). `pause_candidates` sorted by `account_id` for a stable shortlist.

## Tests (floor, not ceiling — 648 pass total, 15 new)

Pure engine (no reader): metric-selection incl. `not_applicable` forces cost; both directions for
cost + roas incl. boundaries; all four partial-threshold branches; `no_goal_thresholds`;
`insufficient_data` (None value AND sub-min-spend); absent-min-spend = no floor; grace softening +
mature + grace-without-launch-date; vocabulary sync test (`GOAL_PAUSE_CANDIDATE ==
early_triage.VERDICT_PAUSE_CANDIDATE`).

Orchestration (`FakeMetaReader`, monkeypatched `_registry_by_ad_account_id`): 4-account portfolio
(Seattle-like over-threshold → pause_candidate; cheap → on_goal; ROAS below floor → pause_candidate;
install → no_goal_thresholds) asserting verdicts + `counts` + sorted shortlist; unconfigured explicit
id → `no_goal_configured`; empty registry + default scope → note (Meta never touched); zero-results →
`insufficient_data` not pause; `not_applicable` ignores a huge row-roas; per-account read error
isolated into `errors`; `as_of` defaults to today; MCP wrapper smoke; **end-to-end through the REAL
registry JSON** (roas_role in `measurement_focus`, thresholds in `action_policy`).

## Validation run

- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **648 passed**.
- `py_compile` clean on all three changed sources.
- No `mypy`/`ruff` configured in this repo (`pyproject.toml` dev extra is `pytest` only) — pytest is
  the project's only automated check. `.pre-existing-error.md` NOT written (no failures observed).
- Note on tooling: I used `.venv/bin/python` (there is no bare `python` on PATH; `python3` is
  homebrew's). Reviewer should use the same venv.

## Known gaps / things to probe (treat my work as a starting point)

1. **`evaluation_start_date` is NOT populated in config for any account** (including Seattle). This
   is deliberate per the plan ("an operator/data task, out of scope here"): the default portfolio
   call grades Seattle `pause_candidate` (unsoftened), matching the plan's "modulo grace" note.
   Consequence: **grace softening is covered only by injected-policy tests, never by a live-config
   end-to-end path.** If a reviewer wants grace exercised through real JSON, add
   `evaluation_start_date` to a temp-config test (the field parses through `action_policy` today —
   the engine reads it from the merged policy dict).
2. **Grace reads `evaluation_start_date` from `action_policy`**, but `roas_role` lives in
   `measurement_focus`. The orchestration's `_grade_policy_for_account` folds `account.roas_role`
   into the policy dict; it does NOT fold anything from `measurement_focus` for the grace date. That
   is correct today (the plan puts `evaluation_start_date` in `action_policy`), but worth confirming
   the plan's intent if config authors put it elsewhere.
3. **Registry is loaded twice per call** — once in `grade_accounts_against_goals`
   (`_registry_by_ad_account_id`) and again inside `cross_account_performance`. Cheap (a JSON read),
   accepted, not optimized. Flagging in case a reviewer sees it as a smell.
4. **Install/subscription accounts → `no_goal_thresholds`, full stop.** Grading them on
   cost-per-install (via `secondary_cost_per_app_install_target`, which `cross_account_performance`
   does not currently expose per account) is a deliberate **backlog follow-up**, not done here.
5. **`level` is fixed to `"account"`** (inherited from `cross_account_performance`); no
   campaign/adset grading. Matches the prereq's contract.
6. **`pause_candidates` sort is by `account_id` (string)**, purely for determinism — NOT
   worst-first. If an operator would prefer the shortlist ranked by how far over/under the pause bar
   each account is, that's a small follow-up (mixing cost-higher-worse and roas-lower-worse into one
   severity key needs a decision).
7. **Downstream**: `portfolio-digest` (backlog) is intended to consume this tool's per-account
   verdicts + rollup.

## Suggested reviewer spot-checks

- Confirm a `not_applicable` account with a real ROAS in the row is graded on cost (test
  `test_grade_accounts_not_applicable_ignores_row_roas`) — the single most load-bearing correctness
  claim.
- Confirm zero-results does NOT read `pause_candidate` (the cheap-but-zero trap) — engine + orch
  both covered, but re-read the guard order in `grade_against_goal`.
- Confirm boundary semantics match intent: `value == target` → on_goal; `value == pause` (cost) →
  watch; `value == pause_roas_floor` → watch. These are asserted but are judgment calls worth a look.
