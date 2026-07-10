description: Review the rank_accounts implementation — a post-processor over cross_account_performance that ranks ad accounts by a single metric and returns a top/bottom-N shortlist.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
----

## Summary

`rank_accounts` is implemented as a pure post-processor over `cross_account_performance`, mirroring the same pattern as `account_benchmark` and `flag_accounts_needing_attention`. It adds:

- `RANK_METRIC_ALIASES` and `_RANK_MONEY_METRICS` module-level constants in `account_discovery.py`
- `rank_accounts` function in `account_discovery.py` (appended after the pacing code, ~100 lines)
- `"rank_accounts"` entry in `DISCOVERY_TOOL_DESCRIPTIONS` in `mcp_server.py`
- `rank_accounts` callable in `build_discovery_tools` in `mcp_server.py`
- 12 new tests in `tests/test_meta_ads_analysis.py`; existing `test_build_discovery_tools_exposes_cross_account_summary` updated to include `"rank_accounts"` in the expected tool set

All 12 new tests pass; all 50 pre-existing related tests still pass.

## Key design decisions

- **Validation is fail-fast before any read**: metric, order, and limit are validated before calling `cross_account_performance`, so a bad call fails immediately without issuing network reads.
- **Money metrics ranked on normalized twin**: `spend_normalized`/`cpm_normalized`/etc. — absent because FX rate missing → `"no FX rate for {currency}"`; absent because native metric absent → `"metric unavailable"`. The distinction is made by checking whether the native field (`canonical`) is present in the row.
- **O(n) rank assignment**: leverages the already-sorted list — `current_rank = i + 1` only when the value changes; ties share the previous rank (no O(n²) re-scan).
- **`fx_table` not exposed to LLM**: the MCP wrapper omits `fx_table` (test seam only), consistent with every other discovery tool.
- **`cpl`/`cpa` aliases**: normalized to `"cost_per_result"` before lookup; canonical name appears in output.

## Use cases for testing / validation

1. **Descending spend ranking** — highest spender gets rank 1
2. **Ascending CPC ranking** — cheapest CPC gets rank 1 (manager sees best-value accounts first)
3. **Cross-currency money metric** — MXN account with 10 MXN CPC beats USD account with 2 USD CPC (0.55 USD normalized vs 2 USD); `value_native` carries the MXN figure
4. **No-FX account → unranked** with "no FX rate" reason
5. **Missing metric → unranked** with "metric unavailable" reason (e.g. zero-impressions account has no CTR)
6. **Limit > scope** → returns all ranked accounts, no error
7. **Limit 0** → `ValueError`
8. **Unknown metric** → `ValueError` listing valid names
9. **Invalid order** → `ValueError`
10. **Ties** → both accounts share rank 1; lower `ad_account_id` appears first; next distinct value gets rank 3 (not 2)
11. **Alias `cpl`** → `metric` key in output is `"cost_per_result"`
12. **MCP wiring** → `"rank_accounts"` in both callable dict and `DISCOVERY_TOOL_DESCRIPTIONS`

## Known gaps / reviewer notes

- The MCP smoke test uses the library directly with `fx_table=_fx()` rather than going through `tools["rank_accounts"]()`, because the tool's MCP wrapper loads the committed `config/fx_rates.json` (no `fx_table` parameter). The committed table works in CI, but a reviewer may want a fuller MCP-layer smoke test similar to `test_build_discovery_tools_pacing_report_mock_smoke` that exercises the committed FX table end-to-end.
- No test exercises `account_ids` (explicit subset). The inherited `cross_account_performance` path is well-tested for that parameter; adding a rank test covering it would be low-risk but currently absent.
- `results` metric requires `actions` in the insight row — tests only cover spend/cpc/ctr for brevity. The `test_rank_accounts_alias_cpl_canonical_name_in_output` test exercises CPL/cost_per_result with an action payload.
