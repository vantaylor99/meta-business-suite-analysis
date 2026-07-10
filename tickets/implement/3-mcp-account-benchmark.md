description: Show how one ad account stacks up against its peers — e.g. "is this account's cost-per-lead good or bad compared to the others?" — by ranking it as a percentile within a comparison group.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

A raw number (CPL = $18) means nothing without context. A specialist's real question is "is that
good *for an account like mine*?" This tool answers by placing one account's efficiency metrics as
**percentiles against a cohort** (default = all accounts the token reaches), so a small number
becomes interpretable ("72nd percentile — better than most peers"). It is the specialist-facing
counterpart to the manager-facing `cross_account_performance` ranking view: same underlying metric
rows, inverted point of view (one account vs. the field, not the field ranked).

## Where it lives, and why (mirrors the prereq)

`account_benchmark` is a **pure post-processor over `cross_account_performance`**. It does NOT re-read
Meta or re-derive metrics: it calls `cross_account_performance` once for the cohort (target account
included), then computes percentiles/quartiles over the per-account rows that call already returned.
This is deliberate — it inherits, for free and without duplication:

- FX normalization into one `reporting_currency` (money metrics are compared via each row's
  `*_normalized` twin, so a USD account benchmarks correctly against a peer set in other currencies);
- Simpson's-paradox-safe derived metrics (recomputed-from-components, never averaged ratios);
- per-account partial-failure isolation and the `errors` list (unreadable account / no-FX currency);
- the bounded-concurrency fan-out and its determinism.

So the new code is only: (1) two small **pure, unit-testable** stats helpers, (2) a `account_benchmark`
library function in `account_discovery.py` that orchestrates the one prereq call + the percentile
assembly, and (3) a 4th discovery-tool wiring in `mcp_server.py`. It carries no Meta/business logic of
its own beyond percentile math — same thin-frontend discipline as the rest of the module.

Reads stay **open to every reachable account** (no registry gate), consistent with
[[reads-open-writes-config-scoped]] and the other discovery tools.

## Interface (finalized)

Library (in `src/meta_ads_analysis/account_discovery.py`):

```python
def account_benchmark(
    reader: "MetaReaderProvider",
    *,
    account_id: str,
    date_from: str,
    date_to: str,
    cohort_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    fx_table: FxTable | None = None,   # test-injection seam, NOT exposed to the LLM (as in the prereq)
) -> dict[str, Any]: ...
```

MCP discovery tool (in `mcp_server.build_discovery_tools`, 4th entry) — same params minus
`fx_table`/`reader`:

```python
def account_benchmark(
    account_id: str,
    date_from: str,
    date_to: str,
    cohort_ids: list[str] | None = None,
    reporting_currency: str = "USD",
) -> dict[str, Any]: ...
```

### Benchmarked metric set + directionality (module constants)

Only **efficiency** metrics are benchmarked. Volume metrics (`spend`, `impressions`, `clicks`,
`results`, `purchase_value`) are NOT — "is my spend in a good percentile?" is ambiguous (volume, not
efficiency). The target's full row is still returned under `account` for context.

```python
# metric -> "lower_is_better" (cost) | "higher_is_better" (quality)
BENCHMARK_METRIC_DIRECTION: dict[str, str] = {
    "cpm": "lower_is_better",
    "cpc": "lower_is_better",
    "cost_per_result": "lower_is_better",
    "ctr": "higher_is_better",
    "roas": "higher_is_better",
}
# money metrics compared via the row's reporting-currency twin; ratios are currency-invariant (native).
_BENCHMARK_MONEY_METRICS: frozenset[str] = frozenset({"cpm", "cpc", "cost_per_result"})
_BENCHMARK_RATIO_METRICS: frozenset[str] = frozenset({"ctr", "roas"})

MIN_COHORT_FOR_PERCENTILE: int = 5   # documented reliability floor
```

Value-source rule per metric: for a money metric read `f"{metric}_normalized"` from each
`cross_account_performance` row (so all comparisons are in `reporting_currency`); for a ratio metric
read the native `metric`. A row lacking the needed field is excluded from **that metric's** cohort
(per-metric N therefore varies — report it).

### Percentile & verdict semantics (orient so higher = better, for BOTH directions)

Percentile is oriented so **a high percentile always means "good"** — a low CPL and a high ROAS both
land in a high percentile. This makes the verdict a pure function of the percentile and directly
serves the use case ("72nd percentile — better than most").

Definition — **percentile rank** over the metric's cohort values (target INCLUDED), N = cohort size
for that metric:

```
pr = 100 * (L + 0.5 * E) / N
  higher_is_better:  L = count of cohort values strictly LESS than target;    E = count EQUAL to target (incl. self)
  lower_is_better:   L = count of cohort values strictly GREATER than target;  E = count EQUAL to target (incl. self)
```

Properties this guarantees (assert in tests): unique best in N=10 → `pr = 95.0`; unique worst → `5.0`;
exact median → ~`50`. Also emit `rank` (1 = best) and `rank_of` (= N).

Verdict bands (from `pr` only, since direction is already baked in):

```
pr >= 75  -> "better than most peers"
50 <= pr < 75 -> "above the cohort median"
25 <= pr < 50 -> "below the cohort median"
pr < 25   -> "worse than most peers"
```

Quartiles per metric (`p25`, `median`, `p75`) are on the **raw metric values in reporting currency**
(NOT oriented) — the honest distribution a human reads. Use a hand-rolled linear-interpolation
quantile (no numpy dep): sort ascending, `rank = q * (n - 1)`, interpolate between the two straddling
order statistics; `n == 1` → the single value for all three. Worked example to pin the test:
`quantiles([10, 20, 30, 40], 0.25) == 17.5`, `median == 25.0`, `0.75 == 32.5`.

### Output shape (finalized)

```python
{
  "account_id": "act_123",              # normalized target
  "date_from": "...", "date_to": "...",
  "reporting_currency": "USD",
  "fx_as_of": "...", "fx_note": "...",  # passed through from cross_account_performance
  "account": { ...target's full cross_account_performance row... },  # None if target unreadable
  "cohort": {
      "count": 12,          # accounts resolved into the cohort (attempted, incl. target)
      "read_ok": 11,        # rows successfully read (len of cross_account_performance "accounts")
      "excluded": [ {"ad_account_id": "...", "reason": "..."} ],  # from the prereq's errors (no FX / unreadable)
      "too_small": false,   # read_ok < MIN_COHORT_FOR_PERCENTILE
      "min_for_percentile": 5,
  },
  "benchmarks": {           # EVERY key in BENCHMARK_METRIC_DIRECTION is present
      "cost_per_result": {
          "value": 18.0,                 # target's value in reporting currency (normalized twin for money)
          "direction": "lower_is_better",
          "cohort_n": 10,                # accounts with a valid value for THIS metric (incl. target)
          "percentile": 72.0,            # oriented: higher = better
          "rank": 3, "rank_of": 10,      # 1 = best
          "median": 25.0, "p25": 20.0, "p75": 31.0,
          "verdict": "better than most peers",
          "unreliable": false,           # true when cohort_n < MIN_COHORT_FOR_PERCENTILE (still computed)
      },
      "cpc": { "value": null, "direction": "lower_is_better", "reason": "account missing cpc" },
      # ...cpm, ctr, roas...
  },
  "errors": [ ... ],        # passthrough of cross_account_performance errors + any benchmark-level note
  "note": "...",            # present only for a whole-result caveat (see edge cases)
}
```

Rule: a `benchmarks[metric]` entry either carries the full percentile block, or `{value, direction,
reason}` when it cannot be computed (target missing the metric, or `cohort_n < 2` = no peers). Every
metric key is always present so a consumer never has to guess whether a metric was skipped.

## Cohort resolution

- `cohort_ids is None` → cohort is the whole reach: call `cross_account_performance(..., account_ids=None)`
  (one discovery + fan-out). The reachable target is naturally included. If the target is NOT among the
  reachable rows, return with `account: None` and `note: "target account <id> not found in cohort"`.
- `cohort_ids` given → call `cross_account_performance(..., account_ids=dedup(cohort_ids + [account_id]))`.
  The target is force-added if absent (decision below). `resolve_scope` already normalizes + dedups, so
  passing a redundant target id is safe.

## Edge cases & interactions

- **Target not in an explicit cohort → include it anyway** (decision, per ticket): the benchmark is
  still valid and the specialist's own account must be in its own comparison. Add it to the id list;
  do NOT error. Document in the docstring.
- **Tiny cohort** (`read_ok < MIN_COHORT_FOR_PERCENTILE` = 5) → still return raw comparison AND
  percentiles, but set `cohort.too_small = true`, mark each computed metric `unreliable: true`, and add
  `note: "cohort too small for a meaningful percentile"`. Do not suppress the numbers — flag them.
- **No peers for a metric** (`cohort_n < 2`, i.e. only the target has a value) → that metric's entry is
  `{value, direction, reason: "no peers with <metric> in cohort"}`; no percentile/rank/quartiles.
- **Target missing a metric** (Meta returned nothing → field absent on its row) → `{value: null,
  direction, reason: "account missing <metric>"}`. A test MUST cover this per metric family.
- **Direction correctness** — a LOW cost yields a HIGH percentile + "better than most peers"; a LOW
  ROAS yields a LOW percentile + "worse than most peers". Assert BOTH so an inverted sign is caught.
- **Currency exclusions surfaced, not silent**: a cohort member in a no-FX currency has no
  `*_normalized` twin → excluded from money-metric cohorts AND already recorded in
  `cross_account_performance` `errors`; surface those verbatim under `cohort.excluded` + `errors`.
  If the TARGET itself is in a no-FX currency, its money metrics get `reason: "no FX rate for
  <currency>"` (ratio metrics ctr/roas still benchmark, being currency-invariant).
- **Target unreadable** (appears in `cross_account_performance.errors`, no row) → `account: None`,
  `benchmarks` all carry `reason: "target account could not be read"`, top-level
  `note: "target account <id> could not be read"`, and the prereq error is passed through in `errors`.
- **Invalid `reporting_currency`** (absent from FX table) → `cross_account_performance` raises
  `ValueError`; let it propagate (whole-call failure, mapped to `ToolError` by the tool layer). Same
  contract as the prereq — do not swallow.
- **`account_id` normalization**: accept bare numeric or `act_` form; normalize via
  `account_registry._normalize_ad_account_id` before matching against the (also-normalized) rows, so
  `"1"` and `"act_1"` both resolve to the same target row.
- **Determinism**: output must be identical regardless of fan-out completion order (inherited from the
  prereq; the percentile math runs on the already-ordered `accounts` list on the main thread).
- **Ties**: equal values share a mid-rank via the `+ 0.5 * E` term — assert a two-account tie gives
  both `pr == 50.0`.

## Tests to add (extend `tests/test_meta_ads_analysis.py`; MOCKS ONLY, no live call)

Reuse the existing `_perf_reader(accounts, insights, **overrides)` and `_fx(**extra_rates)` helpers
(same section as the `cross_account_performance` tests, ~line 10053) — they already build a
`FakeMetaReader` and inject a test FX table.

Pure helpers:
- `percentile_rank` — unique best in N=10 → 95.0; unique worst → 5.0; two-way tie → 50.0 each;
  `higher_is_better` vs `lower_is_better` invert which end is the high percentile.
- `quantiles` — the `[10,20,30,40]` worked example above; `n==1` returns the value for p25/median/p75.

`account_benchmark` end-to-end (mock reader):
- **Direction (the ticket's must-have)**: a cohort where the target has the LOWEST `cost_per_result`
  → `percentile >= 75` and `verdict == "better than most peers"`; a target with the LOWEST `roas`
  → low percentile + `"worse than most peers"`.
- **Currency normalization**: a USD target benchmarked against an MXN + EUR cohort ranks on the
  `*_normalized` money values (assert the ranking flips vs. comparing raw native numbers).
- **Tiny cohort**: 3 accounts → `cohort.too_small == true`, metrics `unreliable == true`, `note` set.
- **Target missing a metric** → that metric entry has `value: null` + a `reason`, no `percentile`.
- **No-FX cohort member excluded + surfaced** in `cohort.excluded` / `errors`; ratio metrics still
  benchmark. No-FX TARGET → money metrics carry a reason, ctr/roas still computed.
- **Target not in explicit `cohort_ids` is included anyway** (pass a cohort list without the target;
  assert the target still gets a row + percentiles and `cohort.count` reflects the union).
- **Target unreadable** (inject a reader whose target `fetch_insights`/`get_account` raises
  `MetaApiError`) → `account is None`, `note` set, benchmarks carry the read-failure reason.
- **Invalid `reporting_currency`** → `ValueError` (mirror the prereq test).

MCP wiring:
- Extend `test_build_discovery_tools_exposes_cross_account_summary` (line ~9676): the discovery set is
  now `{"list_ad_accounts", "cross_account_spend_summary", "cross_account_performance",
  "account_benchmark"}`; assert `"account_benchmark" in DISCOVERY_TOOL_DESCRIPTIONS`.
- Grep for any other test asserting the discovery-tool set/count (e.g. `discovery_names` around line
  10669) and update it to include the 4th tool. Also the four-tool assertion at line ~9680.
- A mock-smoke test that calls the wired `account_benchmark` discovery tool via
  `build_discovery_tools(reader)["account_benchmark"](...)` and gets a well-formed payload.

## Docs to update

- `docs/META_API_SETUP.md` (~line 261 + the `cross_account_performance` paragraph ~272): change "three
  discovery tools" → "four", add `account_benchmark` to the list, and add a short paragraph describing
  it (one account's efficiency as percentiles within a cohort; higher percentile = better for both cost
  and quality metrics; money compared in `reporting_currency`; cohort size + exclusions surfaced;
  `MIN_COHORT_FOR_PERCENTILE` reliability floor).
- `README.md` (~line 44–58 discovery-tools bullet): append a sentence describing `account_benchmark`
  as the specialist-facing "how do I stack up?" counterpart to `cross_account_performance`.

## TODO

### Phase 1 — pure stats helpers
- Add `quantiles(values, q_or_qs)` (or a `p25`/`median`/`p75` triple helper) with linear
  interpolation, `n==1` guard, and empty-list guard, to `account_discovery.py`.
- Add `percentile_rank(cohort_values, value, *, higher_is_better)` implementing the `(L + 0.5E)/N`
  formula; guard `N < 1`.
- Add the module constants: `BENCHMARK_METRIC_DIRECTION`, `_BENCHMARK_MONEY_METRICS`,
  `_BENCHMARK_RATIO_METRICS`, `MIN_COHORT_FOR_PERCENTILE`.
- Unit tests for both helpers (worked examples above).

### Phase 2 — account_benchmark library function
- Implement `account_benchmark` in `account_discovery.py`: normalize `account_id`; resolve cohort id
  list; single `cross_account_performance` call; locate the target row; build `cohort` block from the
  returned `accounts` + `errors`; per metric, gather the metric's cohort values (correct field per
  money/ratio), compute value/percentile/rank/quartiles/verdict or the `reason` fallback; assemble the
  output shape above.
- Handle every edge case in the section above (target missing / unreadable / no-FX; tiny cohort; no
  peers; ties; determinism).
- End-to-end mock tests listed above.

### Phase 3 — MCP wiring + docs
- Add the `account_benchmark` wrapper to `build_discovery_tools` (drop `reader`/`fx_table`) and an
  entry in `DISCOVERY_TOOL_DESCRIPTIONS`.
- Update the discovery-set tests (4 tools) and add the wiring mock-smoke test.
- Update `docs/META_API_SETUP.md` and `README.md`.

### Validation
- `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/benchmark-tests.log`
  (stream output; do not silent-redirect). Also run any ruff/type check the repo uses (check
  `AGENTS.md`). Fix anything your diff caused; flag genuinely pre-existing failures per the runner's
  `.pre-existing-error.md` protocol.
