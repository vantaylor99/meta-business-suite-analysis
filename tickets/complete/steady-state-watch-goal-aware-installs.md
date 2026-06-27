description: The everyday "is this ad wasting money?" scan now grades install-goal ads on cost-per-install instead of purchase return, so a healthy mature install ad is no longer wrongly flagged as urgent for booking ~0 ROAS by design.
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
----

## What shipped

The steady-state (non-early-life) watch scan was ROAS-only: every mature delivering ad went through
`monitor.classify_ad`, which grades on ROAS. On an **install-goal** account a healthy mature install
ad books ~0 ROAS by design, so it was wrongly surfaced as `urgent`. This ticket extends the existing
early-life goal-awareness to the steady-state path.

- **`monitor._classify_ad_install`** — install-goal sibling of `classify_ad`. Reuses
  `early_triage.classify_own_sample` (so the cost-per-install bar is byte-identical to the analog
  engine + early-life sibling), maps `OWN_SAMPLE_*` onto the steady-state taxonomy
  (insufficient / watch-on-grace / ok / urgent), returns the same dict shape.
- **`monitor.build_watch_report`** — the steady-state per-ad body branches on `goal_kind(policy)` for
  both the grade and the row `Evidence` (`cost_per_app_install` vs `roas`). ROAS path preserved,
  relocated into `else`.

`classify_ad` is left ROAS-only and unchanged; the early-life forced-decision call already routes
install goals to `_forced_decision_install`, so the steady-state path was the only gap.

## How to validate

`source .venv/bin/activate && python -m pytest tests/test_meta_ads_analysis.py` — **381 passed**.
Repo ships no ruff/mypy in the venv (confirmed); pytest is the check. `ast.parse` + module import of
`cli.py` and `monitor.py` both clean.

## Review findings

**Checked** (adversarial pass over the implement diff `a7bcec9`, read with fresh eyes before the
handoff): grader verdict→taxonomy mapping and decision ordering (significance floor → grace → keep /
pause, mirrors `classify_ad`); reuse of `classify_own_sample` / `_goal_thresholds` (no duplicated
threshold); `build_watch_report` steady-state branch (goal-aware grade + evidence, row/watchlist
accounting unchanged, `flaggable` correct — install never emits `underperforming`); imports present;
all `classify_ad` callers (the only other steady-state-shaped caller, the early-life forced decision
at `monitor.py:451`, is already guarded by the `install` branch at `:430` — no missed caller);
`fetch_entity_metrics` cpi derivation; edge cases (zero installs, no target, grace); lint/tests.

**Found & fixed inline (all minor):**

- **CLI rendered a misleading `ROAS 0.00` header on install rows.** `cli.watch_main`'s `line(r)`
  hard-coded a `ROAS …` header, so a steady-state install `urgent` row — now graded on
  cost-per-install — printed `URGENT … ROAS 0.00 | …`, a display inconsistency *introduced by this
  ticket* (and a pre-existing wart for early-life install rows, which flow through the same `line`).
  The handoff flagged this and invited a fix. Extracted a pure, testable
  `cli._watch_row_metric_display(row)` that shows the row's actual goal metric (`cost/install $X` from
  the row evidence) for install rows and keeps the ROAS header **byte-identical** for everything else
  (including the `0.00` fallback and rows without evidence). Install rows now render
  `URGENT … cost/install $30.00 | …`; early-life install rows are corrected for free. Unit test
  `test_watch_row_metric_display_is_goal_aware` pins all four cases (install value / install n/a /
  ROAS value / ROAS-none fallback / no-evidence fallback).

- **Untested row-build path for ~0-install urgent ads.** A zero-install over-spend ad grades `urgent`
  but `cost_per_app_install` is `None`, so the row evidence must degrade to `cost/install n/a` without
  crashing on the `:.2f` f-string. The grader-level test covered `installs=0`, but the row-building
  path did not. Added integration test `test_watch_steady_install_zero_installs_flagged_urgent_no_crash`
  (asserts `urgent`, `metric_value is None`, `metric_display == "cost/install n/a"`,
  `dollars_at_risk == 300.0`, on watchlist).

- **Stale module docstring.** `monitor.py`'s "Account goal anchored: ROAS floor / target …" predated
  goal-awareness. Updated to state that install accounts are graded on cost-per-install (both
  early-life and steady-state paths branch on `primary_goal`).

**Found, NOT fixed (out of scope / pre-existing, documented):**

- The watch **summary header** (`cli.py` ~line 2011/2013) still prints `floor {roas_floor} target
  {roas_target}` and an `underperforming {N}` count for install accounts (always 0 / ROAS-irrelevant
  there). This is pre-existing — the header printed these regardless of goal before this ticket — and
  purely cosmetic (the per-ad lines and `$`-at-risk are correct). Left as-is; not introduced here. A
  future polish ticket could make the summary header goal-aware too, but it is not a defect.

**Verified non-issues:**

- **`cost_per_app_install` divergence** (handoff's open question): the row evidence uses
  `fetch_entity_metrics`' value (`control.py:1023`, `round(spend / app_installs, 2)`) while the
  grader's internal confidence uses `own.metric_value` (unrounded `spend / installs`). Both are
  `spend / installs` over the same window — they differ only by 2-dp rounding, and classification is
  driven by the **unrounded** value via `_is_struggling`, so rounding never flips a verdict. Not a bug.
- **Mock-only coverage** (no live/MCP path): consistent with the handoff and the project's
  deterministic-mock test norm; acceptable.

**Categories with nothing to report:** error handling (the no-target and zero-install degrades are
explicit and tested — no crash, no guessed threshold); resource cleanup (pure functions, no I/O in
the changed paths); type safety (dict shapes match `classify_ad`'s contract; no new untyped surface).

No **major** findings → no new fix/plan/backlog tickets filed.

## End
