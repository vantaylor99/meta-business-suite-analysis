description: A new read tool that ranks the actual ads across all the accounts you manage — best and worst by spend, results, or cost-per-result — so a specialist can see at a glance which specific ads to scale or pause, without the tool grinding through every ad ever created.
prereq:
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## What we're building

A new discovery read tool, `cross_account_creative_triage`, that ranks **individual ads pooled
across every reachable account** (or an explicit subset) by a single metric over a window, returning
the top-or-bottom N — i.e. *winners vs losers* at the ad/creative level. It is the ad-level sibling of
the account-level `rank_accounts`, built on the same fan-out / FX-normalization / Simpson's-safe
"recompute ratios from summed components" machinery.

The whole tool exists **only** on top of ad-level insights over the window. It must **never** call
`fetch_ads` / enumerate `/{ad_account_id}/ads`. `reader.fetch_insights(level="ad",
time_increment="all_days")` returns one aggregated row per ad **that actually delivered** in the
window (had impressions/spend), so the read is naturally scoped to recently-active ads and skips the
dormant graveyard that times out the ad-health scan (see `flag-ad-health-scan-scale`).

## How it fits the existing code

`account_discovery.py` already has everything this needs; the new function is a near-mechanical
adaptation of two existing functions:

- **`cross_account_performance`** (`account_discovery.py:527`) — the per-account fan-out that reads one
  `level="account"` insights row per account, resolves the result key, computes derived metrics, and
  normalizes money to `reporting_currency`. Triage does the same but reads `level="ad"` (many rows per
  account, one per delivering ad) and builds one output row **per ad** instead of per account.
- **`rank_accounts`** (`account_discovery.py:2608`) — the pure post-processor that partitions rows into
  rankable vs `unranked`, sorts, assigns 1-based ranks (ties share strictly-better-count + 1, tiebroken
  by id ascending), and truncates to `limit`. Triage reuses this ranking logic verbatim, keyed on
  `ad_id` instead of `ad_account_id`.

Reuse directly (all already in `account_discovery.py`):
- `resolve_scope`, `fan_out_accounts`, `fanout_max_workers_from_env` — scope + bounded-concurrency
  fan-out with per-account error isolation and input-ordered determinism.
- `compute_derived_metrics` — recomputes `cpm/cpc/ctr/cost_per_result/roas` from base components;
  omits (never `inf`/`NaN`/`0`) any metric whose denominator is zero or component missing.
- `_resolve_result_key`, `_find_metric`, `LEAD_KEYS`, `_LEAD_KEYS_LOWER`, `PURCHASE_KEYS`,
  `_metric_blob_list`, `_number`, `_as_count` — result-key resolution + metric parsing, identical to
  `cross_account_performance`.
- `load_fx_table` / `FxTable.has` / `FxTable.convert` — static FX normalization
  ([[currency-precision-low-priority]]).
- `RANK_METRIC_ALIASES`, `_RANK_MONEY_METRICS` — metric-name canonicalization + which metrics rank on
  their `*_normalized` twin. **Reuse these constants as-is** so the triage metric vocabulary is
  identical to `rank_accounts` (`spend, cpm, cpc, cost_per_result` [aliases `cpl`/`cpa`], `ctr, roas,
  impressions, clicks, results`).

## Function contract

```python
def cross_account_creative_triage(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    metric: str = "spend",
    order: str = "desc",
    limit: int = 10,
    account_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,   # test-only seam, NOT exposed to the LLM
) -> dict[str, Any]:
```

Per-ad row shape (before ranking; omit any base metric Meta left blank, exactly like
`cross_account_performance` does — a missing metric stays absent, never `0`):

```
{
  "ad_account_id": "act_1", "account_id": "1", "account_name": "Acme",
  "ad_id": "400", "ad_name": "Spring Promo",
  "currency": "USD",
  "spend": 120.0, "impressions": 5000, "clicks": 90, "results": 8,
  "result_label": "lead", "purchase_value": 0.0,      # each present only when Meta returned it
  "cpm": ..., "cpc": ..., "ctr": ..., "cost_per_result": ..., "roas": ...,   # from compute_derived_metrics
  "spend_normalized": ..., "cpc_normalized": ..., "cpm_normalized": ...,
  "cost_per_result_normalized": ..., "purchase_value_normalized": ...        # money twins (no ctr/roas twin)
}
```

Return shape (mirror `rank_accounts` field-for-field, adapting ids to ad level):

```
{
  "date_from", "date_to",
  "metric": <canonical>, "order", "limit",
  "reporting_currency", "fx_as_of", "fx_note",
  "account_count": <resolved scope size>,       # accounts attempted
  "ad_count": <total pooled ad rows built>,      # delivering ads seen across all accounts
  "ranked": [ {rank, ad_account_id, account_id, account_name, ad_id, ad_name, currency,
               value, value_native (money only), result_label} , ... ][:limit],
  "ranked_total": <len(rankable)>,
  "unranked": [ {ad_account_id, ad_id, ad_name, reason} , ... ],
  "errors": [ {ad_account_id, error} , ... ],
}
```

`note="no accounts reachable"` when `account_ids is None` and discovery found nothing (same as the
prereq tools).

## Design decisions (resolved — do not re-open in implement)

- **Single ranked list, `order`+`limit` — NOT a combined winners+losers payload.** This is exactly the
  `rank_accounts` contract, which is the established "top or bottom N" pattern in this codebase.
  Winners vs losers = two calls with `order="desc"` then `"asc"`. Tradeoff: each call re-runs the
  ad-level fan-out (the expensive part), so the tool description tells the operator to scope
  `account_ids` and, for both ends, that a second call re-reads. A one-call `both_ends` variant is a
  deliberate future enhancement, out of scope here — keeping the contract 1:1 with `rank_accounts`
  keeps this ticket to one run and the ranking logic a single shared shape.
- **`metric` defaults to `"spend"`** (the ticket's stated default). `order` defaults to `"desc"`,
  `limit` to `10` — same defaults as `rank_accounts`.
- **Result key resolved once per account, applied to every ad in that account.** All ads in an account
  share the account's goal, so resolve the key at the account level and reuse it per ad (consistent
  result semantics across an account's ads, and matches how `cross_account_performance` treats an
  account). Mechanism: after reading an account's ad rows, aggregate their `actions` blobs into one
  combined `actions` list and pass that to `_resolve_result_key(ad_account_id, combined_actions,
  registry_by_id)`. Config-configured accounts resolve from config and ignore the aggregate; unconfigured
  accounts infer the key from the pooled actions (so the mock/no-config path still works, same as the
  prereq). Then per ad: if the resolved key is in `_LEAD_KEYS_LOWER`, read `results` via
  `_find_metric(ad_actions, LEAD_KEYS)` (lead-family self-heal); elif a key resolved, `_find_metric(
  ad_actions, [key])`; else `results` absent. `purchase_value` per ad via `_find_metric(ad_action_values,
  PURCHASE_KEYS)`. This is the same three-branch logic as `cross_account_performance:636-647`, applied
  per ad.
- **Account identity (`account_id`, `account_name`, `currency`) comes from the account metadata row**
  (`scope.metadata_by_id[id]` or `reader.get_account(...)`), NOT from the insights row — same source
  the other fan-out tools use. `ad_id`/`ad_name` come from the insights row. Missing `ad_name` → fall
  back to `ad_id` (Meta occasionally returns a blank name).
- **Insights fields**: `["ad_id", "ad_name", "spend", "impressions", "clicks", "actions",
  "action_values"]`. Define as a module constant `DEFAULT_TRIAGE_INSIGHT_FIELDS` next to
  `DEFAULT_PERFORMANCE_INSIGHT_FIELDS`.
- **Money ranking**: for the metrics in `_RANK_MONEY_METRICS`, sort on the row's `*_normalized` twin so
  ads in different currencies compare directly; emit `value` = the normalized figure and `value_native`
  = the native figure (exactly `rank_accounts`). Ratio/count metrics (`ctr, roas, impressions, clicks,
  results`) rank natively (currency-invariant).
- **No `level` parameter.** Triage is inherently ad-level; unlike `cross_account_performance` there is
  no account/campaign/adset choice to make.
- **Do not refactor `rank_accounts`.** It is shipped and tested; duplicating its ~40-line
  partition/sort/rank block into triage (adapted to `ad_id`) is the lower-risk choice for this ticket.
  A shared `_rank_pooled_rows(...)` helper is a legitimate later cleanup but is explicitly out of scope
  — call it out in the review handoff, don't do it here.

## Wiring

- Add `cross_account_creative_triage` to `build_discovery_tools` in `mcp_server.py` (a thin wrapper that
  forwards `date_from, date_to, metric, order, limit, account_ids, reporting_currency` and does **not**
  expose `fx_table` — copy the `rank_accounts` wrapper at `mcp_server.py:624`, including the comment
  that `fx_table` is a test-only seam). Register it in the returned dict.
- Add a `DISCOVERY_TOOL_DESCRIPTIONS["cross_account_creative_triage"]` entry. It must state plainly:
  ranks individual ads across accounts by a chosen metric (top or bottom N); is scoped to ads that
  *delivered* in the window (so it is fast and never walks dormant ads); money metrics normalized to one
  `reporting_currency`; ads lacking the metric (e.g. zero results → no cost-per-result) land in
  `unranked` with a reason; and it is about *performance of ads that ran* — **not** ad health
  (disapproved / not-delivering ads), which is a separate concern.
- **Both discovery-tool test assertions enumerate the full set and must go 8 → 9:**
  `test_build_discovery_tools_exposes_cross_account_summary` (`tests/test_meta_ads_analysis.py:9681`,
  the `set(discovery) == {...}` assertion) and add a
  `"cross_account_creative_triage" in DISCOVERY_TOOL_DESCRIPTIONS` assertion alongside the others there.

## Edge cases & interactions

Name a test for each of these (see test plan):

- **Ad with zero results** → `cost_per_result` absent (never `inf`), so when `metric=cost_per_result`
  that ad goes to `unranked` with `reason="metric unavailable"` — mirror `rank_accounts` exactly.
  Contrast: an ad with `results` but `metric` money-typed in a no-FX currency → `unranked` with
  `reason="no FX rate for <CUR>"`.
- **Cross-currency pooling** → two ads in MXN and EUR ranked by `spend` sort on `spend_normalized`
  (USD), and `value_native` reflects each ad's own currency. Ratio metric (`ctr`) ranks natively and
  is unaffected by FX.
- **Account whose currency is absent from the FX table** → every ad in it keeps native figures, gets
  **one `errors` entry per account** (not per ad — match the granularity: emit the no-FX error once per
  account, keyed by `ad_account_id`, exactly as `cross_account_performance` does), and its money-metric
  ads fall to `unranked` under a money metric. Its ads still rank fine under a ratio/count metric.
- **Lead vs sales/purchase accounts** → result metric resolves per account against the lead family
  (`LEAD_KEYS`) for lead accounts and `PURCHASE_KEYS`/configured key otherwise; a config `primary_result_
  action_type` wins over inference; a stale/drifted lead config key still self-heals via the lead family.
  Reuse the `cross_account_performance` lead-family tests as the model
  (`tests/test_meta_ads_analysis.py:10767`, `:10802`).
- **Account with no delivering ads in the window** → `fetch_insights` returns `[]` → contributes zero
  rows, no error. Not a failure.
- **Partial per-account failure** → a `MetaApiError` for one account lands in `errors` and is skipped;
  never fatal; other accounts still rank. Determinism: identical output regardless of worker completion
  order (assert with a 3-account reader where one raises).
- **Missing `ad_name`** → row's `ad_name` falls back to `ad_id`; `unranked`/`ranked` entries still
  carry a usable label.
- **`limit` larger than the pooled ad count** → `ranked` returns all rankable rows (no padding);
  `ranked_total` reflects the true count.
- **Tie handling** → two ads with the identical sort value share a rank; the next distinct value's rank
  is `index+1` (strictly-better-count + 1); tiebreak by `ad_id` ascending for run-to-run determinism.
- **Validation** → unknown `metric` (after lowercasing + alias lookup) raises `ValueError` listing valid
  names; `order` not in `{asc,desc}` raises; `limit <= 0` raises; `reporting_currency` absent from FX
  raises a whole-call `ValueError` (all identical to `rank_accounts`). A whole-discovery failure (bad
  token in the `account_ids=None` path) propagates unchanged.
- **Explicitly out of scope**: ad *health* (disapproved / active-but-not-delivering). Those ads may have
  zero delivery, so insights never surface them — that is the `flag-ad-health-scan-scale` problem. Do
  not add any `fetch_ads` call to cover them.

## Test plan (add to `tests/test_meta_ads_analysis.py`)

Model the fixtures on the existing `_perf_reader` helper (`tests/test_meta_ads_analysis.py:10097`) and
the `_fx()` table (`:9946`). The reader stubs `list_ad_accounts` + `fetch_insights(level="ad")`, where
`fetch_insights` returns **multiple ad rows** per account. `monkeypatch.setattr(_account_discovery,
"_registry_by_ad_account_id", lambda: {})` for the inference path, and stub it with a fake registry
account for the config-key path. **MOCKS ONLY — no live Meta call.**

Key tests to write:

- `pools_and_ranks_ads_across_accounts_by_spend` — two accounts, 2+2 ads; `metric="spend", order="desc",
  limit=3` → the three highest-spend ads across the pool, correct `rank`, `ad_count == 4`.
- `ranks_ascending_returns_losers` — same fixture, `order="asc"` → the cheapest/worst end.
- `cost_per_result_zero_result_ad_is_unranked` — an ad with results and one with zero results;
  `metric="cost_per_result"` → the zero-result ad in `unranked` with `reason="metric unavailable"`,
  never `inf`.
- `money_metric_ranks_on_normalized_twin` — MXN ad + EUR ad, `metric="spend"` ranks on
  `spend_normalized`; assert `value` is normalized and `value_native` is native.
- `no_fx_currency_ad_is_unranked_and_errored_once` — an account in a currency absent from `_fx()`;
  its ad is `unranked` (`reason` names the currency) under a money metric, exactly **one** `errors`
  entry for that account, and the ad still ranks under `metric="ctr"`.
- `resolves_lead_family_per_account` + `config_result_key_wins_over_inference` — port the
  `cross_account_performance` lead-family / configured-key tests to the ad level.
- `partial_account_failure_isolated_and_deterministic` — 3 accounts, middle one raises `MetaApiError`
  → recorded in `errors`, others ranked; assert output independent of completion order.
- `missing_ad_name_falls_back_to_ad_id`.
- `tie_ranks_share_and_tiebreak_by_ad_id`.
- `invalid_metric_order_limit_and_reporting_currency_raise` — the four validation paths.
- `exposed_in_build_discovery_tools` — update the `set(discovery)` assertion to 9 and add the
  `DISCOVERY_TOOL_DESCRIPTIONS` membership check.

## Validation

- `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/triage_tests.log` (stream — never
  silent-redirect). Run the full file so the two discovery-tool enumeration assertions are exercised.
- Type check per AGENTS.md (whatever the repo uses — `mypy`/`pyright` if configured).

## TODO

- [ ] Add `DEFAULT_TRIAGE_INSIGHT_FIELDS` and `cross_account_creative_triage` to `account_discovery.py`,
      reusing the fan-out, result-key, FX, `compute_derived_metrics`, and `RANK_METRIC_ALIASES` /
      `_RANK_MONEY_METRICS` machinery; ranking logic mirrors `rank_accounts` keyed on `ad_id`.
- [ ] Resolve the result key once per account (config-first, else infer from the account's pooled ad
      `actions`) and apply per ad, including the lead-family self-heal branch.
- [ ] Emit money twins per ad; rank money metrics on the `*_normalized` twin with `value_native`.
- [ ] Wire the wrapper + description into `build_discovery_tools` / `DISCOVERY_TOOL_DESCRIPTIONS`
      (no `fx_table` exposure), and bump the two 8→9 discovery-set assertions.
- [ ] Add the test suite above; run the full test file streamed to a log and type-check.
- [ ] In the review handoff, flag the deliberate deferrals: the shared `_rank_pooled_rows` refactor and
      the one-call `both_ends` winners+losers variant.
