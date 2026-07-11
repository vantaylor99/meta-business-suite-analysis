description: The budget-pacing tool now gives lifetime-budget accounts a real over/under/on-track verdict by working out how much of the fixed budget should have been spent by now, instead of giving up on them.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What was implemented

`pacing_report` previously classified any lifetime-only account `budget_not_projectable` — it reported
the raw `lifetime_budget_total` but produced no verdict, because a lifetime budget is paced by Meta
over the entity's own `start_time..stop_time` schedule rather than the reporting window. This ticket
teaches the tool to **prorate** each lifetime pot across the overlap of its schedule with the window
and fold that additively into the existing daily period-budget math, so lifetime and mixed accounts
now earn `over`/`under`/`on_track`. Daily-only output stays byte-identical.

### Phase 1 — data surface (`account_discovery.py`)
- `PACING_CAMPAIGN_FIELDS` / `PACING_ADSET_FIELDS` now also fetch `start_time` / `stop_time`
  (still budget-only reads; no targeting/objective bloat).
- `summarize_account_budget` return shape widened from `dict[str, float]` to `dict[str, Any]`, adding
  `lifetime_entities`: one `{"lifetime_budget": float (major units), "start_time": str|None,
  "stop_time": str|None}` per ACTIVE lifetime-owning entity. CBO precedence is preserved — the campaign
  emits for a CBO-lifetime campaign, the adset for a non-CBO adset-lifetime; a CBO-daily campaign emits
  no lifetime entity for its (ignored) adsets. `active_daily` / `lifetime_total` unchanged.

### Phase 2 — proration helper (`account_discovery.py`)
- `_overlap_days(a_start, a_end, b_start, b_end)` — inclusive-day overlap of two closed date ranges.
- `_parse_schedule_date(value)` — leading `YYYY-MM-DD` of a Meta ISO string → `date`, else `None`
  (drops the `T…` time/offset suffix; timezone-agnostic calendar days, consistent with the rest of the
  tool).
- `lifetime_pacing(lifetime_entities, *, date_from, date_to, effective_as_of)` — pure, clock-free.
  Returns `{period_budget, expected_to_date, n_entities, n_projectable}` summed over projectable
  entities: `period_budget = Σ lifetime_i * overlap_full_i / schedule_total_i`,
  `expected_to_date = Σ lifetime_i * overlap_todate_i / schedule_total_i`. Non-projectable (contributes
  0): missing/≤0 budget, blank/unparseable start OR stop, `stop <= start`, or no overlap with the
  window.

### Phase 3 — wiring + classification (`account_discovery.py`)
- `classify_pacing` projectability guard relaxed: dropped the `active_daily_budget <= 0` clause, now
  `if period_budget <= 0 or projected_spend is None`. The caller passes the **combined** daily+lifetime
  `period_budget`/`projected_spend`, so a projectable lifetime-only account is admitted while a
  cap-only account (combined `period_budget == 0`) still lands `budget_not_projectable`. For daily
  accounts `period_budget = active_daily * total_days`, so `period_budget > 0 ⇔ active_daily > 0` —
  daily outcomes unchanged.
- `pacing_report` loop branches on `lifetime["period_budget"] > 0`:
  - **combined** (lifetime/mixed): `period_budget = daily*total_days + lifetime_pb`;
    `expected_to_date = daily*total_days*elapsed_fraction + lifetime_expected`;
    `projected = spend * period_budget / expected_to_date` (None when `expected_to_date == 0`).
  - **daily-only** (`else`): the LITERAL pre-existing computation
    (`active_daily * total_days` + `project_spend(spend, elapsed_fraction)`) so output is byte-identical
    to the last ULP.
  - **No new per-account keys** — proration is reflected only in the existing `period_budget`,
    `projected_spend`, `variance_pct`, `status`. `lifetime_budget_total` still reports the raw sum.

### Phase 4 — docs (`account_discovery.py`, `mcp_server.py`)
- Updated the "reported but NOT projected" caveats in the `summarize_account_budget`, `pacing_report`,
  and `classify_pacing` docstrings, and the `pacing_report` LLM tool description in `mcp_server.py`, to
  describe proration and the residual `budget_not_projectable` cases (open-ended / non-overlapping /
  cap-only).

## How to validate

Run: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -k pacing -q`
(and the full module: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q`).

**Result at handoff:** full module green — `601 passed`. `python` isn't on PATH; use `.venv/bin/python`.

### Tests added
- `test_summarize_account_budget_lifetime_entities` — CBO-lifetime campaign emits the campaign+schedule;
  non-CBO adset-lifetime emits per-adset; daily-only and CBO-daily-with-decoy-adset emit none; paused
  campaign emits none.
- `test_lifetime_pacing_proration` — schedule==window, wholly-inside, straddling-start, no-overlap,
  open-ended, missing-start, `stop<=start`, entity-not-yet-started (overlap_full>0 but
  overlap_todate==0), multi-entity aggregate, empty input.
- `test_classify_pacing_status_enum_and_boundaries` (extended) — the relaxed guard admits a
  lifetime-only account with `active_daily_budget=0` but combined `period_budget>0` → `over`; the
  `period_budget=0` lifetime-only case still → `budget_not_projectable`.
- `test_pacing_report_prorates_lifetime_budgets_end_to_end` — lifetime-only overlapping (real verdict +
  correct `period_budget`/`projected_spend`/`variance_pct`), lifetime-only non-overlapping (stays
  `budget_not_projectable`), mixed daily+straddling-lifetime (combined `period_budget = daily +
  prorated lifetime`), and rollup coherence (status_counts, `excluded_from_rollup` drops to 1,
  normalized totals include the projectable accounts, shortlist ordering).
- `_pc_camp` / `_pc_adset` helpers extended with optional `start_time` / `stop_time` kwargs.

### Byte-identical guard (do NOT edit)
`test_pacing_report_end_to_end_statuses_rollup_and_shortlists` is unchanged and green: its
`act_lifetime` fixture (`_pc_camp("c5", lifetime="500000")`, no schedule) still reads
`budget_not_projectable`, and all daily accounts keep identical `period_budget`/`projected_spend`/
`variance_pct`. If a reviewer finds this test needs editing, the daily path was altered incorrectly.

## Known gaps / things to scrutinize (reviewer: treat as a floor)

- **Account-level spend vs per-entity schedules.** A mixed or multi-lifetime account has a single
  `spend_to_date` (whole-account insights over `[date_from, effective_as_of]`) driving one combined
  `variance_pct`. We cannot attribute spend to individual entities. This is the intended account-level
  design (documented in the source ticket), but it means a mixed account where one campaign over-paces
  and another under-paces nets to one blended verdict. Worth a sanity read.
- **Timezone.** Both the window dates and the parsed schedule bounds are naive calendar days
  (`str(value)[:10]`), so `stop_time="2026-07-31T23:59:00-0700"` counts as ending 2026-07-31 regardless
  of offset. Consistent with `pacing_period` and the rest of the tool; flagged as an intentional
  simplification, not verified against Meta's actual TZ semantics for lifetime pacing boundaries.
- **`not_started` / `account_inactive` interaction.** For a global `elapsed_fraction <= 0` or a paused
  account, `classify_pacing` short-circuits before the lifetime verdict matters, so proration is
  computed-then-ignored (harmless). No dedicated test exercises a lifetime account under those
  short-circuits — a reviewer may want to add one (behavior traced by hand: when `elapsed_fraction<=0`,
  `effective_as_of = date_from-1` → all `overlap_todate=0` → `expected_to_date=0` → `projected=None`,
  and `not_started` wins the guard order regardless).
- **`expected_to_date == 0` with `period_budget > 0`.** A pure-lifetime account whose entities all
  overlap the window but none have started as of `as_of` yields `projected=None` →
  `budget_not_projectable` (can't divide). Covered indirectly by the `not_started_yet` unit case on
  `lifetime_pacing`; not exercised end-to-end through `pacing_report`.
- **No lint/type gate configured** (no mypy/ruff in `pyproject.toml`), so only pytest + a syntax/import
  smoke check were run. Type annotations were added by hand.

## Acceptance (all met)
- Lifetime-only account with an overlapping schedule → real `over`/`under`/`on_track` grounded in the
  prorated expectation, and it enters status_counts/shortlists/normalized totals (drops from
  `excluded_from_rollup`).
- Daily accounts unaffected — byte-identical e2e guard test unedited & green.
- The "reported but NOT projected" caveat updated in the `pacing_report` / `summarize_account_budget` /
  `classify_pacing` docstrings and the `mcp_server.py` tool description.
