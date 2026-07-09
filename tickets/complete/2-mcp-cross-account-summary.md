description: A one-call tool that totals spend/performance across every reachable ad account, keeping each currency separate instead of adding them together. Reviewed and shipped.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What shipped

`cross_account_spend_summary` — a cross-account aggregate read that answers a spend/performance
question over **all reachable accounts** (or an explicit subset of account ids) in one call. It fans
out existing per-account reads **sequentially** (no new concurrency; relies on the client's 429
retry) and returns a combined table plus **per-currency subtotals**, with **no grand total** so
different currencies are never summed together.

- **Library** (`account_discovery.py`): `cross_account_spend_summary(reader, *, date_from, date_to,
  account_ids=None, insight_fields=None)` + `DEFAULT_SUMMARY_INSIGHT_FIELDS = ["spend",
  "impressions", "clicks"]`, helpers `_parse_metric` / `_ad_account_id_from_row`. Discovery-level
  `MetaApiError` propagates (whole-call failure); a per-account `MetaApiError` is recorded in
  `errors` and skipped. Subtotals grouped by `currency`; missing currency → `"UNKNOWN"`.
- **MCP surface** (`mcp_server.py`): `build_discovery_tools` now returns `list_ad_accounts` +
  `cross_account_spend_summary` (thin delegate; `insight_fields` deliberately library-only). Rides
  the existing discovery loop + `_wrap_tool_errors` mapping — no extra `build_server` wiring.
- **Docs**: `README.md` discovery bullet + `docs/META_API_SETUP.md` tool-surface enumeration both
  name the new tool.

Matches the implement-stage handoff and the plan (`mcp-cross-account-read-tools`, Part B). The
returned shape is as the implement ticket specified.

## Review findings

**Verified correct (no change needed):**
- **Reader interface contract.** `cross_account_spend_summary` calls `reader.list_ad_accounts`,
  `reader.get_account`, and `reader.fetch_insights(level=..., time_increment=...)` — all present on
  `MetaReaderProvider` with matching keyword-only signatures (`reader_provider.py`); `list_ad_accounts`
  is in `READ_METHODS`, so `FakeMetaReader` accepts the stub. `_normalize_ad_account_id` exists in
  `account_registry` and normalizes bare numeric → `act_`.
- **MCP wiring / error mapping.** The tool is registered via the `build_discovery_tools` loop in
  `build_server` and wrapped by `_wrap_tool_errors` (`functools.wraps` preserves the wrapper's real
  signature so FastMCP derives the schema). A per-account `MetaApiError` never reaches the wrapper; a
  discovery-level one does and maps to `ToolError`.
- **Per-row vs subtotal metric-presence asymmetry** (a no-delivery account row carries no metric keys
  while its subtotal counts the metric as 0). Confirmed this is exactly what the plan mandates
  ("per-row value should reflect what Meta returned (0 or absent); missing counts as 0 for the
  subtotal"), not an oversight. `_parse_metric`'s `not in (None, "")` guard correctly still emits an
  explicit `"0"` string as `0` in the row.
- **`_ad_account_id_from_row` robustness.** Prefers `id` (`act_<n>`, always returned by Graph),
  falls back to `account_id` (which *is* in `DEFAULT_AD_ACCOUNT_FIELDS`) — safe even if `id` is
  absent.
- **Mock mode.** `build_mock_reader` stubs `list_ad_accounts` / `get_account` / `fetch_insights`;
  the smoke test confirms one USD row, one-key totals, zero live calls, `get_account` untouched on
  the discovery path.

**Minor — fixed inline in this review pass:**
- **Doc surface incomplete.** `docs/META_API_SETUP.md` line ~260 enumerated the full MCP tool
  surface but named only `list_ad_accounts` among the discovery tools. Updated to name both
  discovery tools. (README was already correct.)
- **`insight_fields` library param was completely untested.** Added
  `test_cross_account_summary_insight_fields_restricts_metrics_summed` — a restricted set (`["spend"]`)
  narrows both the fields requested from `fetch_insights` (no over-fetch) and the metrics subtotaled.
- **Duplicate explicit ids double-counted.** The explicit-ids path built one target per raw id, so
  `["1", "act_1"]` (both → `act_1`) was fanned out and subtotaled twice — reachable from the MCP
  surface since an LLM controls `account_ids`, and summing an account twice is unambiguously wrong.
  Fixed with order-preserving de-duplication after normalization (`reachable_count`/`account_count`
  now reflect distinct accounts, still equal to each other as the plan requires). Added
  `test_cross_account_summary_explicit_duplicate_ids_counted_once`.

**Considered — accepted as-is, no ticket filed:**
- **Ratio-metric guard is convention, not enforced.** Only additive metrics are summed; a ratio
  field (`cpc`/`ctr`/`roas`) passed via `insight_fields` would be summed incorrectly. Not reachable
  from the MCP tool (`insight_fields` is library-only), there are no library callers that pass ratio
  fields, and both the plan and a module comment document the convention. Left as documented. **If
  `insight_fields` is ever exposed on the MCP surface, add an additive-only allow-list at that
  point.**
- **`int` vs `float` inference in `_parse_metric`** keys off `.`/`e`/`E`. Whole-number spend
  (`"100"`) parses to `int`; a subtotal stays `int` until a float is added. Values are always
  correct (int+float promotes); purely cosmetic. No downstream consumer relies on spend being
  `float` (verified: this is a fresh aggregate with no other readers).
- **`account_count` vs `reachable_count` are always equal in this implementation** (every discovered
  account becomes a target; explicit ids are deduped into both). The plan's example implied they
  could differ; here they don't. Both are in the documented shape and harmless — the meaningful
  distinction (attempted vs *succeeded*) is `account_count` vs `len(accounts)`, which is exercised by
  the partial-failure test.

**Not covered by design (documented in the implement handoff, unchanged):**
- `all_days` / `level="account"` yielding exactly one aggregated row per account is asserted only at
  the call-argument level — the actual Meta aggregation is integration behavior, out of scope under
  the repo's MOCKS-ONLY test policy.
- No live/integration test of the sequential fan-out under real 429s — by design (no live calls).

## Validation

- `python3 -m pytest tests/test_meta_ads_analysis.py -q` → **487 passed** (485 pre-existing + 2 added
  this review). Log: `/tmp/mcp-cross-account-review.log`.
- No linter/type-checker is configured in this repo (pyproject declares only `pytest`); nothing to run.
- No pre-existing failures; nothing deferred.
