---
description: Build a tool that grades each managed ad account against the cost-per-result (or ROAS) goal set for it and returns a plain on-goal / watch / pause-candidate verdict for the whole portfolio in one call.
prereq:
files: src/meta_ads_analysis/goal_grading.py (new), src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/account_registry.py, config/meta_ads_accounts.json, tests/test_meta_ads_analysis.py
difficulty: medium
---

## Summary

Add a read tool `grade_accounts_against_goals` that, over a scope of accounts, grades each
**configured** account's real efficiency against its own configured goal thresholds and returns a
per-account verdict (`on_goal` / `watch` / `pause_candidate` / `insufficient_data` /
`no_goal_thresholds` / `no_goal_configured`) plus a portfolio rollup and a pause-candidate shortlist.

The per-account metric already exists — `cross_account_performance` returns native
`cost_per_result` and `roas` per account. The per-account **goal** already exists — the account's
`action_policy` in `config/meta_ads_accounts.json` carries `target_cost_per_result`,
`pause_cost_per_result_above`, `target_roas`, `pause_roas_floor`, `roas_role`,
`evaluation_grace_days`, `min_spend_before_pause`, `primary_goal`. Nothing joins the two and says
where the account stands. This tool is that join.

## Architecture

Three layers, mirroring the existing "pure engine + orchestration + MCP wiring" split
(`early_triage.py` / `monitor.py` pure; `account_discovery.py` orchestration; `mcp_server.py`
wiring):

### 1. Pure grading engine — `src/meta_ads_analysis/goal_grading.py` (new)

A clock-free, reader-free, pure function. Takes an already-resolved metric value + spend + the
account's policy dict + `as_of` and returns a verdict dataclass. Fully unit-testable with no reader,
no FX, no registry.

Verdict constants (reuse the `"pause_candidate"` string value from
`early_triage.VERDICT_PAUSE_CANDIDATE` so the vocabulary is consistent):

```python
GOAL_ON = "on_goal"
GOAL_WATCH = "watch"
GOAL_PAUSE_CANDIDATE = "pause_candidate"   # == early_triage.VERDICT_PAUSE_CANDIDATE
GOAL_INSUFFICIENT = "insufficient_data"
GOAL_NO_THRESHOLDS = "no_goal_thresholds"  # configured but no CPR/ROAS bar for its metric
GOAL_NO_CONFIG = "no_goal_configured"      # not in config/meta_ads_accounts.json at all
```

Metric selection (the `roas_role` rule):
- `roas_role == "not_applicable"` → **always** grade on `cost_per_result` (never ROAS). Seattle.
- else if the goal is ROAS-based (`primary_goal == "roas"` OR a `target_roas`/`pause_roas_floor` is
  present) → grade on `roas`.
- else → grade on `cost_per_result`.

Thresholds by metric:
- cost_per_result: `target = target_cost_per_result`, `pause = pause_cost_per_result_above`.
  Lower is better.
- roas: `target = target_roas`, `pause = pause_roas_floor`. Higher is better.

Dataclass (returned by the engine):

```python
@dataclass(slots=True)
class GoalGrade:
    verdict: str          # one of the GOAL_* constants
    metric: str | None    # "cost_per_result" | "roas" | None
    value: float | None
    target: float | None
    pause_threshold: float | None
    in_grace: bool
    reasons: list[str]
```

Classification rules (single pure function, e.g. `grade_against_goal(*, metric, value, spend,
policy, as_of)`):

- **No applicable threshold** (chosen metric has neither a target nor a pause bar in policy) →
  `GOAL_NO_THRESHOLDS`. This is where install/subscription accounts like Pollen Sense land: their
  goal (maximize in-app subscriptions) has no `target_cost_per_result`/ROAS bar. Distinct from
  `no_goal_configured` (which means *no config entry*) and from `insufficient_data` (transient).
- **Insufficient data**: `value is None` (no results → no cost_per_result; no spend → no roas) OR
  `spend < min_spend_before_pause` (default reuse the account's `min_spend_before_pause`; when
  absent, treat as 0 → no floor) → `GOAL_INSUFFICIENT`. Guards the "cheap-but-zero-results" trap:
  an account with no results must NOT read `pause_candidate`.
- **cost_per_result** (lower is better), both bars present:
  - `value <= target` → `on_goal`
  - `target < value <= pause` → `watch`
  - `value > pause` → `pause_candidate`
- **roas** (higher is better), both bars present:
  - `value >= target` → `on_goal`
  - `pause <= value < target` → `watch`
  - `value < pause` → `pause_candidate`
- **Partial thresholds** (grade on what's present; report the gap in `reasons`, never crash):
  - cost, only `pause` set: `value > pause` → `pause_candidate`, else `watch` (can't confirm
    `on_goal` without a target).
  - cost, only `target` set: `value <= target` → `on_goal`, else `watch` (can't escalate to
    `pause_candidate` without a pause bar).
  - roas, only `pause` set: `value < pause` → `pause_candidate`, else `watch`.
  - roas, only `target` set: `value >= target` → `on_goal`, else `watch`.
- **Grace softening**: `in_grace` is true when the policy carries `evaluation_start_date`
  (ISO `YYYY-MM-DD`) AND `(as_of - evaluation_start_date).days < evaluation_grace_days`. When in
  grace, a `pause_candidate` verdict softens to `watch` and `in_grace: true` is set (mirrors
  `monitor.classify_ad`'s "protected from kill" rule: a protected entity never gets a kill verdict).
  `on_goal`/`watch` are unchanged. When `evaluation_start_date` is absent (the common case), the
  account is graded as mature (`in_grace: false`) — we do NOT fabricate a launch date.

### 2. Orchestration — `account_discovery.grade_accounts_against_goals(reader, ...)`

Reuse `cross_account_performance` wholesale — do NOT re-implement fan-out or metric resolution:

- Load the configured registry once (`account_registry.load_account_registry()`; empty/absent →
  `{}`, never fatal — mirror `_registry_by_ad_account_id`).
- **Scope**: default (`account_ids=None`) = the configured accounts' `ad_account_id`s. Explicit
  `account_ids` = those (reads stay open — an id not in config is graded `no_goal_configured`).
  Empty configured set + default scope → return an empty result with a `note`.
- Call `cross_account_performance(reader, date_from, date_to, account_ids=<scope>, level="account")`.
  Grade on the **native** `cost_per_result` / `roas` from each returned row (do NOT FX-convert
  thresholds — they are stated in the account's own currency; compare native-to-native, per
  [[currency-precision-low-priority]]).
- For each returned account row, look up its policy by normalized `ad_account_id`
  (`account.ad_account_id` is already `act_`-normalized at load). No config entry →
  `no_goal_configured`. Otherwise call the pure engine with the row's native metric/value/spend +
  policy + `as_of`.
- `as_of` param defaults to today (this is the ONE clock touch, exactly like
  `pacing_report`'s `as_of=None` → today default; the pure engine always takes an explicit
  `as_of`).
- Propagate `cross_account_performance`'s `errors` (per-account read failures, no-FX) into the
  output unchanged.

### 3. MCP wiring — `mcp_server.py`

Register in `DISCOVERY_TOOL_DESCRIPTIONS` + `build_discovery_tools` (a wrapper delegating to
`account_discovery.grade_accounts_against_goals`, exposing `date_from`, `date_to`, `account_ids`,
`as_of`). Description states plainly: grades each managed account against its own configured
cost-per-lead / ROAS goal; reads are open but only configured accounts carry goals.

## Output shape

Per account:
```
{account_id, ad_account_id, name, currency,
 metric: "cost_per_result"|"roas"|null, value, target, pause_threshold,
 spend, in_grace: bool, verdict, reasons: [str]}
```
Rollup + top level:
```
{date_from, date_to, as_of,
 accounts: [...],
 counts: {on_goal, watch, pause_candidate, insufficient_data, no_goal_thresholds, no_goal_configured},
 pause_candidates: [{account_id, name, metric, value, pause_threshold}, ...],
 errors: [...],
 note?: str}
```

## Config addition

`evaluation_start_date` (optional ISO `YYYY-MM-DD`) inside an account's `action_policy` — the
date of the account's launch or last significant change, governing the `evaluation_grace_days`
window. No existing account sets it; absent → mature grading. **Populating it for Seattle (its
~2026-07-01 relaunch, per the vault decision log) is an operator/data task, out of scope here** —
so the default portfolio call grades Seattle `pause_candidate`, matching the ticket's "modulo
grace" note. Document the field in `config/meta_ads_accounts.json` via a real value on one account
only if trivially safe; otherwise leave config unchanged and cover grace with an injected-policy
test.

## Design decisions (resolved)

- **Grace signal**: keyed off an optional `evaluation_start_date` in `action_policy` + the tool's
  `as_of`, NOT off account age. An account age heuristic (first-spend-day from a daily read) is the
  wrong signal — a years-old account like Seattle with a recent budget/audience change would read
  "mature" by age yet needs grace after the change. Activity-log parsing to auto-detect the last
  change is out of scope (heavy, fragile, breaks the one-call contract). Absent field → mature.
- **Install/subscription accounts** (Pollen Sense) → `no_goal_thresholds`, not a crash and not a
  misgrade. Extending grading to cost-per-install (via `secondary_cost_per_app_install_target`,
  which `cross_account_performance` does not currently expose per account) is a backlog follow-up,
  not this ticket.
- **Native currency**: grade native `cost_per_result` against the native threshold; no FX on
  thresholds.

## Edge cases & interactions

- No results → `cost_per_result` absent from the row → `value is None` → `insufficient_data`, NOT
  `pause_candidate`. **Test required.**
- `spend < min_spend_before_pause` with a real (but thin) `cost_per_result` → `insufficient_data`
  (too early to pause). Distinct from the no-results case; both → `insufficient_data`.
- Direction correctness: a cheap account (`cost_per_result` well under target) → `on_goal`; an
  over-`pause` account → `pause_candidate`; a high-ROAS account → `on_goal`; a below-floor ROAS
  account → `pause_candidate`. **Tests required for both directions.**
- Seattle real datum: `act_103014553` reads ~$58–66 cost-per-lead vs `$10` target / `$40` pause →
  `pause_candidate` (no `evaluation_start_date` set → not in grace). **Test asserts this.**
- Grace: with `evaluation_start_date` set inside the window, an over-pause account softens
  `pause_candidate` → `watch`, `in_grace: true`. **Test injects the policy and asserts softening.**
- `roas_role == "not_applicable"` account that happens to also carry a ROAS number in the row → is
  STILL graded on `cost_per_result`, never ROAS. **Test asserts the row's roas is ignored.**
- Partial thresholds (target set, pause null and vice versa) for both metrics → graded on what's
  present, gap noted in `reasons`, never crashes. **Test each partial case.**
- Account in explicit `account_ids` but not in config → `no_goal_configured` (counted, not an
  error). **Test required.**
- Configured account with a goal but no CPR/ROAS bar (install/subscription) → `no_goal_thresholds`.
  **Test required.**
- Empty registry + default scope → empty `accounts`, zeroed `counts`, a `note`; must not raise.
- Per-account read failure / no-FX errors from `cross_account_performance` propagate into `errors`
  and do NOT abort the whole grade.
- Determinism: same inputs → same output (inherits `cross_account_performance`'s ordering; sort
  `pause_candidates` for a stable shortlist).

## Key tests (add to tests/test_meta_ads_analysis.py)

- Pure engine unit tests (no reader): each verdict + each partial-threshold branch + grace softening
  + roas_role=not_applicable forces cost metric + insufficient_data (None value and sub-min-spend).
- `grade_accounts_against_goals` with a `FakeMetaReader` (get_account + fetch_insights) over a
  temp config (monkeypatch `account_registry.DEFAULT_ACCOUNTS_CONFIG_PATH`, as existing cross-account
  tests do ~line 818/945): Seattle-like account over threshold → `pause_candidate`; a cheap account →
  `on_goal`; a ROAS account below floor → `pause_candidate`; an unconfigured explicit id →
  `no_goal_configured`; verify `counts` and the `pause_candidates` shortlist.
- MCP registration parity: `"grade_accounts_against_goals"` in both `DISCOVERY_TOOL_DESCRIPTIONS`
  and `build_discovery_tools(reader)` (extend the existing discovery-parity test around line 9676).

## Validation

- `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/grade-goals-tests.log`
  (stream output; never silent-redirect).
- Type check per AGENTS.md (mypy/ruff if configured) — stream output.

## TODO

Phase 1 — pure engine
- Create `src/meta_ads_analysis/goal_grading.py`: `GOAL_*` constants, `GoalGrade` dataclass, pure
  `grade_against_goal(...)` (metric selection, thresholds, insufficient_data guard, partial-threshold
  handling, grace softening). Clock-free: takes `as_of`.
- Unit tests for the pure engine (all verdicts, partials, grace, roas_role, insufficient_data).

Phase 2 — orchestration
- Add `account_discovery.grade_accounts_against_goals(reader, *, date_from, date_to,
  account_ids=None, as_of=None)`: resolve configured scope, call `cross_account_performance`, join
  by `ad_account_id`, grade each, build `counts` + `pause_candidates` + propagate `errors`.
- Tests with `FakeMetaReader` over a temp config (Seattle over-threshold, cheap on_goal, ROAS below
  floor, unconfigured → no_goal_configured, empty-registry note).

Phase 3 — MCP wiring
- Add `grade_accounts_against_goals` to `DISCOVERY_TOOL_DESCRIPTIONS` and `build_discovery_tools`
  in `mcp_server.py`.
- Extend the discovery-parity test to include the new tool.

Phase 4 — validate
- Run the test suite + type checks, streaming output. Write `tickets/.pre-existing-error.md` only if
  a failure is clearly unrelated to this diff.

Downstream: `portfolio-digest` (backlog) consumes this tool's per-account verdicts and rollup.
