description: Wire the new early-life ad triage into the daily watch scan so brand-new struggling ads are graded against past comparable ads, kept-with-a-day-3-recheck or surfaced as a pause candidate, and forced to a clear keep-or-kill call by day 3 — without contradicting the existing protection for recently-changed ads.
prereq: early-triage-core
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/followups.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What this ticket builds

The **integration layer** on top of `early-triage-core` (its prereq). The core ticket ships the pure
engine (`early_triage.triage_ad`, `HistoryProvider`/`DuckDBHistoryProvider`, `analog_confidence`).
This ticket makes it do something operationally:

1. Run the triage inside the **watch scan** (`monitor.build_watch_report`) for brand-new ads, instead
   of silently abstaining them.
2. **Reconcile** the triage with `monitor.py`'s existing grace-window protection so an ad is never
   simultaneously "protected — keep running" and "early-pause flagged".
3. **File / close / dedupe** the **day-3 follow-up** that forces the re-check, using the existing
   `followups` mechanism.
4. **Force a confident keep/kill at day 3** for ads put on early-life probation.
5. Surface pause candidates through the existing **guarded propose flow** (never a silent kill).

## Background

See `early-triage-core` for the full intent and resolved design. In short: today
`monitor.classify_ad` returns `insufficient` (dropped) for sub-floor brand-new ads and `watch`
(protected, abstain) for ads inside `grace_days`. Both are *silent* — a dead new ad and a slow-start
winner are indistinguishable. The engine grades a struggling young ad against comparable past ads at
the same age and returns one of `not_struggling | abstain_keep | keep_watch | pause_candidate`. This
ticket consumes that verdict.

## Where it plugs in

`build_watch_report` already loops every delivering ad, computes `days_since_change`, and calls
`classify_ad`. Add an early-life branch **for ads whose age (`as_of - first_seen`) ≤
`EARLY_LIFE_MAX_AGE`**, where `first_seen` comes from the history provider (the ad's earliest
`report_date`), NOT `updated_time`. (`days_since_change` is "days since last edit" — a different
quantity; an ad edited yesterday but launched two weeks ago is not early-life. Use the provider's
`first_seen` for age.)

The scan gains a `HistoryProvider` parameter (default `DuckDBHistoryProvider`), fetched once per scan
and passed into the per-ad triage. Keep the scan's existing read-only / no-write posture.

### Reconciliation with the grace window (the central interaction)

The grace window must keep protecting ordinary recently-changed ads. The triage only changes behavior
for **genuinely brand-new** ads (age ≤ `EARLY_LIFE_MAX_AGE`). Rules:

- **age ≤ `EARLY_LIFE_MAX_AGE` (days 1–3):** the early-life triage **supersedes** the blanket
  grace-window `watch`/`insufficient` abstain for *this* ad:
  - `not_struggling` → behave as today (`ok`/`watch`); no early action.
  - `keep_watch` or `abstain_keep` → emit a `watch`-class row **and** file a day-3 follow-up. The row
    must read as "kept on probation, re-check day 3", carrying the analog evidence/confidence — not a
    bare "protected".
  - `pause_candidate` → emit an early **pause-candidate** row carrying the analog `Evidence` +
    `analog_confidence` band. This does NOT write to the account; it is surfaced for the operator/agent
    to route through `propose-pause-ads` (the existing guarded flow). Do not invent a new write path.
- **`EARLY_LIFE_MAX_AGE` < age < `grace_days`:** unchanged — existing protective `watch`. The triage
  does not touch these, so there is no contradiction.
- **age ≥ `grace_days`:** unchanged — existing `classify_ad` logic.

An ad therefore can never be both "protected/abstain" and "early-pause flagged": for age ≤ max the
triage verdict is the single source of truth; outside that range the triage does not run.

### Day-3 forced decision

"By the third day we should 100% be making a solid keep-or-kill decision." Drive this off the
**probation follow-up**, so no new state file is needed:

- An open early-life follow-up for an ad = that ad is on probation.
- When the ad reaches **age ≥ `EARLY_LIFE_DECISION_AGE`** (day 3) and has an open probation follow-up:
  - If its own life-to-date sample now clears the significance floor → use the **normal**
    `classify_ad` direct-observation call (real confidence, keep or pause). The grace-window abstain is
    explicitly overridden for this ad because we deliberately put it on probation — document this in
    code comments and a reason string.
  - If still below floor → the **analog verdict governs** the keep-vs-pause call (no indefinite
    abstain). `keep_watch`/`abstain_keep` → keep; `pause_candidate` → pause candidate.
  - Either way, **close** the probation follow-up (`mark_done`) so it does not loop forever.

### Follow-up file: filing, closing, dedupe

Use `followups.add_followup` / `mark_done` / `iter_followups`. The day-3 follow-up:

- `due` = `first_seen + EARLY_LIFE_DECISION_AGE` (deterministic from `as_of`/`first_seen`, not the
  clock).
- title/body identifies the ad (ad_id + name) and carries the analog basis so the day-3 reader has
  context without re-deriving it.
- **Dedupe:** before filing, scan `iter_followups(account)` for an existing **open** early-life
  follow-up for the same `ad_id` (match on an `early-life-triage` marker + ad_id embedded in the
  filename slug and/or frontmatter). If one exists, do **not** file a duplicate across runs. The
  filename today is `{due}-{slug}.md`; make the slug deterministic per ad (e.g.
  `early-life-triage-<ad_id>`) so re-runs collide on the same file rather than spamming new ones.
- Add a small helper in `followups.py` if needed (e.g. `find_open_followup(account, marker)` /
  `add_followup_if_absent(...)`) rather than open-coding the scan in `monitor.py`.

## CLI (`watch_main` in `cli.py`)

- Construct a `DuckDBHistoryProvider` (default DB path) and pass it to `build_watch_report`.
- Add an `--early-life` toggle / it defaults on; expose the `EARLY_LIFE_*` knobs as optional flags
  mirroring the existing `--grace-days` / `--min-spend` style (default from config).
- Extend the printed summary with the early-life buckets (kept-on-probation N · early-pause-candidate
  N) and per-row reasons, mirroring the existing `line(r)` renderer. Pause-candidate rows must show
  the analog basis ("X analogs at age N, R recovered").
- File/close the day-3 follow-ups as a side effect of the run (the scan returns what to file; the CLI
  writes — keep `build_watch_report` itself write-free for follow-ups too, or pass the followup
  writer in. Prefer: scan returns a `followup_actions` list, CLI applies it, so the pure scan stays
  testable without touching the filesystem).

## Watch-report schema additions

Each early-life row needs: `early_life: true`, `age`, the triage `verdict`, `analog_basis`,
`confidence`, `evidence`, and `reasons`. Bump `schema_version` if the consumers assert on it. Keep the
existing row fields intact for non-early-life rows.

## Edge cases & interactions

- **`first_seen` vs `updated_time`:** age uses the provider's `first_seen` (launch), protection grace
  uses `updated_time` (edit). A two-week-old ad edited yesterday is NOT early-life — must not be
  triaged. Test it.
- **Provider returns no history for a delivering ad** (brand-new, not yet in a synced snapshot): triage
  returns `None` → fall back to today's `classify_ad` behavior (don't crash, don't force a verdict).
- **Duplicate follow-ups across runs:** running the scan twice in one day, or on consecutive days while
  the ad is still on probation, must not create a second follow-up for the same ad. Test the dedupe.
- **Follow-up closure idempotency:** closing an already-done / already-moved follow-up must not raise
  (`mark_done` raises `FileNotFoundError` if missing — guard it).
- **Day-3 override of grace:** an ad at age 3 on probation must get a real keep/kill, NOT the
  grace-window `watch`. An ad at age 3 *not* on probation keeps today's behavior. Test both.
- **pause_candidate never auto-writes:** the row is flag-only; assert the scan performs no account
  write and produces an op only through the existing `propose-pause-ads` path when the operator runs
  it. (Confirm the row carries enough — ad_id, evidence, confidence — for that flow.)
- **Determinism / clock-free:** `due` dates and ages derive from `as_of`/`first_seen` only. No
  `date.today()` inside `build_watch_report`.
- **Goal-aware:** ROAS and install-goal accounts both flow through; reuse the engine's goal handling.

## Key tests (mocks-only — fake `HistoryProvider`, monkeypatched `followups` root / `tmp_path`)

- early-life struggling ad with recovering analogs → watch report row `verdict == "keep_watch"`, a
  day-3 follow-up filed at `first_seen + decision_age`, no account write.
- early-life struggling ad with non-recovering analogs → row `verdict == "pause_candidate"` carrying
  `analog_confidence` (≤ medium) + evidence; still no write.
- ad edited yesterday but launched 2 weeks ago → NOT triaged (age from `first_seen`), normal path.
- running the scan twice → exactly one follow-up file for the ad (dedupe).
- ad at age 3 on probation whose own sample now clears floor → real `classify_ad` keep/kill, follow-up
  closed; grace-window abstain overridden.
- ad at age 3 on probation still below floor → analog verdict governs keep vs pause; follow-up closed.
- provider has no history for a delivering ad → falls back to existing `classify_ad`, no crash.
- `build_watch_report` performs no filesystem follow-up writes itself (the CLI applies the returned
  follow-up actions) — pure-scan test asserts the returned `followup_actions` without touching disk.

## TODO

### Phase 1 — scan integration (pure)
- [ ] Add `history_provider` param to `build_watch_report`; fetch histories once.
- [ ] Add the early-life branch keyed on age (`first_seen` from provider), calling
      `early_triage.triage_ad`; map verdicts to row classifications + `followup_actions`.
- [ ] Implement the day-3 forced-decision path (override grace for probation ads; close follow-up).
- [ ] Extend the watch-report row/schema with the early-life fields; keep `build_watch_report`
      filesystem-free (return `followup_actions`).

### Phase 2 — followups helpers
- [ ] Add dedupe-aware helper(s) to `followups.py` (`find_open_followup` / `add_followup_if_absent`,
      deterministic per-ad slug + marker). Guard `mark_done` against missing files.

### Phase 3 — CLI
- [ ] Wire `DuckDBHistoryProvider` + `EARLY_LIFE_*` flags into `watch_main`; apply `followup_actions`
      (file/close); extend the printed summary + row renderer with the early-life buckets.

### Phase 4 — tests + checks
- [ ] Add the tests above to `tests/test_meta_ads_analysis.py`.
- [ ] Run the suite + type/lint checks; stream output with `tee`. Flag any pre-existing unrelated
      failure per the runner's `.pre-existing-error.md` protocol — do not chase it.

## Handoff honesty (for the reviewer)

- The `pause_candidate` row is **flag-only**: this ticket surfaces it for the operator to route through
  `propose-pause-ads`; it does not auto-build a pause op. If a future ticket wants the scan to emit a
  ready-to-approve grounded pause op directly, that is deliberately out of scope here (file it as
  follow-on work rather than expanding this ticket).
- Install-goal targets depend on the install-goal grounding work
  (`confidence-install-goal-significance-ops`, `review-gate-install-goal-direction`); use whatever
  target the policy exposes today and degrade gracefully when absent — do not block on those tickets.
