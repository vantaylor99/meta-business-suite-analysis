description: Wired the new early-life ad triage into the daily watch scan so a brand-new struggling ad is graded against past comparable ads — kept on a day-3 probation, surfaced as a pause candidate, or forced to a keep/kill by day 3 — without breaking the existing protection for recently-changed ads.
prereq: early-triage-core
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/followups.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
----

## Summary

Integration layer on top of the `early-triage-core` engine. `monitor.build_watch_report` now grades
genuinely brand-new struggling ads with `early_triage.triage_ad` instead of silently abstaining them,
reconciles the verdict with the existing grace window, and drives a day-3 forced keep/kill via the
existing `followups` mechanism. The scan stays read-only and **filesystem-free**: it returns a
`followup_actions` list (file/close) that `cli.watch_main` applies.

Build + tests: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **329 passed** (was 318;
+11 integration tests). pytest is the only configured gate (no ruff/mypy/pyright/black in this repo;
confirmed in pyproject + `.venv`). All changed modules byte-compile clean; no pre-existing failures, so
no `.pre-existing-error.md` filed.

## What changed (where the reviewer should look)

**`monitor.py`** — the core of the work:
- `build_watch_report` gained `early_life` (default on), `history_provider`, `open_followups`,
  `policy`, and the `early_life_*` knobs. Histories are fetched once per scan; an early-life branch runs
  per delivering ad **before** the normal `classify_ad` path.
- `_early_life_branch` is the reconciliation logic. Age comes from the provider's `first_seen` (launch),
  NOT `updated_time` (edit). Verdict mapping: `not_struggling` → fall through to today's behavior;
  `keep_watch`/`abstain_keep` → `watch`-class row + a `file` follow-up action; `pause_candidate` →
  flag-only `pause_candidate` row (no write, no follow-up).
- `_early_life_forced_decision` is the day-3 path: a probated ad at `age ≥ decision_age` gets a real
  decision with the grace abstain **deliberately overridden** (`days_since_change=None`). Own sample
  clears the significance floor → real `classify_ad` (direct-observation confidence); still below floor
  → analog verdict governs. Either way a `close` action is returned so probation never loops.
- Report `schema_version` bumped 1 → 2 (adds early-life row fields + `followup_actions`; sort order adds
  `pause_candidate`). Existing non-early-life rows are unchanged.

**`followups.py`** — `EARLY_LIFE_MARKER`, deterministic per-ad `early_life_slug(ad_id)` +
`early_life_ad_id(followup)` round-trip, `find_open_followup(slug=)`, `add_followup_if_absent(...)` (the
cross-run dedupe), and `mark_done(..., missing_ok=True)` (idempotent close).

**`cli.py`** — `watch_main` constructs `DuckDBHistoryProvider`, loads open early-life follow-ups, passes
the knobs (with `--no-early-life` + `--early-life-*` flags + `--db-path`), applies the returned
`followup_actions`, and prints the early-life buckets (kept-on-probation N · early-pause-candidate N)
with analog-basis per row.

## Validation / use cases (test floor — treat as a starting point)

New tests are in `tests/test_meta_ads_analysis.py` (search `test_watch_early_life`, `test_watch_day3`,
`test_watch_running_scan_twice`, `test_followups_add_if_absent`). They use a fake reader
(`_WatchFakeClient`), a fake `_FakeHistoryProvider`, and a tmp followups root — **mocks only, no live
Meta / no DuckDB**. Covered:

- early-life struggling ad, recovering analogs → row `verdict == keep_watch`, follow-up filed at
  `first_seen + decision_age` (= 2026-06-27), no write.
- early-life struggling ad, non-recovering analogs → `pause_candidate` row carrying correlational
  confidence (≤ medium) + evidence; nothing filed, no write.
- ad edited yesterday but launched 2 weeks ago → NOT triaged (age from `first_seen`); normal grace path.
- provider has no history for a delivering ad → falls back to `classify_ad` (urgent), no crash.
- age-3 probation, own sample clears floor → real keep (direct-observation, grace overridden), close;
  and the kill variant (below pause floor → `pause_candidate`).
- age-3 probation, still below floor → analog verdict governs keep vs pause; follow-up closed.
- run the scan twice → exactly one follow-up file (scan-level + file-level dedupe).
- pure-scan assertion: `build_watch_report` returns `followup_actions` but writes nothing to the
  followups tree.
- followups helpers: `add_followup_if_absent` dedupe + marker round-trip; `mark_done(missing_ok=True)`
  idempotency (and still raises without it).

Manual smoke (live, out-of-band — not agent-runnable): `watch_account --account <slug> --as-of <date>`
on an account with a recently-launched struggling ad should print the early-life buckets and create one
`followups/<slug>/<due>-early-life-triage-<ad_id>.md`; re-running must not create a second.

## Handoff honesty — known gaps / things to scrutinize

- **`pause_candidate` is flag-only by design.** This ticket surfaces the row (with ad_id, evidence,
  confidence) for the operator to route through the existing `propose-pause-ads` guarded flow; it does
  **not** auto-build a ready-to-approve pause op. A future ticket wanting the scan to emit a grounded
  pause op directly is deliberately out of scope (file as follow-on). Reviewer: confirm the row truly
  carries enough for `propose-pause-ads` and that no account write path was introduced.
- **`watch_main` is not executed by any test.** It needs a live Meta client (`client_from_env` +
  `resolve_ad_account_id`), so the tests mirror its apply loop via `_apply_followup_actions` rather than
  running the CLI. The CLI wiring (arg parsing, provider construction, the apply loop, the printed
  summary/renderer) is verified by reading only — a real gap. Worth a careful read of `cli.py:1872`+.
- **CLI open-followups filter is looser than the monitor's.** `watch_main` selects open follow-ups with
  `EARLY_LIFE_MARKER in f.path.stem` (substring), while `build_watch_report` indexes them via
  `early_life_ad_id` (structured parse). Consistent in practice but not the same predicate — confirm no
  edge case (e.g. an unusual ad_id) slips through.
- **Early-life ads are not added to the persistent watchlist** (`times_flagged`); the probation
  follow-up tracks them instead. Intentional, but a behavior change worth confirming is acceptable.
- **Install-goal accounts** flow through the engine's goal handling and degrade to `abstain_keep` when
  no install-cost target is in policy (depends on `confidence-install-goal-significance-ops` /
  `review-gate-install-goal-direction`, both already complete). Not exercised in the *integration*
  tests here (only ROAS scenarios) — engine-level install tests live in `early-triage-core`. An
  install-goal `build_watch_report` test would close this gap.
- **`accelerating` is hardcoded `False` and `days_since_change` `None` on early-life rows** (the
  renderer shows the analog basis instead). Cosmetic; not used for any early-life decision.
- **Determinism:** `due` dates and ages derive only from `as_of`/`first_seen`; no `date.today()` inside
  `build_watch_report`. The only wall-clock read is the watchlist `generated_at` timestamp (pre-existing,
  unrelated to early-life).
