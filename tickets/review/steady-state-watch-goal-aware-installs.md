description: The everyday "is this ad wasting money?" scan now grades install-goal ads on cost-per-install instead of purchase return, so a healthy mature install ad is no longer wrongly flagged as urgent for booking ~0 ROAS by design.
prereq:
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py, src/meta_ads_analysis/cli.py
difficulty: hard
----

## What shipped

The steady-state (non-early-life) watch scan was ROAS-only: every mature delivering ad went through
`monitor.classify_ad`, which grades on ROAS. On an **install-goal** account (`primary_goal:
"maximize_in_app_subscriptions"`) a healthy mature install ad books ~0 ROAS by design, so it was
wrongly surfaced as `urgent`. The early-life path was already goal-aware
(`_forced_decision_install`); this ticket extends that goal-awareness to the steady-state path.

### Changes (`src/meta_ads_analysis/monitor.py`)

1. **New `_classify_ad_install(...)`** — a parallel install grader, sibling to `classify_ad`. It does
   NOT re-implement the install bar: it calls `early_triage.classify_own_sample` (which threads
   through `_goal_thresholds`), so the cost-per-install bar is byte-identical to the analog engine and
   the early-life install sibling. Returns the SAME dict shape as `classify_ad`
   (`classification`/`dollars_at_risk`/`reasons`/`confidence`). Mapping of the `OWN_SAMPLE_*` verdict
   onto the steady-state taxonomy:
   - `OWN_SAMPLE_INSUFFICIENT` (spend < `min_spend` **or** no install target in policy) →
     `insufficient` (skipped; abstains) — graceful degradation, no crash, no guessed threshold.
   - grace window (`days_since_change` < `grace_days`) → `watch` (learning, protected from kill),
     goal-agnostic, checked AFTER the significance floor (mirrors `classify_ad`'s ordering).
   - `OWN_SAMPLE_KEEP` (cost ≤ target) → `ok` (skipped) — **the core fix**.
   - `OWN_SAMPLE_PAUSE` (cost > target, or ~0 installs on the spend) → `urgent`; `dollars_at_risk` =
     full window spend (no ROAS waste estimate exists), mirroring `_forced_decision_install`.

2. **`build_watch_report` steady-state per-ad body** — branches on `goal_kind(policy)` for BOTH the
   grade (`_classify_ad_install` vs `classify_ad`) and the row `Evidence` (`cost_per_app_install` vs
   `roas`). The ROAS branch (the `classify_ad` call + ROAS `Evidence`) is preserved in content,
   relocated into `else` branches. The shared watchlist-accounting + ordering block is unchanged. Row
   dict shape is identical (top-level `roas`/`purchases` retained; install rows leave them ~0/None and
   the real metric lives in `evidence`, exactly like early-life install rows).

### Design decisions (carried from the plan, re-confirmed during implement)

- **`urgent`, not `underperforming`** — install exposes ONE effective bar through `_goal_thresholds`
  (`secondary_cost_per_app_install_target` → `pause_if_no_primary_and_secondary_cost_above`, returned
  as `(target, target)`). That bar IS the account's pause threshold, so there is no `underperforming`
  middle tier; over-bar maps to `urgent`. Documented in the grader docstring.
- **`classify_ad` left ROAS-only and unchanged** — no goal-parameterization of the pure function; the
  install grader is a parallel sibling. This keeps `classify_ad`'s tests and the early-life forced-
  decision call (`monitor.py:_early_life_forced_decision`) untouched.

## How to validate

Run: `python -m pytest tests/test_meta_ads_analysis.py` (venv: `source .venv/bin/activate` first).
Result at handoff: **379 passed**. The repo ships no ruff/mypy in the venv — pytest is the check.
`python -c "import ast; ..."` syntax check + module import both clean.

### New tests (all in `tests/test_meta_ads_analysis.py`)

Steady-state integration via `_run_steady_install` (wraps `_run_watch` with `early_life=False`,
`policy=_INSTALL_POLICY`; no DuckDB/history needed):
- `test_watch_steady_install_cheap_cpi_not_flagged` — **the core regression fix**: $300 / 200 installs
  ($1.50 < $3.00 target), ~0 ROAS → absent from `rows` and `watchlist`. (Was `urgent` before.)
- `test_watch_steady_install_expensive_cpi_flagged_urgent_on_install_metric` — $300 / 10 installs
  ($30 > $3.00) → `urgent`, `evidence.metric_name == "cost_per_app_install"`, `metric_value == 30.0`,
  `dollars_at_risk == 300.0`, on the watchlist with `times_flagged == 1`.
- `test_watch_steady_install_no_target_degrades_gracefully` — install goal, policy omits both target
  keys → skipped (no crash, no flag).
- `test_watch_steady_install_grace_protects_over_target_ad` — recently-changed (`days_since_change`
  2 < grace 5) over-target install ad → `watch`, never `urgent`, not on watchlist.
- `test_classify_ad_install_buckets_and_protection` — pure-unit harness `_cls_install` mirroring the
  `_cls` helper: cheap→`ok`, expensive→`urgent`, young→`watch`, no-target→`insufficient`, zero
  installs on spend→`urgent`, urgent `dollars_at_risk == 300.0`.

ROAS regression: extended `test_build_watch_report_rows_carry_confidence_and_reproducible_evidence`
with `assert ev["metric_name"] == "roas"` (the install branch must not bleed through).

## Known gaps / things for the reviewer to confirm (treat tests as a floor)

- **CLI prints `ROAS 0.00` on install steady-state rows.** Confirmed by reading `cli.py:2019-2036`:
  `line(r)` renders `ROAS {roas}` where `roas` falls back to `"0.00"` when `r["roas"]` is None, then
  prints each `reason`. So an install `urgent` row renders as `URGENT … ROAS 0.00 | $300 | $300 at
  risk | age 25d` with `- cost/install $30.00 over the $3.00 target on $300` underneath. This is
  **parity with how early-life install rows already render** (same `line(r)`), and the cost metric is
  in `reasons` + `evidence`. **No CLI test was added** — the change relies on the existing non-early
  `urgent` bucket (`cli.py:2005`) and `line(r)` with zero CLI code change. A reviewer may want a CLI-
  level assertion, or to teach `line(r)` to show the goal metric in the header. Accepted as-is per the
  plan (the header line is cosmetic; bucketing/$-at-risk are correct).
- **`recent`-window accelerating flag.** In the integration tests the fake reader returns the SAME
  insights for both the window and recent queries, so `accelerating` computes True; it only appends a
  reason on `urgent` rows and never changes the classification. Real data differs — not asserted.
- **No live/MCP path exercised.** All tests are mock-only (`_WatchFakeClient`, fake policy). The
  `goal_kind(policy)` branch depends on the account's resolved `action_policy` carrying
  `primary_goal: "maximize_in_app_subscriptions"` and an install target; verified against
  `config/meta_ads_accounts.json` in the plan but not re-pulled live here.
- **`m.get("cost_per_app_install")` vs `own.metric_value`.** The row `Evidence` uses the
  `fetch_entity_metrics`-derived `cost_per_app_install` (`spend / app_installs`), while the grader's
  internal confidence `Evidence` uses `own.metric_value` (also `spend / results`). They agree for a
  single-window aggregate; a reviewer should confirm they can't diverge (e.g. rounding) in a way that
  matters — they shouldn't, since both are `spend / installs` over the same window.
