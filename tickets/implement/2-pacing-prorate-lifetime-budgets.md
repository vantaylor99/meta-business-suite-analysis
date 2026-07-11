description: The budget-pacing tool can only judge whether an account is on track when its budget is a daily amount; when the budget is instead a single lifetime pot spread over a campaign's whole run, it just reports the number and gives up. Teach it to work out, for those lifetime budgets, how much should have been spent by now and whether the account is ahead of or behind schedule.
prereq: pacing-currency-aware-minor-units
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## Context

`pacing_report` (`src/meta_ads_analysis/account_discovery.py:1848`) answers "is each account on
track to spend its budget for the reporting window `[date_from, date_to]`, measured through
`as_of`?". It is a two-source join: spend-to-date from `cross_account_performance`, budget config
from a per-account `list_campaigns` + `list_adsets` + `get_account` fan-out.

Today the **authoritative period budget** is the CBO-deduplicated sum of **ACTIVE daily budgets**
times the period length (`summarize_account_budget` → `active_daily`; `pacing_report` line ~2008:
`period_budget = active_daily * total_days`). An account whose only budget is a **lifetime budget**
(a fixed pot Meta paces over the entity's own `start_time`..`stop_time`) is classified
`budget_not_projectable` (`classify_pacing`, `account_discovery.py:1821-1825`): its
`lifetime_budget_total` is reported for context, but no over/under verdict is produced, because a
lifetime budget spans the entity's own schedule, not the arbitrary reporting window. The budget read
(`PACING_CAMPAIGN_FIELDS` / `PACING_ADSET_FIELDS`, lines 1626-1633) deliberately does not fetch the
schedule needed to prorate.

## What to build

Give a lifetime-budget entity a real over/under/on_track verdict by **prorating its lifetime budget
across the overlap between its own schedule and the reporting window**, then folding that into the
existing classification. Fold into the existing `over` / `under` / `on_track` statuses — **do not add
a new status enum value** (keeps `_PACING_STATUSES`, the rollup, and shortlists unchanged and keeps
daily-only output byte-identical).

### The unifying identity (why this stays clean)

The daily path today computes `variance_pct = (projected_spend - period_budget) / period_budget`
where `projected_spend = spend_to_date / elapsed_fraction` and `period_budget = active_daily *
total_days`. This is **algebraically identical** to an expected-to-date framing:

```
expected_to_date        = period_budget * elapsed_fraction          (= active_daily * elapsed_days)
variance_pct            = (spend_to_date - expected_to_date) / expected_to_date
projected_spend         = spend_to_date * period_budget / expected_to_date   (= spend / elapsed_fraction)
```

Both give the same `variance_pct`. That lets lifetime and daily budgets combine additively:

```
daily_period_budget     = active_daily * total_days
daily_expected_to_date  = daily_period_budget * elapsed_fraction
lifetime_period_budget  = Σ over entities: lifetime_i * overlap_full_i  / schedule_total_i
lifetime_expected_todate= Σ over entities: lifetime_i * overlap_todate_i / schedule_total_i

period_budget    = daily_period_budget    + lifetime_period_budget       # full-window denominator
expected_to_date = daily_expected_to_date + lifetime_expected_todate
projected_spend  = spend_to_date * period_budget / expected_to_date       # feeds classify + rollup
variance_pct     = (projected_spend - period_budget) / period_budget      # == (spend - expected)/expected
```

Where:
- `overlap_full_i`  = inclusive-day overlap of entity schedule `[start, stop]` with `[date_from, date_to]`.
- `overlap_todate_i`= inclusive-day overlap of entity schedule with `[date_from, effective_as_of]`.
- `schedule_total_i`= inclusive days of the entity's own `[start, stop]`.

**Byte-identical guarantee for daily-only accounts.** Do NOT route daily-only accounts through the
`spend * period_budget / expected_to_date` form — floating point may differ in the last ULP from
today's `spend / elapsed_fraction`. Branch: only accounts with `lifetime_period_budget > 0` use the
combined form; accounts with no projectable lifetime overlap keep the literal existing computation
(`project_spend(spend_to_date, elapsed_fraction)` and `active_daily * total_days`).

## Interfaces / shapes

**Field lists** (`account_discovery.py:1626-1633`) — add schedule, still budget-only:
```python
PACING_CAMPAIGN_FIELDS = ["id", "effective_status", "daily_budget", "lifetime_budget",
                          "start_time", "stop_time"]
PACING_ADSET_FIELDS    = ["id", "campaign_id", "effective_status", "daily_budget",
                          "lifetime_budget", "start_time", "stop_time"]
```

**`summarize_account_budget`** — additionally return the ACTIVE lifetime entities with their
schedules (major units), so the caller can prorate per-entity (different campaigns/adsets have
different schedules; `lifetime_total` alone is insufficient). The lifetime entity is whichever level
owns the lifetime budget under the existing CBO precedence — the campaign for a CBO-lifetime campaign,
the adset for a non-CBO adset-lifetime:
```python
{
  "active_daily": float,
  "lifetime_total": float,          # unchanged (Σ all ACTIVE lifetime budgets, major units)
  "lifetime_entities": [            # NEW — one per ACTIVE lifetime-owning entity
    {"lifetime_budget": float,      #   major units, currency-aware via _minor_to_major
     "start_time": str | None,      #   raw Meta ISO string, verbatim
     "stop_time":  str | None},
    ...
  ],
}
```
Existing `summarize_account_budget` tests index `active_daily` / `lifetime_total` by key (no
whole-dict `==`), so adding a key is safe.

**New pure helper `lifetime_pacing`** (clock-free, all dates explicit — same testability posture as
`pacing_period`):
```python
def lifetime_pacing(
    lifetime_entities: list[dict[str, Any]],
    *, date_from: str, date_to: str, effective_as_of: str,
) -> dict[str, Any]:
    """Prorate a set of lifetime-budget entities across the reporting window.

    Returns aggregated major-unit figures over all *projectable* entities:
      {
        "period_budget":    float,  # Σ lifetime_i * overlap_full_i  / schedule_total_i
        "expected_to_date": float,  # Σ lifetime_i * overlap_todate_i / schedule_total_i
        "n_entities":       int,    # total lifetime entities considered
        "n_projectable":    int,    # entities with a valid schedule AND overlap_full > 0
      }
    An empty / all-non-projectable input returns zeros (caller then keeps budget_not_projectable).
    """
```
Inclusive-day arithmetic mirrors `pacing_period` (`(end - start).days + 1`). Parse `start_time` /
`stop_time` by taking the leading `YYYY-MM-DD` (`str(value)[:10]`, then `date.fromisoformat`) —
timezone-agnostic calendar days, consistent with the rest of the tool. A blank/unparseable/missing
bound makes that entity non-projectable (contributes 0). Add a small internal
`_overlap_days(a_start, a_end, b_start, b_end) -> int = max(0, (min(a_end,b_end) - max(a_start,b_start)).days + 1)`.

**`classify_pacing`** — one-line guard relaxation only. Replace:
```python
if active_daily_budget <= 0 or period_budget <= 0 or projected_spend is None:
    return {"status": "budget_not_projectable", "variance_pct": None}
```
with:
```python
if period_budget <= 0 or projected_spend is None:
    return {"status": "budget_not_projectable", "variance_pct": None}
```
Rationale: the caller now passes the **combined** `period_budget` (daily + prorated lifetime) and a
non-None combined `projected_spend` for a projectable lifetime account, so dropping the
`active_daily_budget <= 0` clause lets lifetime-only accounts through. Verify this does not change any
daily-only outcome: for daily accounts `period_budget = active_daily * total_days`, so
`period_budget > 0 ⇔ active_daily > 0`; and a cap-only account (no daily, no lifetime) yields a
combined `period_budget = 0` → still `budget_not_projectable`. The `no_budget_set` and
`account_inactive` / `not_started` guards are unchanged and still short-circuit first.

**`pacing_report` loop** (`account_discovery.py:~2000-2019`) — after `summarize_account_budget`:
```python
lifetime = lifetime_pacing(budget["lifetime_entities"],
                           date_from=date_from, date_to=date_to, effective_as_of=effective_as_of)
daily_period_budget = active_daily * total_days
if lifetime["period_budget"] > 0:
    period_budget    = daily_period_budget + lifetime["period_budget"]
    expected_to_date = daily_period_budget * elapsed_fraction + lifetime["expected_to_date"]
    projected = (spend_to_date * period_budget / expected_to_date) if expected_to_date > 0 else None
else:
    period_budget = daily_period_budget                       # byte-identical daily-only path
    projected     = project_spend(spend_to_date, elapsed_fraction)
```
Then feed `period_budget` / `projected` into `classify_pacing` and the normalized-twin conversion
exactly as today. **Do NOT add new keys to the per-account entry dict** — the proration is reflected
in the existing `period_budget`, `projected_spend`, `variance_pct`, and `status`; `lifetime_budget_total`
still reports the raw sum. (Adding a key to every entry would break the daily byte-identical criterion.)

## Edge cases & interactions

- **Open-ended (no `stop_time`)** — no schedule denominator → entity non-projectable, contributes 0.
  A lifetime-only account with only open-ended entities stays `budget_not_projectable`. (Meta requires
  an end time for lifetime budgets, so this is a data-anomaly guard.) Document.
- **Missing `start_time`** (but stop present) — need both bounds → non-projectable, contributes 0.
- **`stop_time` <= `start_time`** (bad data) — `schedule_total <= 0` → non-projectable, contributes 0.
- **No overlap** — schedule wholly before `date_from` or after `date_to` → `overlap_full = 0` →
  contributes 0. A lifetime-only account with only non-overlapping entities stays
  `budget_not_projectable`.
- **Schedule wholly inside the window** — `overlap_full == schedule_total` → the whole pot is expected
  within the window; `entity_period_budget == lifetime_budget`.
- **Schedule straddles a window edge** — overlap clipped to the window; budget prorated to the
  in-window fraction of the schedule.
- **Window elapsed but this entity's schedule not yet started** (`overlap_todate = 0`,
  `overlap_full > 0`) — `expected_to_date` contribution 0. If the account is pure-lifetime and *all*
  entities are in this state, combined `expected_to_date == 0` → `projected = None` →
  `budget_not_projectable` (can't divide). A concurrent daily budget rescues it.
- **Mixed account (daily + lifetime)** — both fold additively into `period_budget` /
  `expected_to_date`; the account's single spend-to-date drives one combined `variance_pct`. It leaves
  `budget_not_projectable`/daily-only-projection territory and gets a combined verdict.
- **CBO precedence preserved** — a CBO-daily campaign still ignores its adsets (no lifetime entity
  emitted for its adsets); only the level that owns the lifetime budget emits a lifetime entity. Guard
  against double-counting exactly as `summarize_account_budget` does today.
- **`not_started` / `account_inactive` short-circuit** — these classify before the lifetime math, so
  proration is computed-then-ignored for those accounts (harmless).
- **Currency** — lifetime budgets convert through the currency-aware `_minor_to_major` (per the
  `pacing-currency-aware-minor-units` prereq); `variance_pct` is FX-invariant (native ratio), the
  rollup's normalized totals use the FX-converted `period_budget`/`projected_spend` twins.
- **Rollup coherence** — projectable lifetime/mixed accounts now carry non-zero
  `period_budget_normalized` / `projected_spend_normalized`, so they enter `total_period_budget_normalized`,
  `total_projected_normalized`, `overall_variance_pct`, and the worst-pacer shortlists automatically
  (no rollup code change). Confirm `excluded_from_rollup` drops accordingly.
- **Determinism** — `lifetime_entities` order follows the campaign/adset read order; the aggregate sum
  is order-independent. No reliance on dict/set iteration order in the aggregation.

## Docstring / tool-description updates

- `summarize_account_budget` docstring (`account_discovery.py:1749-1751`) — replace the "lifetime
  budgets are summed for reporting only … a backlog follow-up" caveat with a note that lifetime
  entities + schedules are now returned for proration.
- `pacing_report` docstring (`account_discovery.py:1879-1881`) — replace "Lifetime budgets are
  reported but NOT projected … a lifetime-only account is `budget_not_projectable`" with the proration
  behavior and the non-projectable residual cases (open-ended / no-overlap / no-schedule).
- `classify_pacing` docstring item 4 (`account_discovery.py:1807-1808`) — restate
  `budget_not_projectable` as "a lifetime/cap-only account with no projectable schedule overlap"
  rather than "has a lifetime budget … but ZERO active daily budget".
- `mcp_server.py:473-474` LLM tool description — change "Lifetime budgets are reported but not
  projected (budget_not_projectable)" to reflect that lifetime budgets are now prorated across their
  schedule's overlap with the window and get over/under/on_track, with `budget_not_projectable`
  reserved for open-ended / non-overlapping / cap-only accounts.

## Key tests (add to `tests/test_meta_ads_analysis.py`)

Extend the `_pc_camp` / `_pc_adset` helpers (lines 11607-11622) to accept optional
`start_time` / `stop_time`.

- **`lifetime_pacing` unit** (pure, explicit dates over `[2026-07-01, 2026-07-31]`, `as_of=2026-07-14`):
  - schedule == window (`2026-07-01`..`2026-07-31`), lifetime `9300` → `period_budget == 9300`,
    `expected_to_date == 9300 * 14/31` (schedule_total 31, overlap_todate 14).
  - schedule wholly inside (`2026-07-05`..`2026-07-20`, 16 days), lifetime `1600` → `period_budget ==
    1600`; `overlap_todate` = `2026-07-05`..`2026-07-14` = 10 days → `expected_to_date == 1600*10/16`.
  - schedule straddling the start (`2026-06-15`..`2026-07-20`), verify overlap clipped to `date_from`.
  - **no overlap** (`2026-08-01`..`2026-08-31`) → `period_budget == 0`, `expected_to_date == 0`,
    `n_projectable == 0`.
  - **open-ended** (`start_time` set, `stop_time=None`) → non-projectable, zeros.
  - **missing start** / **stop <= start** → non-projectable, zeros.
  - multiple entities aggregate (two schedules → summed).
- **`classify_pacing`** — lifetime-only projectable case: pass combined `period_budget > 0`,
  `active_daily_budget=0`, non-None `projected_spend` → returns over/under/on_track (regression that
  the relaxed guard admits it). Keep the existing lifetime-only-with-`period_budget=0` case →
  `budget_not_projectable`.
- **`pacing_report` end-to-end** — add a lifetime-only account whose schedule overlaps the window
  (via `_pc_camp("cX", lifetime=..., start_time="2026-07-01", stop_time="2026-07-31")`) and assert it
  gets a real over/under/on_track verdict, correct `period_budget` / `projected_spend`, and that it
  now appears in `status_counts` + shortlists + normalized totals (drops from
  `excluded_from_rollup`). Add a lifetime-only account with a **non-overlapping** schedule and assert
  it stays `budget_not_projectable`. Add a **mixed** account (daily + overlapping lifetime) and assert
  the combined `period_budget` = daily + prorated lifetime.
- **Byte-identical regression** — the existing
  `test_pacing_report_end_to_end_statuses_rollup_and_shortlists` (line 11745): its `act_lifetime`
  fixture (`_pc_camp("c5", lifetime="500000")`, no schedule) MUST still read
  `budget_not_projectable`, and the daily accounts' `period_budget`/`projected_spend`/`variance_pct`
  MUST be unchanged. Do not edit this test — it is the byte-identical guard. If it needs editing, the
  daily path was altered incorrectly.

## Acceptance

- A lifetime-only account with a schedule overlapping the reporting window returns
  over/under/on_track grounded in the prorated expectation, not `budget_not_projectable`.
- Daily-budget accounts are unaffected (byte-identical output — existing e2e test unedited & green).
- The "lifetime budgets are reported but NOT projected" caveat is updated in the `pacing_report` /
  `summarize_account_budget` docstrings and the `mcp_server.py` tool description.

## TODO

### Phase 1 — data surface
- Add `start_time` / `stop_time` to `PACING_CAMPAIGN_FIELDS` and `PACING_ADSET_FIELDS`.
- Extend `summarize_account_budget` to also return `lifetime_entities` (major-unit budget + raw
  schedule strings), respecting existing CBO precedence for which level owns the lifetime budget.

### Phase 2 — proration helper
- Add `_overlap_days` and the pure `lifetime_pacing` helper with the schedule parsing + per-entity
  proration + aggregation described above.

### Phase 3 — wire into the report + classification
- Relax the `classify_pacing` projectability guard (drop the `active_daily_budget <= 0` clause).
- In `pacing_report`, branch on `lifetime["period_budget"] > 0`: combined form for lifetime/mixed,
  literal existing form for daily-only (byte-identical). Feed combined figures to `classify_pacing`
  and the normalized-twin conversion. Add no new per-account keys.

### Phase 4 — docs + tests
- Update the four docstrings / description (summarize, classify, pacing_report, mcp_server).
- Add the unit + e2e tests above; extend `_pc_camp`/`_pc_adset` with schedule kwargs.
- Run `python -m pytest tests/test_meta_ads_analysis.py -k pacing 2>&1 | tee /tmp/pacing.log` and the
  full suite; confirm the byte-identical e2e test passes unedited.
