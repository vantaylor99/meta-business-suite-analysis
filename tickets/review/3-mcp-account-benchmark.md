description: A new tool that shows how one ad account compares to its peers — e.g. "is this account's cost-per-lead good or bad?" — by ranking it as a percentile within a comparison group, so a bare number becomes interpretable ("72nd percentile — better than most peers").
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## What landed

A new discovery-surface tool **`account_benchmark`** — the specialist-facing counterpart to
`cross_account_performance`. It answers "how does *this one* account stack up?" by ranking the target
account's efficiency metrics as **percentiles within a cohort** of peers. A high percentile always
means "good" for *both* cost metrics (low CPM ranks high) and quality metrics (high ROAS ranks high),
so the verdict reads the same direction everywhere.

It is a **pure post-processor over `cross_account_performance`** — it re-reads nothing from Meta. It
calls that tool once for the cohort (target always included) and computes percentiles/quartiles over
the per-account rows it already returned. So it inherits, for free: FX normalization into one
`reporting_currency`, Simpson's-paradox-safe derived metrics, per-account partial-failure isolation
and the `errors` list, and the bounded-concurrency fan-out + its determinism. The only new logic is
percentile math and per-metric assembly.

### New / changed code

- **`account_discovery.py`** (changed): added
  - module constants `BENCHMARK_METRIC_DIRECTION` (the 5 benchmarked efficiency metrics →
    `lower_is_better`/`higher_is_better`), `_BENCHMARK_MONEY_METRICS`, `_BENCHMARK_RATIO_METRICS`,
    `MIN_COHORT_FOR_PERCENTILE = 5`;
  - pure helper `quantiles(values, q)` — hand-rolled linear-interpolation quantile(s), no numpy;
    accepts a single `q` (→ float) or a list (→ list); `n==1` → the value; empty → `None`;
  - pure helper `percentile_rank(cohort_values, value, *, higher_is_better)` — the
    `100 * (L + 0.5·E) / N` mid-rank formula, oriented so high = good; `N<1` → `None`;
  - `_benchmark_verdict(pr)` — the 4 verdict bands from the percentile alone;
  - the tool `account_benchmark(reader, *, account_id, date_from, date_to, cohort_ids=None,
    reporting_currency="USD", fx_table=None)`.
  - `import math` added for the quantile floor/ceil.
- **`mcp_server.py`** (changed): added the `DISCOVERY_TOOL_DESCRIPTIONS["account_benchmark"]` entry and
  the `build_discovery_tools` wrapper (exposes `account_id, date_from, date_to, cohort_ids=None,
  reporting_currency="USD"`; `fx_table`/`reader` are NOT exposed to the LLM, matching the prereq). The
  discovery set is now **four** tools.
- **Docs**: `docs/META_API_SETUP.md` (three → four discovery tools + a describing paragraph) and
  `README.md` (a sentence on the "how do I stack up?" counterpart).

### Key design decisions (all per the ticket, called out for the reviewer)

- **Only efficiency metrics are benchmarked** (`cpm`, `cpc`, `cost_per_result`, `ctr`, `roas`). Volume
  metrics (spend/impressions/clicks/results/purchase_value) are deliberately NOT — a "good spend
  percentile" is ambiguous. The target's full `cross_account_performance` row is still returned under
  `account` for context.
- **Value-source per metric:** money metrics read the row's `*_normalized` twin (so all comparisons
  are in `reporting_currency`); ratio metrics read the native value (currency-invariant). A row lacking
  the needed field is dropped from **that metric's** cohort → per-metric `cohort_n` varies and is
  reported.
- **Every metric key is always present** in `benchmarks`: either the full percentile block, or
  `{value, direction, reason}` when it can't be computed (target missing the metric / target in a no-FX
  currency / no peers). A consumer never has to guess whether a metric was skipped.
- **Single FX-table load, shared with the prereq.** `account_benchmark` loads the table once (or takes
  the injected one) and passes it *down* to `cross_account_performance` as `fx_table=`, so the same
  table validates `reporting_currency`, normalizes the rows, AND tells us whether the *target's own*
  currency has a rate. No double load.

## How to validate

- Full file: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **532 passed** (log at
  `scratchpad/benchmark-tests.log`), up from 519 (+13 assertions across 12 new test functions and 2
  updated).
- Focused: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q -k "account_benchmark or
  percentile_rank or quantiles"`.
- Compile: `.venv/bin/python -c "import meta_ads_analysis.account_discovery, meta_ads_analysis.mcp_server"`.
- **`ruff`/`mypy` are not installed in this repo and there is no lint config** — validation is import +
  pytest, matching the prereq's handoff. Python in `.venv` is 3.14; the system `python` is absent (use
  `.venv/bin/python` or `python3`).

## Use cases covered by tests (the floor — a starting point, not a ceiling)

- **Directionality (the ticket's must-have, both signs in one target row):** a cohort where the target
  has the LOWEST `cost_per_result` (a cost) → `percentile >= 75`, `verdict == "better than most peers"`,
  `rank == 1`; the SAME target also has the LOWEST `roas` (a quality ratio) → `percentile < 25`,
  `verdict == "worse than most peers"`, `rank == 5`. An inverted sign on either direction fails this.
- **Percentile-rank helper:** unique best in N=10 → 95.0; unique worst → 5.0; two-way tie → 50.0 each;
  `higher_is_better`/`lower_is_better` invert which end is high; `N<1` → `None`.
- **Quantiles helper:** the `[10,20,30,40]` worked example (17.5 / 25.0 / 32.5); list form; `n==1`
  returns the value; empty → `None`/`[None,...]`.
- **Currency normalization:** a USD target vs. an MXN+EUR cohort ranks on the `*_normalized` money
  values — a peer with a huge NATIVE MXN cpc that normalizes to the cheapest USD reorders the field
  (target is rank 1 on raw native but rank 2 once normalized); asserts the normalized percentile (50.0)
  is NOT the native-based one (~66.7).
- **Tiny cohort:** 3 accounts → `cohort.too_small == true`, per-metric `unreliable == true`, percentile
  still computed, `note == "cohort too small for a meaningful percentile"`.
- **Target missing a metric (both families):** zero clicks → no `cpc` (money) → `{value: null, reason:
  "account missing cpc"}`, no percentile; no results → same for `cost_per_result`; no revenue → same for
  `roas` (ratio). Every metric key still present.
- **No-FX cohort MEMBER (JPY):** dropped from the money cohort (`cpc.cohort_n == 2`) but kept in the
  ratio cohort (`ctr.cohort_n == 3`); surfaced verbatim in `cohort.excluded` AND `errors`.
- **No-FX TARGET (JPY):** money metrics carry `reason: "no FX rate for JPY"`; `ctr`/`roas` still
  benchmark; `account` is the native row (not None).
- **Target forced into an explicit cohort:** `cohort_ids` omits the target → it's force-added → gets a
  row + percentiles; `cohort.count == 5` (union); explicit path uses `get_account`, never
  `list_ad_accounts`.
- **Target unreadable:** target `get_account` raises `MetaApiError` → `account is None`, `note ==
  "target account act_1 could not be read"`, every metric `reason == "target account could not be
  read"`, the prereq error passed through in `errors`.
- **Invalid `reporting_currency`** → `ValueError` propagates (same contract as the prereq).
- **MCP wiring:** discovery set is now `{list_ad_accounts, cross_account_spend_summary,
  cross_account_performance, account_benchmark}`; `"account_benchmark" in DISCOVERY_TOOL_DESCRIPTIONS`;
  registered on the built server; a mock-smoke call through the wired tool returns a well-formed payload
  (single-account cohort → "no peers" for metrics it has, "account missing" for metrics it lacks).

## Known gaps / things for the reviewer to poke at (tests are a floor)

- **`cohort.excluded` mirrors ALL prereq errors, including the target's own.** When the target itself
  is unreadable or in a no-FX currency, its error appears in `cohort.excluded` too — so the *target* can
  show up in a list nominally about *cohort members* excluded. This is deliberate transparency (it is
  also in `errors`), but a reviewer may prefer filtering the target out of `excluded`. Not tested
  either way.
- **`percentile_rank` uses exact float `==`** for the equal-count (`E`) term. Real Meta-derived floats
  rarely tie exactly, so in practice `E` is usually just the target itself (`E>=1`); genuine ties are
  only exercised on hand-constructed equal values. The mid-rank math is correct; the float-equality
  edge is worth a second look.
- **`rank` is competition ranking** (`strictly_better + 1`): tied accounts share a rank and the next
  rank skips. Standard, but not documented in the output shape and not tie-tested at the `account_benchmark`
  level (only in the `percentile_rank` unit test).
- **Quartiles (`p25`/`median`/`p75`) INCLUDE the target** in the distribution — by design ("the honest
  distribution a human reads" contains the target), but a reviewer might argue the target should be
  excluded from its own quartile spread. Not asserted numerically in the e2e tests (only that the block
  is present); worth adding a pinned-value quartile assertion.
- **No dedicated determinism test at the benchmark layer.** Determinism is inherited from the prereq
  (the percentile math runs single-threaded over the already-ordered `accounts` list), and the prereq
  has a reordering test — but there is no `account_benchmark`-level determinism test.
- **`currency == "UNKNOWN"` target** (Meta omitted the field) is treated as no-FX, so money metrics
  would read `reason: "no FX rate for UNKNOWN"`. Sensible fallback, but not directly tested.
- **Empty explicit `cohort_ids=[]`** degenerates to a target-only cohort (every metric → "no peers").
  Benign and untested.
- **Mocks only, no live path** (per the ticket). The tool consults the *real* gitignored
  `config/meta_ads_accounts.json` for the result key on a live box (tests monkeypatch it), same as the
  prereq.
