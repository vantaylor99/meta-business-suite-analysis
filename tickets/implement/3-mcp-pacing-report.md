description: Add a tool that tells you whether each ad account is on track to spend its budget for the month — which accounts are overspending, which are underspending, and the projected end-of-period total — across all the accounts someone oversees.
prereq: mcp-cross-account-batched-fanout
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: hard
----

## What to build

A sixth discovery tool, **`pacing_report`**, that answers "given how much each account has spent so
far this period and its configured budget, will it land over, under, or on target?" across every
account the token reaches (or an explicit `account_ids` list).

Unlike `account_benchmark` and `flag_accounts_needing_attention` — pure post-processors that need
**no new Meta read shape** — pacing genuinely needs a second data surface: the account's **budget
configuration** (campaign/adset daily & lifetime budgets, plus the account spend cap). Spend-to-date
comes from the existing insights read; budget config is a new per-account campaign+adset read. So
`pacing_report` is a **two-source join**, not a post-processor:

1. **Spend-to-date + FX + scope** — call `cross_account_performance` once over
   `[date_from, effective_as_of]`. This resolves scope, reads one account-insights row per account,
   and gives per-account native `spend` + `spend_normalized` + `currency` + `account_status_label`,
   plus the shared `fx_table`, `normalized_total`, and per-account `errors` — all inherited for free.
2. **Budget config** — a *second* `fan_out_accounts` over the accounts that read OK in step 1
   (`perf["accounts"]` ids), each reading `list_campaigns` + `list_adsets` (budget fields only) and
   computing the CBO-deduplicated **active daily-budget sum** for that account.
3. **Join + project + classify** by `ad_account_id`; compute `elapsed_fraction`, `projected_spend`,
   `status`, `variance_pct` per account; roll up to a scope view + worst-pacer shortlists.

It rides the same fan-out engine (determinism + per-account partial-failure isolation) and the same
FX table as the rest of the discovery suite.

### Why this is not a pure post-processor (and the read-cost consequence)

Budget config is not in the insights row. Step 2 issues **3 extra reads per readable account**
(`list_campaigns` + `list_adsets` + `get_account` for the spend cap) on top of
`cross_account_performance`'s `1 + N`. Total ≈ `1 + 4N` reads for an N-account scope. This is
documented and accepted (same posture as the attention tool's `2×` note); a single combined
per-account read is a future optimization, out of scope here.

## Interface

```python
def pacing_report(
    reader: MetaReaderProvider,
    *,
    date_from: str,              # full reporting period start (inclusive, YYYY-MM-DD)
    date_to: str,               # full reporting period end (inclusive) — e.g. the month
    account_ids: list[str] | None = None,
    as_of: str | None = None,   # spend measured THROUGH this date; None -> today (UTC). Injectable for tests.
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,   # test-only seam; NOT exposed to the LLM
) -> dict[str, Any]: ...
```

Per-account entry:

```json
{
  "ad_account_id": "act_123", "account_id": "123", "name": "…", "currency": "USD",
  "account_status_label": "ACTIVE",
  "spend_to_date": 4200.0, "spend_to_date_normalized": 4200.0,
  "period_budget": 9300.0, "period_budget_normalized": 9300.0,   // active daily sum × total_days
  "elapsed_fraction": 0.4516,                                     // 14/31
  "projected_spend": 9300.0, "projected_spend_normalized": 9300.0,
  "status": "on_track",                                           // see status enum below
  "variance_pct": 0.0,                                            // (projected - budget)/budget; fraction
  "active_daily_budget": 300.0,        // native major units, CBO-deduped, ACTIVE entities only
  "lifetime_budget_total": 0.0,        // native major units; reported, NOT projected (see below)
  "spend_cap": null,                   // native major units; lifetime account cap (0/absent -> uncapped)
  "amount_spent": 51000.0              // native major units; account LIFETIME spend (context for spend_cap)
}
```

Top level: `date_from`, `date_to`, `as_of` (the effective as-of used),
`reporting_currency`, `fx_as_of`, `fx_note`, `total_days`, `account_count`, `accounts`,
`rollup`, `errors`, and an optional `note`.

`rollup`:

```json
{
  "reporting_currency": "USD",
  "total_period_budget_normalized": 120000.0,     // projectable+FX accounts only
  "total_projected_normalized": 131000.0,
  "overall_variance_pct": 0.0917,
  "status_counts": {"over": 3, "under": 5, "on_track": 40, "no_budget_set": 2,
                    "budget_not_projectable": 4, "account_inactive": 1, "not_started": 0},
  "worst_over_pacers": [ {ad_account_id, name, variance_pct, projected_spend_normalized, period_budget_normalized}, … ],
  "worst_under_pacers": [ … ],
  "excluded_from_rollup": 7                        // accounts not in the over/under math + why counts
}
```

## Locked design decisions (resolved at plan stage — do NOT re-open in implement)

### 1. Dates & projection (the three-date problem)

`date_from..date_to` is the **full reporting period** (e.g. month start → month end). `as_of` is the
day spend is measured **through** (defaults to today). This separates "the period we're pacing
against" from "how far into it we are." Pure, clock-free math (a helper), with the single clock touch
being the `as_of=None` default:

- `effective_as_of = clamp(as_of, date_from - 1 day, date_to)`.
- `total_days = (date_to - date_from).days + 1`.
- `elapsed_days = clamp((effective_as_of - date_from).days + 1, 0, total_days)`.
- `elapsed_fraction = elapsed_days / total_days`.
- **Spend-to-date** read = `cross_account_performance(date_from, effective_as_of)`.
- `projected_spend = spend_to_date / elapsed_fraction` — a pure helper
  `project_spend(spend_to_date, elapsed_fraction) -> float | None` that returns `None` when
  `elapsed_fraction <= 0` (guards divide-by-zero → `not_started`).
- Completed period (`as_of >= date_to`): `effective_as_of = date_to`, `elapsed_fraction = 1.0`,
  `projected_spend == spend_to_date` (actuals).

### 2. Authoritative period budget (the double-counting rule)

**The period-budget denominator is the sum of ACTIVE daily budgets (CBO-deduplicated), converted
cents→major units, × `total_days`.** Account spend cap is a *lifetime* ceiling, not a period budget,
so it is **reported as context, never the denominator**.

**CBO-dedup precedence** — a pure helper `summarize_account_budget(campaigns, adsets) ->
{active_daily, lifetime_total}` (native minor units in, native major units out). Only
`effective_status == "ACTIVE"` entities count (a paused campaign/adset does not deliver). Per
**ACTIVE** campaign:

- campaign `daily_budget > 0` → **CBO daily**: add campaign daily to `active_daily`; **ignore its
  adsets** (their budgets are null under CBO; guard anyway — this is where naive summing
  double-counts).
- elif campaign `lifetime_budget > 0` → **CBO lifetime**: add to `lifetime_total`; ignore adsets.
- else (**non-CBO** campaign) → for each **ACTIVE** adset whose `campaign_id` == this campaign:
  adset `daily_budget > 0` → `active_daily`; elif adset `lifetime_budget > 0` → `lifetime_total`.

Adsets whose parent campaign is **not** ACTIVE are ignored (the parent gates delivery). This mirrors
`control.classify_adset_budget`'s adset-daily-first-else-campaign logic — reuse its shape, do not
re-derive a contradictory rule.

**Lifetime budgets are reported but NOT projected against the period.** A lifetime budget spans the
entity's own schedule, not an arbitrary reporting period; prorating it needs campaign
`start_time`/`stop_time` we don't read here. So an account whose only budget is lifetime is
`budget_not_projectable` (see status), with its `lifetime_budget_total` surfaced. Prorating lifetime
budgets via campaign schedule is a **backlog follow-up** (note it in the docstring).

### 3. Units: cents → major currency

Meta budget/cap fields (`daily_budget`, `lifetime_budget`, `spend_cap`, `amount_spent`) are in the
account currency's **minor unit**; insights `spend` is in **major** units. Convert with
`_minor_to_major(v) -> v / 100.0`. This is correct for 2-decimal currencies (USD/EUR/GBP/… — the vast
majority). **Zero-decimal currencies (JPY, KRW) and 3-decimal currencies are a KNOWN inaccuracy**
(off by 100×) — document as a limitation and file a backlog follow-up for a currency-aware minor-unit
divisor. Do NOT silently guess.

### 4. Per-account status enum (checked in this order)

1. `not_started` — global `elapsed_fraction <= 0` (as_of before period start). No projection.
2. `account_inactive` — `account_status_label != "ACTIVE"`. Excluded from over/under math (a paused
   account is not "under-pacing"); spend_to_date still reported.
3. `no_budget_set` — no active daily budget, no lifetime budget, no spend cap (uncapped/free
   delivery). Excluded from over/under math; reported explicitly.
4. `budget_not_projectable` — has a lifetime budget and/or spend cap but **zero** active daily budget
   (can't project against the period). Excluded from over/under math; `lifetime_budget_total` /
   `spend_cap` surfaced.
5. `over` / `under` / `on_track` — `variance_pct = (projected_spend - period_budget) / period_budget`;
   `over` if `> +tolerance`, `under` if `< -tolerance`, else `on_track`. Tolerance =
   `PACING_ON_TRACK_TOLERANCE_PCT` (default `0.05`).

`variance_pct` is a per-account ratio of two same-currency figures → **FX-invariant** (native and
normalized give the same value); compute it once from native.

### 5. Rollup & shortlists

- `total_period_budget_normalized` / `total_projected_normalized` sum **only** accounts that are
  projectable (`over`/`under`/`on_track`) **and** had an FX rate. `overall_variance_pct` from those
  totals.
- `status_counts` counts every account by status.
- `worst_over_pacers` = projectable accounts sorted by `variance_pct` desc; `worst_under_pacers` =
  sorted asc. Limit `PACING_SHORTLIST_LIMIT` (default `10`). Deterministic tiebreak: `ad_account_id`
  asc. Accounts with no FX (native-only) still get a per-account entry + verdict but are excluded from
  the *normalized* totals (their FX gap already surfaces in `errors` from step 1) — include them in
  the shortlists using native `variance_pct` (FX-invariant) but never in normalized sums.

### 6. Errors & reads

- Step-1 (insights) failures and no-FX accounts flow through `cross_account_performance`'s `errors`
  verbatim; merge into the top-level `errors`. An account that failed step 1 gets **no** step-2 budget
  read (it can't be paced) — do not double-report it.
- Step-2 (budget config) failures: `fan_out_accounts` isolates per-account `MetaApiError` → an
  `errors` entry tagged `{"stage": "budget", "ad_account_id", "error"}`. Such an account has
  spend_to_date but no budget → treat as `no_budget_set`-equivalent but with a distinct
  `status: "budget_unread"` so a read failure is never silently reported as "uncapped." Excluded from
  over/under math.
- Config constants live in `config.py` (`PACING_ON_TRACK_TOLERANCE_PCT`, `PACING_SHORTLIST_LIMIT`) —
  no magic numbers in the engine, matching the `ATTENTION_*` pattern.

## Edge cases & interactions

- **Period not started** (`as_of < date_from`) → every account `not_started`, no projection, no
  divide-by-zero. Top-level `note` explains.
- **Completed period** (`as_of >= date_to`) → `elapsed_fraction == 1`, projected == actual.
- **Uncapped account** (no daily, no lifetime, no cap) → `no_budget_set`, excluded from over/under,
  reported explicitly — never counted as under-pacing.
- **Lifetime-only / mixed** account → `budget_not_projectable`; `lifetime_budget_total` surfaced;
  documented precedence prevents CBO+adset double-count.
- **Paused/closed account** → `account_inactive`, excluded from under-pacing, clearly marked.
- **No FX for an account's currency** → native figures kept, excluded from normalized rollup totals,
  FX gap in `errors` (inherited); still gets a native `variance_pct` + shortlist eligibility.
- **Budget read fails but insights succeeded** → `status: "budget_unread"`, in `errors` tagged
  `stage=budget`, excluded from over/under — distinct from a genuinely uncapped account.
- **Insights read fails** → account absent from `accounts`, single `errors` entry (step 1), no budget
  read attempted.
- **Currency discipline** → budget and spend compared in the **same** (native) currency per account;
  only the rollup uses normalized figures. Never compare a native budget to a normalized spend.
- **Zero-decimal currency (JPY/KRW)** → known 100× units inaccuracy; documented + backlog follow-up.
- **Determinism** → main-thread assembly iterates scope order; all sorts carry an `ad_account_id`
  tiebreak; `as_of=None` (today) is the only clock touch and tests always pass an explicit `as_of`.
- **`reporting_currency` absent from FX table** → whole-call `ValueError` (inherited from
  `cross_account_performance`), same contract as the rest of the suite.
- **`date_from > date_to`** → `ValueError` before any read.
- **CBO campaign with lingering adset budgets** (data anomaly) → CBO branch wins; adset budgets under
  a CBO campaign are ignored, never added — the double-count guard.

## TODO

### Phase 1 — config + pure helpers (fully unit-testable, no reader)

- Add `PACING_ON_TRACK_TOLERANCE_PCT = 0.05` and `PACING_SHORTLIST_LIMIT = 10` to `config.py`
  (alongside the `ATTENTION_*` block, with a comment).
- `pacing_period(date_from, date_to, as_of) -> {total_days, elapsed_days, elapsed_fraction,
  effective_as_of}` — pure, clock-free (takes an explicit `as_of` string); clamps as specified;
  raises `ValueError` on `date_from > date_to` / unparseable dates.
- `project_spend(spend_to_date, elapsed_fraction) -> float | None` — `None` when
  `elapsed_fraction <= 0`.
- `_minor_to_major(value) -> float | None` — `v/100.0`; `None`/blank → `None`.
- `summarize_account_budget(campaigns, adsets) -> {"active_daily": float, "lifetime_total": float}` —
  the CBO-dedup precedence over ACTIVE entities (major units out). This is the core correctness unit.
- `classify_pacing(status_inputs…) -> {status, variance_pct|None}` — the status enum + variance,
  checked in the documented order.

### Phase 2 — orchestration

- `pacing_report(...)`: load FX table once; `perf = cross_account_performance(date_from,
  effective_as_of, account_ids, reporting_currency, fx_table=table)`; budget `fan_out_accounts` over
  `perf["accounts"]` ids where each worker reads `list_campaigns(PACING_CAMPAIGN_FIELDS)` +
  `list_adsets(PACING_ADSET_FIELDS)` **and** `get_account(ad_account_id, fields=["currency",
  "spend_cap","amount_spent"])`. NOTE: the `perf["accounts"]` rows do **not** carry `spend_cap` or
  `amount_spent` (the row built at `account_discovery.py:633-640` propagates only
  `account_id/name/currency/account_status[_label]`, and `DEFAULT_AD_ACCOUNT_FIELDS` has
  `amount_spent` but not `spend_cap`) — so the budget worker must fetch the cap fields itself via this
  dedicated `get_account`; do not bloat the shared `DEFAULT_AD_ACCOUNT_FIELDS`. Getting all three
  reads in one worker keeps the fan-out at 3 reads/account and isolates their failures together.
- Define module field lists `PACING_CAMPAIGN_FIELDS = ["id","effective_status","daily_budget",
  "lifetime_budget"]` and `PACING_ADSET_FIELDS = ["id","campaign_id","effective_status",
  "daily_budget","lifetime_budget"]` (budget-only — do not reuse the heavier `control.*_FIELDS` with
  targeting/objective).
- Join by `ad_account_id`; compute per-account entry; build `rollup`; merge `errors` (step-1 verbatim
  + step-2 budget-tagged).
- Docstring: state the two-source join, the `1+4N` read cost, the CBO-dedup precedence, the
  lifetime-not-projected decision, the cents→major units caveat, and that budget pacing lives here
  (cross-reference the attention tool's `NOTE`).

### Phase 3 — MCP wiring

- Add `pacing_report` to `DISCOVERY_TOOL_DESCRIPTIONS` and `build_discovery_tools` in `mcp_server.py`
  (expose `date_from, date_to, account_ids, as_of, reporting_currency`; keep `fx_table` as a
  test-only seam, not exposed). Add to the returned dict → the server auto-registers it (loop at
  `mcp_server.py:1060`).

### Phase 4 — docs

- `README.md` + `docs/META_API_SETUP.md`: add `pacing_report` to the discovery-tool list (now six
  tools), describe spend-to-date vs. budget, the CBO-dedup rule, the lifetime-budget limitation, the
  cents→major (2-decimal) caveat, and the `1+4N` read cost.

### Phase 5 — tests (`tests/test_meta_ads_analysis.py`)

Pure helpers:
- `pacing_period`: mid-period (14/31 → `elapsed_fraction ≈ 0.4516`), not-started (`as_of < from` →
  0), completed (`as_of >= to` → 1.0), single-day period, `from > to` → `ValueError`.
- `project_spend`: normal, `elapsed_fraction == 0` → `None`.
- `summarize_account_budget`: **CBO daily** (campaign daily set, adsets have budgets → adsets ignored,
  no double-count); **CBO lifetime** (→ lifetime_total, not active_daily); **non-CBO** (adset daily
  summed); **paused campaign** (its adsets ignored even if ACTIVE); **paused adset under active
  campaign** (ignored); mixed account (one CBO campaign + one non-CBO campaign).
- `classify_pacing`: over / under / on_track at tolerance boundary; `no_budget_set`;
  `budget_not_projectable` (lifetime-only); `account_inactive`.

Integration (`FakeMetaReader` with per-account callable stubs for `list_campaigns`/`list_adsets`):
- End-to-end multi-account: one on_track, one over, one under, one uncapped (`no_budget_set`), one
  lifetime-only (`budget_not_projectable`), one paused (`account_inactive`) → assert statuses,
  `rollup.status_counts`, `worst_over_pacers`/`worst_under_pacers` ordering + tiebreak.
- No-FX account: kept native, in shortlists via native variance, excluded from
  `total_*_normalized`, FX gap in `errors`.
- Budget-read failure (step-2 `MetaApiError` for one account) → `status: "budget_unread"`, tagged
  `errors` entry, excluded from over/under, and NOT double-reported.
- Insights-read failure (step-1) for one account → absent from `accounts`, single step-1 `errors`
  entry, no budget read attempted for it.
- Determinism: reversed worker finish order yields byte-identical output (mirror the existing
  reorder test for `cross_account_performance`).
- `reporting_currency` with no FX rate → `ValueError` (whole call).

### Validation

- `.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py`
- `.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/pacing_tests.log` (stream, never silent
  redirect). No `ruff`/`mypy` configured — `py_compile` is the lint per prior tickets.
