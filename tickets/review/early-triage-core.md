description: Review the new early-life ad-triage engine that grades a struggling brand-new ad against how similar past ads on the same account behaved at the same age, so a genuinely-bad ad is told apart from a slow-starting eventual winner.
prereq:
files: src/meta_ads_analysis/early_triage.py (new), src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/storage.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What was built (implement → review handoff)

The **pure engine + data seam** for early-life ad triage. No CLI wiring, no monitor changes, no
follow-up writing — that is the sibling ticket `early-triage-monitor-integration` (which lists this as
its prereq). Everything here is unit-tested against hand-built `AdHistory` lists / a fake provider; no
live Meta. Clock-free: `as_of` is always passed in.

Build + tests: **`python -m pytest tests/test_meta_ads_analysis.py` → 316 passed** (301 pre-existing +
15 new). No pre-existing failures observed; no `.pre-existing-error.md` filed. (Run from the repo
venv: `source .venv/bin/activate`. No ruff/mypy/pyright is configured in this repo — pytest is the
only gate.)

### Files & surface

- **`src/meta_ads_analysis/early_triage.py`** (new) — the whole module:
  - Data seam: `AdDailyPoint`, `AdHistory` (with `first_seen` / `last_seen` / `age_on(as_of)` /
    `last_age`), `HistoryProvider` (Protocol).
  - Pure engine: `triage_ad(...) -> EarlyTriageVerdict | None`.
  - `EarlyTriageVerdict(verdict, age, reasons, analog_basis, confidence, evidence)`.
  - Concrete provider: `DuckDBHistoryProvider(db_path)` + module-level `group_histories(rows)` (the
    grouping is exported so it is testable without a DB). All SQL lives in the provider.
- **`src/meta_ads_analysis/confidence.py`** — added `analog_confidence(*, analogs, recovered,
  min_analogs, strong_analogs, factors)`: correlational tier (ceiling medium), data band computed
  deterministically from analog count, capped at medium. Construction stays inside `confidence.py`.
- **`src/meta_ads_analysis/config.py`** — added the documented `EARLY_LIFE_*` /
  `ANALOG_RATIO_TOLERANCE` block (plus one extra constant — see "Decisions" below).

### Verdict ladder (what `triage_ad` returns)

- `None` — ad not found in `histories`, or `age > EARLY_LIFE_MAX_AGE` (not early-life). Caller skips.
- `not_struggling` — short-circuits before any analog work; leave to normal flow.
- `abstain_keep` — `A < EARLY_LIFE_MIN_ANALOGS` (thin/no comparable history) OR install-goal account
  with no target install cost in policy. Never a confident early kill. Uses `abstain_confidence`.
- `keep_watch` — `A >= min` and `R/A >= EARLY_LIFE_RECOVERY_RATE`. Uses `analog_confidence`.
- `pause_candidate` — `A >= min` and `R/A < rate`. Uses `analog_confidence`.

`A` = qualifying analogs (matched **and** old enough to judge recovery, i.e. `last_age >=
EARLY_LIFE_RECOVERY_HORIZON`); `R` = those whose `[age+1 .. horizon]` window cleared the account
target. Matched-but-too-short-lived analogs are excluded from `A` entirely.

## Use cases / what to validate (tests are a floor, not a ceiling)

Covered by the 15 new tests (names start `test_early_triage_*`, `test_analog_confidence_*`,
`test_group_histories_*`, `test_duckdb_history_provider_*`):

- keep_watch when comparable new ads recovered (zero-result day-1 fallback match path).
- survivorship: 1/20 recovered → pause_candidate (rate over the population, not "any recovery").
- strong population (6 analogs) → band medium; 5 analogs → band low (the knee, see Decisions).
- too few analogs (2) → abstain_keep via `abstain_confidence`.
- not_struggling short-circuits (no analog work, A=0).
- install goal grades on cost-per-install; install goal with no target degrades to abstain (no crash).
- too-short-to-judge analog excluded from the population count (vs an older stayed-bad analog).
- age purely `(as_of - first_seen).days`; deterministic (repeat call == itself); clock-skew clamps to 0.
- returns None for missing ad / age past the early-life window.
- `analog_confidence` capped at medium even with 50 analogs all recovered; full ladder.
- `group_histories` parses str+date `report_date`, drops id-less rows, sorts; `DuckDBHistoryProvider`
  picks the latest `ingestion_run_date` and groups per ad; empty for unknown account.

**Suggested adversarial probes for the reviewer:**

- Ratio tolerance boundaries: an analog exactly at 2.0× / 0.5× cost-per-result (or spend) — confirm
  inclusive `[tol, 1/tol]` is intended.
- Mixed result-presence: triaged ad has purchases but a candidate has none (and vice-versa) — should
  NOT match. (Engine returns False; a direct test of this exact path is not yet written — gap below.)
- Sparse/gappy daily series (missing calendar days) — age is computed from `report_date`, not row
  index, so verify a candidate with a gap inside `[age+1..horizon]` still recovers/doesn't correctly.
- An ad with multiple rows for the same `report_date` (shouldn't happen post-normalize) would be
  double-counted by `group_histories` — confirm whether that invariant needs a guard.

## Decisions made (resolved here; reviewer should sanity-check, not re-litigate)

- **5-analog "medium" narrative reconciled to the explicit knee.** The source ticket's prose called a
  5-analog call "medium", but the same ticket sets `EARLY_LIFE_STRONG_ANALOGS = 6` and the confidence
  spec says "medium if analogs >= strong_analogs". I treated the explicit constant/spec as
  authoritative: **5 analogs → `low`, 6+ → `medium`.** Documented inline in code and in the keep_watch
  test. If the operator really wants 5 → medium, drop `EARLY_LIFE_STRONG_ANALOGS` to 5 (one-line
  config change) — no logic change.
- **New constant `EARLY_LIFE_MIN_SPEND = 10.0`** (≈10% of `MIN_WASTE_SPEND`), not in the ticket's
  listed knobs. The edge-case section *required* defining "non-trivial spend"; I made it a documented
  constant that gates the whole struggling test, so a $0.50 day-1 ad is never force-graded and analogs
  must also clear it to count as "also struggling". Reviewer: confirm $10 is a sane floor for a day-1
  ad on these accounts (configurable).
- **Goal-aware result count is derived, not read from `AdDailyPoint.results`.** `results` is carried
  on the point (raw goal primary-result column) for completeness, but struggling/matching/recovery use
  `purchase_count` (ROAS) / `app_installs` (install) so the metric matches
  `actions._select_action_metric` (`blended_roas` / `cost_per_app_install`). `results` is therefore
  currently unused by any decision — left in place as documented schema for the integration ticket.
- **Install target field.** Uses `secondary_cost_per_app_install_target`, falling back to
  `pause_if_no_primary_and_secondary_cost_above`, then graceful abstain. (Both are 3.0 in the current
  `pollen_sense` policy.) Note the open install-goal grounding backlog items — this does not block on
  them; it uses whatever the policy exposes today.
- **`as_of` before `first_seen`** clamps age to 0 (treat as day-1), rather than returning None.
- **`decision_age`** is accepted as a parameter but only shapes the "re-check by day N" wording in
  `reasons`; the actual day-3 forcing is the integration ticket's responsibility.

## Known gaps / honest caveats (treat the implementation as a starting point)

- **ROAS is computed as `purchase_value / spend` directly**, NOT through `analyze._reliable_roas`
  (which zeroes ROAS for `low_results_without_revenue` tracking). For an account/ad with untrustworthy
  revenue, the analog ROAS here may differ from the report's blended ROAS. Acceptable for a
  cross-sectional *relative* comparison, but the reviewer should decide whether to honor tracking
  confidence here too. The `AdDailyPoint` does not currently carry `tracking_confidence`.
- **No direct test for the "triaged has results, candidate has none" non-match path** (and its
  mirror). The engine handles it (`_is_analog` returns False) but it is only covered indirectly.
- **`DuckDBHistoryProvider` is validated against a synthetic fixture DB**, not a real synced snapshot.
  The real `ad_daily_metrics` has many more columns; the provider reads only `ad_id`, `ad_name`,
  `report_date`, `spend`, `purchase_count`, `purchase_value`, `app_installs`, `results`. It calls
  `storage.initialize_database` on the read path (idempotent CREATE IF NOT EXISTS) so a brand-new DB
  yields `[]` instead of erroring — confirm that's acceptable on a read path.
- **Recovery window is `[age+1 .. horizon]` cumulative** (the spec's wording), i.e. it excludes the
  first-N struggling days by design. If the operator actually wants life-to-date-through-horizon, that
  is a one-line change in `triage_ad`'s `_points_in_age_range(..., age + 1, recovery_horizon)`.
- This is the engine only. The consumer (`early-triage-monitor-integration`) renders the
  `analog_basis` / `reasons` / `evidence` and routes `pause_candidate` through the guarded propose
  flow — none of that is exercised here.
