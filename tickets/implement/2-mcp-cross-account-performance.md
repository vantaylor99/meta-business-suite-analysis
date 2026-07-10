description: A cross-account report that shows efficiency, not just raw totals — cost per click, cost per lead, click-through rate, ROAS — and lets you compare accounts that bill in different currencies by converting everything to one reporting currency.
prereq: mcp-cross-account-batched-fanout
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/currency.py, config/fx_rates.json, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## Summary

Add a new discovery-surface tool `cross_account_performance` that succeeds
`cross_account_spend_summary`. Where the summary returns only raw additive totals grouped by
currency, this returns **per-account efficiency metrics** (CPM, CPC, CTR, cost-per-result, ROAS)
correctly recomputed from summed base metrics, plus **currency normalization** to a chosen reporting
currency (default USD) driven by a **static FX table checked into `config/`**. It is the read that
the ranking (`mcp-rank-accounts`), attention-triage (`mcp-flag-accounts-attention`), and benchmark
(`mcp-account-benchmark`) tools consume.

It rides the existing fan-out engine — `resolve_scope` → `fan_out_accounts` → main-thread assembly —
so it inherits determinism (output identical regardless of worker completion order) and per-account
partial-failure isolation for free (see `account_discovery.py:171-279`, and the completed prereq
`mcp-cross-account-batched-fanout`).

## Design decisions (RESOLVED — no open questions for the implementer)

### 1. Module placement

- **New module `src/meta_ads_analysis/currency.py`** for FX: loading the static rate table,
  surfacing its `as_of`, and converting a native money amount into a reporting currency. FX is a
  distinct concern with its own config file and is independently unit-testable, so it does **not**
  belong bolted onto `account_discovery.py`.
- **`normalize.py` is deliberately NOT used** (the original plan ticket's `files:` hint named it).
  That module is exclusively CSV-export ingestion (header aliases, blob parsing) and shares nothing
  with live-insights FX normalization; adding currency logic there would couple two unrelated
  concerns. Decision: use a dedicated `currency.py` instead. This is the one deviation from the plan
  ticket's file hint and is intentional.
- **Derived-metric computation** lives as a pure helper `compute_derived_metrics(...)` in
  `account_discovery.py`, next to the tool that uses it. Pure, reader-free, unit-testable.
- **The tool `cross_account_performance`** lives in `account_discovery.py` alongside
  `cross_account_spend_summary`, and is wired into `mcp_server.build_discovery_tools`
  (`mcp_server.py:440-465`) with an entry in `DISCOVERY_TOOL_DESCRIPTIONS`
  (`mcp_server.py:426-437`).

### 2. FX rate table — `config/fx_rates.json` (committed, static, no network)

FX source is a static table checked into the repo — **decided by the product owner; live/Meta FX was
explicitly deferred, do not build it.** No network calls, so mock and unattended runs stay
deterministic.

**This file MUST be committed** (unlike `config/meta_ads_accounts.json`, which is gitignored). Verify
`.gitignore` does not exclude it (today it only ignores `config/meta_ads_accounts.json`, so
`config/fx_rates.json` is committable as-is — do not add an ignore rule).

Schema:

```json
{
  "as_of": "2026-07-01",
  "base": "USD",
  "note": "Approximate static reference rates for cross-account normalization. NOT live FX — do not use for billing or precise financial reporting.",
  "rates": {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "BRL": 0.18,
    "MXN": 0.055,
    "CAD": 0.73,
    "AUD": 0.66
  }
}
```

- `rates` maps **currency code → multiplier that converts one native unit into `base` (USD)**:
  `usd_amount = native_amount * rates[native_currency]`.
- To convert into an arbitrary `reporting_currency`:
  `amount_reporting = native_amount * rates[native] / rates[reporting]`.
  (Both `native` and `reporting` must be present in the table.)
- `as_of` is **required**; loading a table without it is a `ValueError`. The tool surfaces `as_of`
  (as `fx_as_of`) and the `note` (as `fx_note`) in its output so no consumer mistakes these for live
  rates.
- Seed the committed table with USD, EUR, GBP, BRL, MXN, CAD, AUD at minimum (the ticket use cases
  name MXN/BRL/EUR). Rates are approximate and clearly labelled.

### 3. `currency.py` interface

```python
DEFAULT_FX_TABLE_PATH: Path            # config/fx_rates.json, resolved via meta_ads_analysis.config

@dataclass(frozen=True)
class FxTable:
    as_of: str
    base: str
    note: str | None
    rates: dict[str, float]            # currency -> rate-to-base

    def convert(self, amount: float, *, from_currency: str, to_currency: str) -> float | None:
        """native amount -> reporting currency, or None if EITHER currency is absent
        from the table (never guess, never silently pass through unlike currencies)."""

    def has(self, currency: str) -> bool: ...

def load_fx_table(path: Path | None = None) -> FxTable:
    """Load + validate config/fx_rates.json. Missing file, missing 'as_of', missing/empty
    'rates', or a non-numeric rate -> ValueError with an actionable message. Currency codes
    are upper-cased on load so lookups are case-insensitive."""
```

- `convert` returns `None` (not a raise) when a currency is missing, so the fan-out assembly can
  route that account's normalized fields to *absent* + record an `errors` entry while still returning
  native figures. A zero or negative rate in the table is a load-time `ValueError` (bad data),
  distinct from a currency simply being absent.
- Injectable for tests: `cross_account_performance(..., fx_table=<FxTable>)` accepts a preloaded
  table; when `None`, it loads `DEFAULT_FX_TABLE_PATH` **once** before the fan-out (not per worker).

### 4. `compute_derived_metrics` — the pure Simpson's-paradox-safe helper

```python
def compute_derived_metrics(base: dict[str, float | int | None]) -> dict[str, float]:
    """Given summed/native base metrics, recompute ratio metrics from the components.
    NEVER averages a ratio across accounts. Returns only the metrics that are defined
    (divide-by-zero / missing component -> key ABSENT, never inf/NaN/0-fill)."""
```

Inputs consumed: `spend`, `impressions`, `clicks`, `results`, `purchase_value` (any may be
missing/None). Outputs, each present only when its denominator is non-zero and the needed component
is present:

| metric            | formula                              | unit               |
|-------------------|--------------------------------------|--------------------|
| `cpm`             | `spend / impressions * 1000`         | money (native)     |
| `cpc`             | `spend / clicks`                     | money (native)     |
| `ctr`             | `clicks / impressions * 100`         | percentage (ratio) |
| `cost_per_result` | `spend / results`                    | money (native)     |
| `roas`            | `purchase_value / spend`             | ratio              |

- Guard EVERY divide: `impressions == 0` → no `cpm`/`ctr`; `clicks == 0` → no `cpc`;
  `results` missing/0 → no `cost_per_result`; `spend == 0` or `purchase_value` missing → no `roas`.
  Emit the metric as **absent**, never `inf`/`NaN`/`0`.
- `ctr` and `roas` are **currency-invariant ratios** — they have NO `*_normalized` twin. Only money
  metrics (`spend`, `cpm`, `cpc`, `cost_per_result`, `purchase_value`) get normalized twins.
- The same helper computes per-account derived metrics (from that account's native base), per-currency
  subtotal derived metrics (from the currency group's summed base), and the normalized-total derived
  metrics (from the summed normalized base). This is the single point that guarantees ratios are
  always recomputed from summed components, never averaged.

### 5. Result / conversion sourcing

Fetch fields per account: `["spend", "impressions", "clicks", "actions", "action_values"]`
(account-level, `time_increment="all_days"` — one aggregated row, same shape
`cross_account_spend_summary` already uses).

- **Result count** = `_find_metric(actions_blob, [primary_result_key])` where `primary_result_key`
  is resolved as: the account's configured `primary_result_action_type` **if the account is in the
  registry**, else inferred via `sync_api._infer_primary_result_action(actions)`. If neither yields a
  key → `results` is **absent** for that account (and therefore `cost_per_result` is absent too — NOT
  zero-filled into a misleading ratio).
- **Result label** = registry `primary_result_label` or `sync_api._label_for_action(key)`; carried
  as `result_label` on the row when a key was resolved.
- **Revenue** = `purchase_value = _find_metric(action_values_blob, PURCHASE_KEYS)`; drives `roas`.
  Absent revenue → no `purchase_value`, no `roas` for that account.
- Reuse the existing shared helpers — do **not** re-implement: `sync_api._metric_blob_list`,
  `sync_api._find_metric`, `sync_api._infer_primary_result_action`, `sync_api._label_for_action`,
  `sync_api.PURCHASE_KEYS`, `sync_api._number` (`sync_api.py:83-94,303-320,409-454,532`).
  `control.fetch_entity_metrics` (`control.py:1003-1046`) is the sibling live path that reads the same
  `actions`/`action_values` fields — mirror its extraction, not a new dialect.

### 6. Registry consult (graceful, config-optional)

Reads are open to every reachable account, but the config registry (`config/meta_ads_accounts.json`)
is gitignored and **absent in mock/unattended runs**. So consulting it for
`primary_result_action_type` must be best-effort:

- Build a `{ad_account_id: MetaAdsAccount}` map once (reverse of `load_account_registry`, which keys
  by slug — `account_registry.py:32-106`). Wrap in try/except `(FileNotFoundError, ValueError)` →
  empty map when no/invalid config. Never let a missing config break the tool.
- When an account is not in the map, fall back to inference from its own `actions` blob. This keeps
  the mock path (no config, `act_mock001`) fully working and deterministic.

### 7. Tool interface

```python
def cross_account_performance(
    reader: MetaReaderProvider,
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    level: str = "account",
    fx_table: FxTable | None = None,   # injected in tests; else loaded from config once
) -> dict[str, Any]:
```

`level` is accepted per the plan interface but for this ticket only `"account"` is implemented;
validate and raise `ValueError` for any other value (a documented, honest limitation — a future
ticket can add campaign/adset roll-ups; do not stub other levels).

MCP wrapper in `build_discovery_tools` exposes `date_from, date_to, account_ids=None,
reporting_currency="USD", level="account"` (do **not** expose `fx_table` to the LLM — internal seam).

### 8. Output shape

```jsonc
{
  "date_from": "2026-06-01",
  "date_to": "2026-06-30",
  "level": "account",
  "reporting_currency": "USD",
  "fx_as_of": "2026-07-01",
  "fx_note": "Approximate static reference rates ... NOT live FX ...",
  "account_count": 3,          // attempted (== resolved scope size)
  "reachable_count": 3,
  "accounts": [
    {
      "ad_account_id": "act_123", "account_id": "123", "name": "Reno Mission",
      "currency": "MXN", "account_status": 1, "account_status_label": "ACTIVE",
      // native base metrics (present only when Meta returned them)
      "spend": 1000.0, "impressions": 50000, "clicks": 800,
      "results": 40, "result_label": "Leads", "purchase_value": 5000.0,
      // native derived metrics (absent when undefined)
      "cpm": 20.0, "cpc": 1.25, "ctr": 1.6, "cost_per_result": 25.0, "roas": 5.0,
      // normalized money twins in reporting currency (absent if currency not in FX table)
      "spend_normalized": 55.0, "cpm_normalized": 1.1, "cpc_normalized": 0.069,
      "cost_per_result_normalized": 1.375, "purchase_value_normalized": 275.0
    }
  ],
  "totals_by_currency": {
    "MXN": { "spend": 1000.0, "impressions": 50000, "clicks": 800, "results": 40,
             "purchase_value": 5000.0, "account_count": 1,
             "cpm": 20.0, "cpc": 1.25, "ctr": 1.6, "cost_per_result": 25.0, "roas": 5.0 }
  },
  "normalized_total": {           // only meaningful in reporting_currency; excludes no-FX accounts
    "reporting_currency": "USD",
    "spend": 55.0, "impressions": 50000, "clicks": 800, "results": 40,
    "purchase_value": 275.0, "account_count": 1, "excluded_no_fx": 0,
    "cpm": 1.1, "cpc": 0.069, "ctr": 1.6, "cost_per_result": 1.375, "roas": 5.0
  },
  "errors": [ { "ad_account_id": "act_x", "error": "..." } ]
}
```

- Per-account row: a base metric Meta left blank/absent is **omitted** from the row (mirror
  `cross_account_spend_summary`'s `if raw_metric not in (None, "")` rule at
  `account_discovery.py:366-374`). A derived metric that is undefined is omitted. A normalized twin
  is omitted when the account's currency is not in the FX table.
- **No-FX account**: native figures + native derived metrics still returned; normalized twins absent;
  one `errors` entry recorded, e.g. `{"ad_account_id": ..., "error": "no FX rate for currency 'XYZ' (as_of 2026-07-01)"}`; the account is **excluded from `normalized_total`** and counted in
  `normalized_total.excluded_no_fx`. It still appears in its native `totals_by_currency` group.
- **`totals_by_currency`**: sum base metrics per currency (as today), then recompute the five derived
  metrics from the summed base via `compute_derived_metrics`. All accounts in a group share a
  currency, so the native derived subtotal is meaningful.
- **`normalized_total`**: sum `spend_normalized` + `purchase_value_normalized` (money, converted) and
  `impressions`/`clicks`/`results` (counts, currency-invariant) across accounts that HAD an FX rate,
  then recompute derived metrics in the reporting currency. Empty (all excluded / no accounts) →
  present with zeroed base + no derived keys + `excluded_no_fx` reflecting the count.
- `note = "no accounts reachable"` when `requested_all and not account_ids` (mirror existing tool).

## Edge cases & interactions (write a test for each)

- **Divide-by-zero guards**: zero impressions → no `cpm`/`ctr`; zero clicks → no `cpc`; zero/absent
  results → no `cost_per_result`; zero spend or absent revenue → no `roas`. Assert the key is ABSENT,
  not `inf`/`NaN`/`0`.
- **Simpson's paradox**: two accounts, same currency, with different spend/click ratios — assert the
  `totals_by_currency` `cpc` equals `sum(spend)/sum(clicks)`, NOT the mean of the two per-account
  `cpc`s. This is the central correctness test.
- **Currency normalization correctness**: an MXN account and a EUR account both normalize to USD;
  assert `spend_normalized == spend * rate[cur]` and that `normalized_total.spend` is the sum of the
  two normalized spends (not a raw sum of unlike native spends).
- **Currency absent from FX table**: account in an unlisted currency → normalized twins absent, native
  figures present, `errors` entry recorded, excluded from `normalized_total` (and counted in
  `excluded_no_fx`), still present in its native `totals_by_currency` group.
- **`reporting_currency` other than USD** (e.g. `"EUR"`): assert conversion uses `rate[native]/rate[EUR]`
  and `fx_as_of` is surfaced; a `reporting_currency` absent from the table → whole-call `ValueError`
  (can't normalize anything).
- **Missing / partial action data**: account with no resolvable primary result key → `results`,
  `result_label`, `cost_per_result` all absent for that account only; other accounts unaffected.
- **Registry consult with NO config file** (mock/unattended): tool still works, results inferred from
  the `actions` blob; no exception from the missing `config/meta_ads_accounts.json`.
- **Registry consult WITH config**: an account whose configured `primary_result_action_type` differs
  from what naive inference would pick → results reflect the configured optimization event.
- **Per-account partial failure & determinism** (inherited): one account's `MetaApiError` → `errors`,
  others succeed; a non-`MetaApiError` propagates and fails the whole call; output identical
  regardless of worker completion order (reuse the reordering-style test from the prereq).
- **Discovery-path whole-call failure**: `account_ids=None` and discovery raises `MetaApiError` →
  propagates (whole-call), distinct from a per-account failure.
- **Empty reach**: `account_ids=None`, discovery returns `[]` → `note="no accounts reachable"`,
  empty `accounts`/`totals_by_currency`, `normalized_total` present-but-empty.
- **FX table load failures**: missing file, missing `as_of`, empty `rates`, non-numeric/zero/negative
  rate → `load_fx_table` raises `ValueError` with an actionable message (unit-test each).
- **Mock path stays live-call-free & deterministic**: the `--mock` reader (`act_mock001`, USD, canned
  `MOCK_INSIGHT`) drives `cross_account_performance` with the real committed FX table (USD present) →
  a stable USD row with `spend_normalized == spend`. No config file, no network.

## TODO

### Phase 1 — FX + derived-metric primitives (pure, no reader)

- Add `config/fx_rates.json` (committed) per the schema in §2; seed USD/EUR/GBP/BRL/MXN/CAD/AUD with
  `as_of` and the `note`. Confirm `.gitignore` does not exclude it.
- Create `src/meta_ads_analysis/currency.py`: `FxTable` dataclass, `load_fx_table`, `convert`, `has`,
  `DEFAULT_FX_TABLE_PATH` (resolve via `meta_ads_analysis.config` like `DEFAULT_ACCOUNTS_CONFIG_PATH`).
  Validate `as_of`/`rates`; upper-case currency codes on load.
- Add `compute_derived_metrics(base)` to `account_discovery.py` per §4 (divide-by-zero-safe; absent,
  never inf/NaN).
- Unit tests for `load_fx_table` (happy + each failure mode), `FxTable.convert` (present/absent/other
  reporting currency), and `compute_derived_metrics` (every guard + the "recompute not average" case).

### Phase 2 — the tool + wiring + docs

- Add a graceful `_registry_by_ad_account_id()` helper (best-effort; empty on missing/invalid config).
- Add `_resolve_result_key(ad_account_id, actions, registry_by_id)` using config then inference.
- Implement `cross_account_performance(...)` in `account_discovery.py`: `resolve_scope` →
  `fan_out_accounts` (fields `spend, impressions, clicks, actions, action_values`) → main-thread
  assembly of per-account rows (native base + derived + normalized twins), `totals_by_currency`
  (summed base + recomputed derived), `normalized_total` (summed normalized base + recomputed derived,
  excluding no-FX accounts), `errors`, and `fx_as_of`/`fx_note`. Validate `level == "account"` and a
  resolvable `reporting_currency`.
- Wire into `mcp_server.build_discovery_tools` + add `DISCOVERY_TOOL_DESCRIPTIONS` entry.
- Tests for every case in "Edge cases & interactions".
- Docs: add the tool to `README.md` (discovery/cross-account section) and `docs/META_API_SETUP.md`,
  noting: efficiency metrics recomputed from components; `reporting_currency` default USD; the static
  FX table in `config/fx_rates.json` with its `as_of` and the explicit "approximate, not live"
  caveat; and that live/Meta FX is deferred.

### Validate

- `.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/perf.log` (stream, don't silently redirect).
  Focused run first: `-k "cross_account or fx or currency or derived or performance"`.
- `.venv/bin/python -m py_compile` the new/changed modules. (`ruff`/`mypy` are not installed in this
  repo — validate via `py_compile` + pytest, matching the prereq's approach.)
