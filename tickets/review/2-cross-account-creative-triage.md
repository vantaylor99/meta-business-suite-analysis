description: A new read tool that ranks the actual ads across all managed accounts — best/worst by spend, results, or cost-per-result — so a specialist sees at a glance which specific ads to scale or pause, without walking every ad ever created.
prereq:
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What shipped

`cross_account_creative_triage` — the ad-level sibling of `rank_accounts`. It pools **one row per
delivering ad** across every reachable account (or an explicit `account_ids` subset), ranks the pool
by a single metric over a window, and returns the top-or-bottom N (winners vs losers = two calls,
`order="desc"` then `"asc"`).

Everything is built on `reader.fetch_insights(level="ad", time_increment="all_days")`, which returns
one aggregated row per ad **that actually delivered** (had impressions/spend). It therefore **never**
calls `fetch_ads` / enumerates `/{account}/ads`, so it is naturally scoped to recently-active creative
and skips the dormant graveyard that times out the ad-health scan (`flag-ad-health-scan-scale`). It is
a *performance* read, **not** an ad-health read.

### Where the code lives

- `src/meta_ads_analysis/account_discovery.py`
  - `DEFAULT_TRIAGE_INSIGHT_FIELDS` (next to `DEFAULT_PERFORMANCE_INSIGHT_FIELDS`) — adds `ad_id` /
    `ad_name` to the performance base fields.
  - `cross_account_creative_triage(...)` — inserted between `rank_accounts` and the goal-grading
    section. Reuses the existing machinery verbatim: `resolve_scope` / `fan_out_accounts` for the
    fan-out; `_resolve_result_key` + the `_LEAD_KEYS_LOWER` / `LEAD_KEYS` / `PURCHASE_KEYS` /
    `_find_metric` / `_metric_blob_list` / `_number` / `_as_count` metric parsing; `compute_derived_metrics`
    for the ratios; `load_fx_table` / `FxTable` for FX; and the `RANK_METRIC_ALIASES` / `_RANK_MONEY_METRICS`
    constants + the `rank_accounts` partition/sort/rank block, adapted to key on `ad_id`.
- `src/meta_ads_analysis/mcp_server.py`
  - Wrapper in `build_discovery_tools` (forwards `date_from, date_to, metric, order, limit,
    account_ids, reporting_currency`; does **not** expose `fx_table` — test-only seam) + registered in
    the returned dict.
  - `DISCOVERY_TOOL_DESCRIPTIONS["cross_account_creative_triage"]` entry.

### Key design points (as specced, resolved — don't re-open)

- Single ranked list keyed on `ad_id` — 1:1 with the `rank_accounts` contract, not a combined
  winners+losers payload.
- Result key resolved **once per account** (config-first via `_resolve_result_key`, else inferred from
  the account's *pooled* ad `actions`) and applied per ad, including the lead-family self-heal branch.
  Account identity (`account_id` / `account_name` / `currency`) comes from the account metadata row,
  not the insights row; `ad_id` / `ad_name` come from the insights row (blank `ad_name` → falls back to
  `ad_id`).
- Money metrics rank on the `*_normalized` twin (`value` = normalized, `value_native` = native);
  ratio/count metrics rank natively.
- No-FX currency: **one** `errors` entry per account (not per ad), matching
  `cross_account_performance` granularity; that account's ads fall to `unranked` under a money metric
  but still rank under a ratio/count metric.

## How to validate

- `python -m pytest tests/test_meta_ads_analysis.py -q` — **663 passed** (full file, so both
  discovery-tool enumeration assertions are exercised). New tests live under the
  `cross_account_creative_triage` header just after the `rank_accounts` tests.
- `python -m py_compile` on the three touched files — clean.
- **Type check:** the repo configures **no** mypy/pyright/ruff (checked `pyproject.toml` — only
  `[tool.pytest.ini_options]`). Nothing to run; annotations follow the surrounding style.

## Tests added (all mocks — no live Meta call)

Modeled on `_perf_reader` (multiple ad rows per account) + the `_fx()` table; the result-key path is
driven by `monkeypatch.setattr(_account_discovery, "_registry_by_ad_account_id", ...)`.

- `pools_and_ranks_ads_across_accounts_by_spend` — 2 accts × 2 ads, `desc`/`limit=3` → top 3, ranks,
  `ad_count == 4`, `ranked_total == 4`.
- `ascending_returns_losers` — same fixture, `asc` → cheapest end.
- `cost_per_result_zero_result_ad_is_unranked` — zero-result ad → `unranked` (`metric unavailable`),
  never `inf`.
- `money_metric_ranks_on_normalized_twin` — MXN + EUR ranked by `spend` on `spend_normalized`;
  `value`/`value_native` distinct.
- `no_fx_currency_ad_is_unranked_and_errored_once` — JPY account: `unranked` (reason names currency) +
  exactly one `errors` entry under a money metric; still ranks under `ctr`.
- `resolves_lead_family_per_account` — stale config lead key self-heals via the lead family (single
  value, not summed).
- `config_result_key_wins_over_inference` — ad carries purchase(10)+lead(3), configured `lead` key
  wins → `results == 3`, label `Sign-ups`.
- `partial_account_failure_isolated_and_deterministic` — 3 accts, middle raises `MetaApiError` →
  recorded + skipped, survivors rank, order-independent (staggered `time.sleep`).
- `missing_ad_name_falls_back_to_ad_id` — absent and blank `ad_name` both fall back.
- `tie_ranks_share_and_tiebreak_by_ad_id` — ties share a rank, tiebreak `ad_id` ascending.
- `invalid_metric_order_limit_and_reporting_currency_raise` — the four validation paths.
- `no_accounts_reachable_note` — `account_ids=None` + empty discovery → `note="no accounts reachable"`.
- `build_discovery_tools_creative_triage_mock_smoke` — end-to-end through the wrapper (no `fx_table`).
- Updated `test_build_discovery_tools_exposes_cross_account_summary`: `set(discovery)` 8 → 9 and added
  the `DISCOVERY_TOOL_DESCRIPTIONS` membership assertion.

## Reviewer: treat this as a starting point — gaps & things to poke at

**Deliberate deferrals flagged by the plan (out of scope, NOT done here — confirm you agree):**

1. **Shared `_rank_pooled_rows(...)` helper.** The ~40-line partition/sort/rank block is duplicated
   from `rank_accounts` (adapted to `ad_id`) rather than extracted, per the plan's explicit
   lower-risk-for-this-ticket call. A later cleanup could unify both. If you extract it now, re-run
   the `rank_accounts` **and** triage suites together.

2. **One-call `both_ends` winners+losers variant.** Not built — winners vs losers is two calls, each
   re-running the (expensive) ad-level fan-out. The tool description tells the operator to scope
   `account_ids` and that a second call re-reads. A future enhancement.

**Honest gaps / where my tests are only a floor:**

- **No live-scale test.** Everything is mock-only. On the real token (792 accounts per
  [[token-reach-and-summary-scaling]]) an all-accounts triage issues one ad-level insights read per
  account through the bounded fan-out; each such read can itself be large/paginated for a
  high-ad-count account. I did **not** measure wall-clock or paging behavior against live data — worth
  a bounded `account_ids` sanity check out-of-band before trusting the all-accounts path. The tool
  description already steers operators to scope `account_ids`.
- **`ad_id` tiebreak is lexicographic string order** (Meta returns ids as strings), same convention as
  `rank_accounts`' `ad_account_id` tiebreak. Fine for determinism; just note it's string-sorted, not
  numeric — two ids `"100"` and `"99"` order as `"100" < "99"`. Consistent with the existing tool, so
  intentional, but flagging in case a reviewer expects numeric ordering.
- **`result_label` in ranked entries is emitted unconditionally** (`row.get("result_label")` → may be
  `None` when no result key resolved), matching the documented ranked-entry shape. The per-ad *row*
  omits it when absent; the ranked *projection* keeps the key. Confirm that's the shape you want.
- **Duplicate `ad_id` across accounts** is theoretically possible (ids are unique within Meta, so in
  practice not), and would only affect the tiebreak ordering, never correctness of values — each row
  carries its own `ad_account_id`. Not defended against beyond the stable sort.
