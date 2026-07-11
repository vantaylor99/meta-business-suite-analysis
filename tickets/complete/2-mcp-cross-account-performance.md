description: A new cross-account report that shows efficiency (cost per click, cost per lead, ROAS, etc.) instead of just raw spend, and converts every account to one reporting currency so accounts that bill in different currencies can be compared side by side.
files: src/meta_ads_analysis/currency.py, config/fx_rates.json, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## What shipped

A third discovery-surface tool **`cross_account_performance`** alongside `list_ad_accounts` and
`cross_account_spend_summary`. Where the summary returns only raw additive totals grouped by
currency, this returns **per-account efficiency metrics** (`cpm`, `cpc`, `ctr`, `cost_per_result`,
`roas`) recomputed from summed base components (Simpson's-paradox-safe — never an averaged ratio),
plus **currency normalization** of money metrics into a chosen `reporting_currency` (default USD)
driven by a **static FX table committed at `config/fx_rates.json`**. It rides the existing fan-out
engine (`resolve_scope` → `fan_out_accounts` → main-thread assembly), inheriting determinism and
per-account partial-failure isolation.

### Code
- `config/fx_rates.json` (new, committed — confirmed NOT gitignored).
- `src/meta_ads_analysis/currency.py` (new): `FxTable` + `load_fx_table()` + `DEFAULT_FX_TABLE_PATH`.
- `src/meta_ads_analysis/account_discovery.py`: `compute_derived_metrics`, `_as_count`,
  `_registry_by_ad_account_id`, `_resolve_result_key`, `cross_account_performance`, and the two
  finalizers.
- `src/meta_ads_analysis/mcp_server.py`: `DISCOVERY_TOOL_DESCRIPTIONS` entry + `build_discovery_tools`
  wrapper (`fx_table` is a test-only seam, not exposed to the LLM).
- Docs: `README.md`, `docs/META_API_SETUP.md` updated (two → three discovery tools).

## Review findings

Reviewed the full implement diff (`76a5710`) with fresh eyes before the handoff summary, then
scrutinized against SPP/DRY/modularity/scalability/maintainability/performance/resource-cleanup/
error-handling/type-safety. Lint tooling (`ruff`/`mypy`) is not installed in this repo — validation
is `py_compile` + pytest, matching the prereq.

**Verified correct — no change needed:**
- **Simpson's-paradox safety (central correctness):** aggregate ratios recompute from summed
  numerator/denominator via the single `compute_derived_metrics` point (per-account, per-currency
  subtotal, and normalized total all route through it). Confirmed by test + by reading.
- **Divide-by-zero / absent-vs-zero discipline:** every ratio with a zero/absent denominator is
  *omitted*, never `inf`/`NaN`/`0`. `_number` distinguishes Meta-blank (`None`) from a real `0`.
- **Currency normalization math:** `amount * rate[from] / rate[to]`; money metrics get `*_normalized`
  twins, `ctr`/`roas` correctly do not (currency-invariant ratios). `convert()` returns `None` (never
  a guess) when either currency is absent.
- **FX load validation:** missing file / missing `as_of` / empty `rates` / non-numeric (incl. `bool`,
  correctly rejected before the `int` subclass trap) / zero / negative all raise `ValueError`.
- **Fan-out inheritance:** input-order determinism under reordering delays, per-account `MetaApiError`
  isolated to `errors`, non-`MetaApiError` and discovery-path failures propagate whole-call.
- **Config-optional graceful degradation:** `_registry_by_ad_account_id()` → `{}` on missing/invalid
  config; configured `primary_result_action_type` overrides naive inference; explicit-ids path uses
  `get_account` and skips discovery. All exercised.
- **Docs & non-code surfaces:** `config/fx_rates.json` confirmed tracked (in the commit, not
  gitignored). README + `META_API_SETUP.md` updated to "three discovery tools" and describe the
  static-FX caveat. Tool registration auto-iterates `build_discovery_tools` / falls back on
  `DISCOVERY_TOOL_DESCRIPTIONS.get`, so no hardcoded tool count went stale. No `__all__`/changelog
  to update.

**Minor — fixed inline this pass:**
- The handoff flagged the *partial*-contributor `normalized_total` (some accounts report
  results/revenue, others don't) as covered only indirectly. Added
  `test_cross_account_performance_partial_contributor_normalized_total` pinning the aggregate
  semantic (spend/impressions/clicks summed over all FX-eligible accounts; results/purchase_value
  over contributors only; `cost_per_result`/`roas` = portfolio spend over the contributors'
  results/revenue) on both `normalized_total` and the per-currency subtotal. Suite now **520 passed**
  (was 519).

**Major — filed to backlog (not blocking):**
- `tickets/backlog/cross-account-perf-roas-coverage-transparency.md`: the aggregate `roas` /
  `cost_per_result` are emitted even when only a subset of accounts track revenue/results, and the
  output surfaces no coverage signal (`results_contrib`/`pv_contrib` are internal-only). Given the
  project's strong stance on not overstating ROAS trustworthiness (`AGENTS.md` Interpretation Rules),
  a downstream agent could report a portfolio ROAS without knowing coverage is partial. The
  computation itself is correct; only the transparency is missing. Left to a follow-up rather than an
  inline schema expansion during review.

**Accepted deferrals (documented in the handoff, confirmed reasonable):**
- `level` supports only `"account"` (other values raise `ValueError`) — campaign/adset roll-ups are a
  future ticket, honestly stubbed-out, not silently broken.
- Static FX only; live/Meta FX deliberately deferred by the product owner. `fx_as_of` + `fx_note`
  surfaced so no consumer mistakes the table for billing-grade rates.
- No rounding applied (matches the plan's example; left to the display layer).

**No pre-existing failures** encountered — full suite green at HEAD before and after the change.

## How to validate
- Full suite: `.venv/bin/python -m pytest tests/ -q` → **520 passed**.
- Focused: `.venv/bin/python -m pytest tests/ -q -k "cross_account_performance or fx or currency or derived"` → 23 passed.
- Compile: `.venv/bin/python -m py_compile src/meta_ads_analysis/{currency,account_discovery,mcp_server}.py`.
