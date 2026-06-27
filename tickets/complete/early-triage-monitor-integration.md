description: Wired the new early-life ad triage into the daily watch scan so a brand-new struggling ad is graded against past comparable ads — kept on a day-3 probation, surfaced as a pause candidate, or forced to a keep/kill by day 3 — without breaking the existing protection for recently-changed ads.
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/followups.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
----

## Summary

Integration layer on top of the `early-triage-core` engine. `monitor.build_watch_report` now grades
genuinely brand-new struggling ads with `early_triage.triage_ad` instead of silently abstaining them,
reconciles the verdict with the existing grace window, and drives a day-3 forced keep/kill via the
existing `followups` mechanism. The scan stays read-only and filesystem-free: it returns a
`followup_actions` list (file/close) that `cli.watch_main` applies. Feature is gated on a
`history_provider` being supplied (default-on in the CLI, default-off for the legacy test path), so
existing non-early-life behavior is byte-for-byte unchanged.

Implemented by commit `afe1ace`. See that commit's handoff for the full design narrative.

## Review findings

Adversarial pass over the `afe1ace` diff (monitor.py, followups.py, cli.py, tests). Read every changed
file in full plus the engine (`early_triage.py`), `classify_ad`, `fetch_entity_metrics`, and all
`mark_done` / watch-report consumers.

### Checked — correct, no action

- **Backward compatibility.** `early_life_enabled = early_life and history_provider is not None`.
  Every pre-existing `build_watch_report` test passes no provider → feature off → old code path
  unchanged. Verified the only watch-report consumer is `cli.watch_main` (grepped; no other reader of
  `watch_report.json` / `schema_version`), and it was updated for v2.
- **`mark_done` signature change** (now `Path | None`, new `missing_ok`) is additive — the other
  caller (`cli.py:2105`) uses the default `missing_ok=False` and still gets a `Path`. No breakage.
- **Age source.** Age is `history.age_on(as_of)` from the provider's `first_seen`, not `updated_time`;
  covered by `test_watch_age_from_first_seen_not_updated_time`. An ad edited yesterday but launched two
  weeks ago is correctly NOT triaged.
- **Grace override** on the day-3 forced path (`days_since_change=None`) is deliberate and tested
  (`test_watch_day3_probation_own_sample_clears_floor_keep_and_close` asserts `direct_observation`
  grounding, not the protective abstain).
- **Dedupe / idempotency.** Scan-level (`on_probation` → no file action) and file-level
  (`add_followup_if_absent` / `find_open_followup` by deterministic slug) dedupe both hold across runs;
  `mark_done(missing_ok=True)` closes idempotently. Tested.
- **Marker round-trip.** `early_life_slug` ↔ `early_life_ad_id` round-trips for numeric ad ids;
  non-early-life follow-ups yield `None`. Tested.
- **Determinism.** `due`/ages derive only from `as_of`/`first_seen`; the only wall-clock read is the
  pre-existing watchlist `generated_at`.
- **CLI renderer.** Early-life rows carry every key `line(r)` touches (`accelerating`,
  `times_flagged`, `roas`); the `urgent + early_pause + under + early_keep + watch` loop lists each row
  exactly once with no double-count (early vs non-early partitions are disjoint).
- **No account writes.** `build_watch_report` returns actions but touches no filesystem
  (`test_watch_build_report_performs_no_followup_writes`); `pause_candidate` is flag-only and emits no
  op.

### Found

- **MAJOR — install-goal forced decision uses ROAS, not the install metric.** Filed
  `tickets/fix/early-life-forced-decision-install-goal.md`. The day-3 forced "own sample clears the
  floor" shortcut in `_early_life_forced_decision` calls the ROAS-only `classify_ad` regardless of the
  account goal, so a healthy high-spend install-goal ad (zero purchases, cheap installs) can be
  force-flagged as a `pause_candidate` at the decision age. Only bites install-goal accounts whose ad
  cleared `min_spend` in the window; low-spend install ads fall through to the goal-aware analog path
  (already correct). Data to fix it (`m["cost_per_app_install"]`, the goal-aware engine) is already in
  hand.

- **MINOR (fixed in this pass) — install-goal integration coverage gap.** The handoff flagged that the
  integration tests only exercised ROAS scenarios. Added
  `test_watch_early_life_install_goal_keep_watch_is_goal_aware`: an install-goal brand-new struggling
  ad (zero installs) graded against comparable install ads that booked cheap installs → `keep_watch`,
  follow-up filed, and asserts the engine evidence cites `cost_per_app_install` (proves the policy
  threads through the brand-new branch goal-aware). This documents the *correct* brand-new path and
  is distinct from the MAJOR forced-decision bug above.

### Found — noted, not actioned (minor, acceptable as-is)

- **Probation follow-up can linger if the ad stops delivering before the decision age.** A paused/
  archived ad drops out of `window`/`DELIVERING`, so the forced-decision close never runs and its open
  follow-up stays open until a human handles it when it comes due. Harmless (the ad is already paused;
  the due follow-up surfaces for manual review) and closing it would require fetching non-delivering
  ads. Left as-is.
- **CLI open-followups filter is looser than the monitor's** (`EARLY_LIFE_MARKER in f.path.stem`
  substring vs `early_life_ad_id` structured parse). Consistent in practice for numeric ad ids; a
  follow-up bearing the marker substring but no trailing ad id is simply dropped from the monitor index
  (harmless). Left as-is.
- **`watch_main` itself is not executed by any test** (needs a live Meta client). Confirmed by reading
  that the CLI apply loop (`cli.py:1952-1966`) matches the test mirror `_apply_followup_actions`
  exactly. Acceptable; an end-to-end CLI test would need a fake `client_from_env` seam.

## Validation

`.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` → **330 passed** (was 329; +1 install-goal
integration test added this pass). pytest is the only configured gate (confirmed no ruff/mypy/black/
pyright in `pyproject.toml`). No pre-existing failures; no `.pre-existing-error.md` filed.
