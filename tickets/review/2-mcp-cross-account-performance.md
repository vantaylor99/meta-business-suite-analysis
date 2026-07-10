description: A new cross-account report that shows efficiency (cost per click, cost per lead, ROAS, etc.) instead of just raw spend, and converts every account to one reporting currency so accounts that bill in different currencies can be compared side by side.
files: src/meta_ads_analysis/currency.py, config/fx_rates.json, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## What landed

A new discovery-surface tool **`cross_account_performance`** alongside `cross_account_spend_summary`.
Where the summary returns only raw additive totals grouped by currency, this returns **per-account
efficiency metrics** (`cpm`, `cpc`, `ctr`, `cost_per_result`, `roas`) recomputed from summed base
components, plus **currency normalization** of the money metrics into a chosen `reporting_currency`
(default USD) driven by a **static FX table committed at `config/fx_rates.json`**.

It rides the existing fan-out engine (`resolve_scope` → `fan_out_accounts` → main-thread assembly), so
it inherits determinism (output identical regardless of worker completion order) and per-account
partial-failure isolation.

### New / changed code

- **`config/fx_rates.json`** (new, committed — verified NOT gitignored). Schema: `as_of` (required),
  `base`, `note`, `rates` (currency → multiplier-to-USD). Seeded USD/EUR/GBP/BRL/MXN/CAD/AUD.
- **`src/meta_ads_analysis/currency.py`** (new). `FxTable` dataclass + `load_fx_table()` +
  `DEFAULT_FX_TABLE_PATH`. `convert()` returns `None` when *either* currency is absent (never guesses);
  `load_fx_table` raises `ValueError` on missing file / missing `as_of` / empty `rates` / non-numeric
  / zero-or-negative rate. Codes upper-cased on load (case-insensitive lookups).
  - **Deliberate deviation from the plan ticket's file hint:** used a dedicated `currency.py`, NOT
    `normalize.py` (that module is CSV-export ingestion and shares nothing with live-insights FX).
    This was pre-approved in the ticket §1.
- **`account_discovery.py`** (changed): added pure helpers `compute_derived_metrics(base)` (the single
  Simpson's-paradox-safe recompute point — used for per-account, per-currency subtotal, and normalized
  total), `_as_count`, `_registry_by_ad_account_id` (best-effort; `{}` on missing/invalid config),
  `_resolve_result_key` (config `primary_result_action_type` first, else inference), and the tool
  `cross_account_performance(...)`. Reuses `sync_api` helpers (`_number`, `_find_metric`,
  `_metric_blob_list`, `_infer_primary_result_action`, `_label_for_action`, `PURCHASE_KEYS`) — no new
  extraction dialect.
- **`mcp_server.py`** (changed): added the `DISCOVERY_TOOL_DESCRIPTIONS` entry and the
  `build_discovery_tools` wrapper (exposes `date_from, date_to, account_ids=None,
  reporting_currency="USD", level="account"`; `fx_table` is a test-only seam, deliberately NOT exposed
  to the LLM).

## How to validate

- Full suite: `.venv/bin/python -m pytest tests/ -q` → **519 passed** (36 new).
- Focused: `.venv/bin/python -m pytest tests/ -q -k "cross_account_performance or fx or currency or derived"`.
- Compile: `.venv/bin/python -m py_compile src/meta_ads_analysis/{currency,account_discovery,mcp_server}.py`.
- (`ruff`/`mypy` are not installed in this repo — validation is `py_compile` + pytest, matching the
  prereq.)

## Use cases covered by tests (the floor — treat as a starting point, not a ceiling)

- **Simpson's paradox (central correctness):** two USD accounts with different spend/click ratios —
  the `totals_by_currency` `cpc` equals `sum(spend)/sum(clicks)`, NOT the mean of per-account cpcs.
- **Currency normalization:** MXN + EUR → USD; `spend_normalized == spend * rate[cur]` and
  `normalized_total.spend` is the sum of normalized spends. `cpm_normalized` recomputed from the
  normalized base. `ctr`/`roas` have no `*_normalized` twin (currency-invariant).
- **`reporting_currency` other than USD** (EUR): conversion uses `rate[native]/rate[EUR]`; `fx_as_of`
  surfaced. A `reporting_currency` absent from the table → whole-call `ValueError`.
- **Currency absent from FX table** (JPY): native figures + native derived retained; normalized twins
  absent; `errors` entry recorded; excluded from `normalized_total` (counted in `excluded_no_fx`);
  still present in its native `totals_by_currency` group.
- **Divide-by-zero guards** (unit-level on `compute_derived_metrics`): zero impressions → no cpm/ctr;
  zero clicks → no cpc; absent/zero results → no cost_per_result; zero spend / absent revenue → no
  roas. Asserted **absent**, never inf/NaN/0. `math.isfinite` sweep.
- **Result-key resolution:** inferred from `actions` when no config; **configured
  `primary_result_action_type` overrides naive inference** when present; no resolvable key → results /
  result_label / cost_per_result all absent for that account only.
- **Registry graceful-degradation:** `_registry_by_ad_account_id()` → `{}` when config missing;
  tool works end-to-end with no config file.
- **Fan-out inheritance:** per-account `MetaApiError` → `errors`, others succeed, survivors in scope
  order under reordering delays; non-`MetaApiError` propagates (whole-call); discovery-path
  `MetaApiError` propagates; empty reach → `note="no accounts reachable"` + present-but-empty
  `normalized_total`; explicit ids use `get_account` and skip discovery.
- **FX load failures:** missing file / missing `as_of` / empty `rates` / non-numeric (incl. `bool`) /
  zero / negative rate each raise `ValueError`.
- **Mock path:** `build_mock_reader()` (act_mock001, USD) + the real committed FX table → stable USD
  row with `spend_normalized == spend`, no config, no network.

## Known gaps / things for the reviewer to poke at (tests are a floor)

- **`level` only supports `"account"`.** Any other value raises `ValueError` — campaign/adset
  roll-ups are an honest, documented deferral (a future ticket), NOT stubbed.
- **Static FX only.** Live/Meta FX was explicitly deferred by the product owner. The table is a
  committed *approximate* reference; `fx_as_of` + `fx_note` are surfaced so no consumer mistakes it
  for billing-grade rates. BRL/CAD/AUD are seeded but only USD/EUR/GBP/MXN/JPY are exercised in tests.
- **No rounding applied** to any output value (unlike `control.fetch_entity_metrics`, which rounds to
  2 dp). I matched the ticket's example (e.g. `cpc_normalized 0.069` unrounded) and left rounding to
  the consumer/display layer. Worth a second opinion on whether the tool should round.
- **`normalized_total` contributor-tracking with mixed accounts** (some report results/revenue, some
  don't) is covered indirectly (the no-FX and empty-reach paths exercise the contributor counters) but
  there is no single end-to-end test asserting a *partial*-contributor `normalized_total` (e.g. one of
  three accounts has revenue). Reasonable next test to add.
- **Live registry consult on a real machine:** the tool consults the *real* gitignored
  `config/meta_ads_accounts.json` when run for real (tests monkeypatch it for hermeticity). This is by
  design (config-optional, best-effort), but note that on this dev box the file exists, so a live run
  would honor configured `primary_result_action_type` values.
