description: Add a "rank my accounts by metric" tool so managers can get a top/bottom-N shortlist without reading a flat table.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: easy
----
## Architecture

`rank_accounts` is a **pure post-processor** over `cross_account_performance` — the same
relationship `account_benchmark` and `flag_accounts_needing_attention` have to that tool. It calls
`cross_account_performance` once, sorts the per-account rows by the requested metric, and returns a
`ranked` list and an `unranked` bucket (accounts that lacked the metric or had no FX rate). No new
Meta read shape; FX normalization, Simpson's-paradox-safe derived metrics, and partial-failure
isolation are inherited for free.

## Metric surface

Valid `metric` names (lowercase):

| name | field read | class |
|------|-----------|-------|
| `spend` | `spend_normalized` (ranking); `spend` (display) | money |
| `cpm` | `cpm_normalized` / `cpm` | money |
| `cpc` | `cpc_normalized` / `cpc` | money |
| `cost_per_result` | `cost_per_result_normalized` / `cost_per_result` | money |
| `cpl`, `cpa` | aliases for `cost_per_result` | money |
| `ctr` | `ctr` | ratio (currency-invariant) |
| `roas` | `roas` | ratio |
| `impressions` | `impressions` | count |
| `clicks` | `clicks` | count |
| `results` | `results` | count |

Money metrics are ranked on their `*_normalized` twin (reporting currency). An account missing its
FX rate has no normalized twin → lands in `unranked` with `"no FX rate for {currency}"`. Ratio and
count metrics are ranked natively (no twin needed).

Module-level constants in `account_discovery.py`:

```python
# Maps every accepted metric name (including aliases) to its canonical internal field name.
RANK_METRIC_ALIASES: dict[str, str] = {
    "spend": "spend", "cpm": "cpm", "cpc": "cpc",
    "cost_per_result": "cost_per_result", "cpl": "cost_per_result", "cpa": "cost_per_result",
    "ctr": "ctr", "roas": "roas",
    "impressions": "impressions", "clicks": "clicks", "results": "results",
}

# Money metrics are ranked on their normalized twin (reporting_currency).
_RANK_MONEY_METRICS: frozenset[str] = frozenset({"spend", "cpm", "cpc", "cost_per_result"})
```

## Return shape

```python
{
    "date_from": str,
    "date_to": str,
    "metric": str,           # canonical name (e.g. "cost_per_result" even if "cpl" was passed)
    "order": str,            # "asc" | "desc"
    "limit": int,
    "reporting_currency": str,
    "fx_as_of": str,
    "fx_note": str,
    "account_count": int,    # total scope (attempted)
    "ranked": [              # up to `limit` entries, in rank order
        {
            "rank": int,
            "ad_account_id": str,
            "account_id": str | None,
            "name": str | None,
            "currency": str,
            "value": float,          # sort key: normalized for money, native for ratio/count
            "value_native": float,   # ONLY present for money metrics (the native currency figure)
        },
        ...
    ],
    "ranked_total": int,    # total accounts that had the metric (before limit truncation)
    "unranked": [
        {"ad_account_id": str, "name": str | None, "reason": str},
        ...
    ],
    "errors": [...],        # read-level errors from cross_account_performance
}
```

`rank` is 1-based. Ties share the count-of-strictly-better + 1 convention (same as `account_benchmark`
rank). Tiebreak for sort stability: `ad_account_id` ascending, so identical metric values produce
deterministic ordering run-to-run.

## MCP wiring

Add `rank_accounts` to `DISCOVERY_TOOL_DESCRIPTIONS` and wire it in `build_discovery_tools`.
The MCP wrapper does NOT expose `fx_table` (test seam only). `order` and `limit` have MCP defaults
(`"desc"`, `10`). The `metric` name is normalized to lowercase before lookup.

Tool description (for `DISCOVERY_TOOL_DESCRIPTIONS`):

> "Rank every ad account this token can reach (or an explicit list) by a single efficiency or spend
> metric for a date range, returning the top or bottom N. Money metrics (spend/CPC/CPM/CPL/ROAS)
> are compared in one reporting_currency (default USD) so accounts in different currencies are
> comparable. Accounts lacking the metric (e.g. no results → no CPL) are grouped into an 'unranked'
> bucket with a reason instead of sorted as zero or infinity. Valid metrics: spend, cpm, cpc, ctr,
> cost_per_result (aliases: cpl, cpa), roas, impressions, clicks, results."

## Edge cases & interactions

- **`limit <= 0`**: `ValueError("limit must be a positive integer; got {limit}")`.
- **`limit > ranked_total`**: return all ranked accounts (no error, no padding).
- **Unknown metric**: `ValueError` listing all valid names sorted.
- **`order` not in `{"asc", "desc"}`**: `ValueError`.
- **Ties**: stable sort by `(metric_value, ad_account_id)` (asc on id), rank = strictly-better-count + 1.
- **Money metric, no FX for some accounts**: those accounts go to `unranked` with `"no FX rate for {currency}"`, ranking proceeds for the rest.
- **Account missing metric entirely** (e.g. zero impressions → `ctr` absent from row): `unranked` with `"metric unavailable"`.
- **`reporting_currency` not in FX table**: `ValueError` propagated from `cross_account_performance` (whole-call failure, same contract as prereq).
- **`account_ids=None` discovery fails**: `MetaApiError` propagates unchanged (whole-call failure).
- **Empty scope / all accounts unranked**: `ranked=[]`, `ranked_total=0`, normal `unranked` list.
- **`cpl`/`cpa` aliases**: normalized to `cost_per_result` before lookup; `metric` key in output is the CANONICAL name.

## TODO

- Add `rank_accounts` function to `account_discovery.py`:
  - Validate `metric` (normalize lowercase, resolve alias to canonical), `order`, `limit` (fail fast before any read)
  - Call `cross_account_performance(reader, date_from=..., date_to=..., account_ids=..., reporting_currency=..., fx_table=...)` — pass `fx_table` through as test seam
  - Separate rows into `rankable` (have a non-None metric value) vs `unranked` (missing value or no-FX twin for money metrics); for money metrics, try `{canonical}_normalized` — absent means either no FX or genuinely zero/absent
  - Sort `rankable` by `(sort_value, ad_account_id)` asc always (flip `sort_value` sign for `order="desc"`)
  - Assign `rank` (1-based, same rank for ties using strictly-better count + 1)
  - Truncate to `limit`, build output shape
  - Include `value_native` in ranked rows only for money metrics (present on the source row as the canonical field name)
  - Include `ranked_total` (pre-limit count of rankable rows)
  - Propagate `errors`, `fx_as_of`, `fx_note` from the `cross_account_performance` result

- Add `RANK_METRIC_ALIASES` and `_RANK_MONEY_METRICS` constants to `account_discovery.py`

- Wire into `mcp_server.py`:
  - Add description to `DISCOVERY_TOOL_DESCRIPTIONS`
  - Add `rank_accounts` callable inside `build_discovery_tools` (does NOT expose `fx_table`)

- Add tests to `tests/test_meta_ads_analysis.py`:
  - `test_rank_accounts_descending_by_spend` — 3 accounts, rank by spend desc, check rank order/values/identity fields
  - `test_rank_accounts_ascending_by_cpc` — rank by CPC asc (lowest first = best), check order
  - `test_rank_accounts_money_metric_uses_normalized_for_ranking` — USD + MXN accounts, rank by CPC; verify ranked on normalized twin, `value_native` present and distinct
  - `test_rank_accounts_no_fx_account_lands_in_unranked` — account with unknown currency → `unranked` reason contains "no FX rate"
  - `test_rank_accounts_missing_metric_lands_in_unranked` — account row without `ctr` field → `unranked` with "metric unavailable"
  - `test_rank_accounts_limit_larger_than_scope_returns_all` — limit=100, 3 accounts → returns all 3
  - `test_rank_accounts_limit_zero_raises` — `limit=0` → `ValueError`
  - `test_rank_accounts_unknown_metric_raises` — `metric="foobar"` → `ValueError` listing valid names
  - `test_rank_accounts_invalid_order_raises` — `order="sideways"` → `ValueError`
  - `test_rank_accounts_ties_are_stable_by_account_id` — two accounts with same metric value → lower id ranks first, rank numbers match
  - `test_rank_accounts_alias_cpl_canonical_name_in_output` — pass `metric="cpl"`, output `metric` key is `"cost_per_result"`
  - `test_build_discovery_tools_exposes_rank_accounts` — assert `"rank_accounts"` in discovery tools set and in `DISCOVERY_TOOL_DESCRIPTIONS` (update the existing `test_build_discovery_tools_exposes_cross_account_summary`)

  Test helpers: reuse `_perf_reader` fixture pattern (FakeMetaReader with `list_ad_accounts` + `fetch_insights` stubs), `monkeypatch` out `_registry_by_ad_account_id`, inject `_fx()` as the `fx_table` seam.
