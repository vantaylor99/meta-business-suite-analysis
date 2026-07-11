description: Added and reviewed the rank_accounts MCP tool — it sorts every reachable ad account by one metric (spend, CPC, ROAS, etc.) and returns the top or bottom few, so a manager can instantly see best/worst performers across the whole fleet.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
----

## Summary

`rank_accounts` is a **pure post-processor over `cross_account_performance`** (one read, no new
Meta read shape), mirroring `account_benchmark` and `flag_accounts_needing_attention`. It sorts the
whole reachable fleet (or an explicit `account_ids` subset) by a single metric and returns the top or
bottom `limit`. Money metrics rank on their `reporting_currency`-normalized twin (`value` normalized,
`value_native` native); ratio/count metrics rank as-is. Accounts lacking the metric (no delivery, or a
money metric in a currency with no FX rate) land in an `unranked` bucket with a reason rather than
being sorted as a misleading 0/∞. Ties share a 1-based rank (strictly-better + 1), tiebroken by
`ad_account_id`. Validation (metric/order/limit) is fail-fast **before** any read.

Wired into `build_discovery_tools` and `DISCOVERY_TOOL_DESCRIPTIONS` as the seventh discovery tool;
the MCP wrapper omits `fx_table` (test-only seam) and loads committed `config/fx_rates.json` itself.

## Review findings

**Verdict:** implementation is correct and well-structured. Five minor issues fixed inline; no major
issues, so no new tickets were filed.

### What was checked
- **Correctness** of the rank pipeline: partition → sort (sign-flip for `desc`, ascending tuple) →
  1-based rank assignment (O(n), ties share strictly-better + 1) → truncate to `limit`. Verified
  against `cross_account_performance`'s actual row shape (`_NORMALIZED_MONEY_DERIVED`,
  `compute_derived_metrics`, `_as_count`).
- **Money vs. currency-invariant handling:** money metrics (spend/cpm/cpc/cost_per_result) rank on the
  `*_normalized` twin; ROAS/CTR/counts rank natively. Confirmed `roas` is correctly **excluded** from
  `_RANK_MONEY_METRICS`.
- **Unranked partitioning:** the `no FX rate` vs `metric unavailable` distinction (native field present
  in row but no normalized twin ⇒ no-FX; native field absent ⇒ unavailable) is correct against how
  `cross_account_performance` omits vs. normalizes fields.
- **Fail-fast validation** ordering (before the network read), **alias** resolution (cpl/cpa →
  cost_per_result, canonical in output), **MCP wiring**, **determinism** (perf rows are in input order;
  ranked has an explicit tiebreak).
- **Edge/error/interaction paths:** limit>scope, limit 0, unknown metric, invalid order, ties across the
  truncation boundary, zero-impression → CTR absent, cross-currency comparison, explicit `account_ids`.
- **Docs:** README, docs/META_API_SETUP.md, and the config.py "sixth discovery tool" comment.
- **Lint + tests:** project ships pytest only (no ruff/mypy configured); `py_compile` clean; full
  suite green.

### Findings & disposition (all minor → fixed in this pass)
1. **Count `value` emitted as float.** Count metrics (impressions/clicks/results) surfaced `value` as
   the `float()`-coerced sort key (e.g. `1000.0`), inconsistent with the rest of the codebase which
   presents counts as `int` via `_as_count`. → `entry["value"]` now reads the row's original-typed
   value (`row.get(sort_field)`); the float sort key is used only for ranking. Regression test added
   (`test_rank_accounts_count_metric_value_is_int`).
2. **MCP description miscategorized ROAS.** The tool description listed ROAS among money metrics
   "compared in one reporting_currency," but ROAS is currency-invariant and ranked natively. → Reworded
   to split normalized money metrics from currency-invariant ratio/count metrics.
3. **No MCP-layer smoke test** (implementer-flagged gap). The wiring test called the library function
   directly with `fx_table=_fx()`, never the actual MCP callable. → Added
   `test_build_discovery_tools_rank_accounts_mock_smoke`, which exercises `tools["rank_accounts"]()`
   end-to-end against the committed FX table (USD accounts keep it deterministic).
4. **No `account_ids` subset coverage** (implementer-flagged gap). → Added
   `test_rank_accounts_respects_account_ids_subset`, which also asserts the explicit path uses
   `get_account` and never touches `list_ad_accounts`.
5. **Docs omitted the new tool.** README and docs/META_API_SETUP.md enumerated only six discovery tools
   and never mentioned `rank_accounts`. → Added a paragraph to each and corrected the "six → seven
   discovery tools" count/list. (config.py's "sixth discovery tool" comment refers to `pacing_report`
   and remains correct.)

### Observations (acceptable, not changed)
- The `unranked` list follows `cross_account_performance`'s input order (deterministic per
  `fan_out_accounts`) rather than being independently re-sorted by `ad_account_id` like `ranked`. This
  is deterministic run-to-run, so it is not a bug; left as-is.
- `results`/`cost_per_result` require an `actions` payload; that path is covered by the alias-cpl test
  and the new subset/smoke tests exercise spend/impressions. No additional coverage needed.

### Test count
584 → **587** passing (+3: MCP smoke, count-value-int, account_ids subset). Full
`tests/test_meta_ads_analysis.py` suite green.
