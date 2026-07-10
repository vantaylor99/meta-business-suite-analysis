description: The "summarize spend across all my ad accounts" feature no longer times out on hundreds of accounts — it now reads them in parallel instead of one at a time, with a shared, reusable way for any future multi-account tool to say which accounts a request covers.
prereq:
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----

## What shipped

An **engine swap under the existing contract** for `cross_account_spend_summary`: the sequential
per-account fan-out is replaced by a **bounded concurrent thread-pool fan-out**, and a shared
scope-resolution seam (`resolve_scope` / `ResolvedScope`) is introduced for every future
multi-account tool. Every field the tool returned before is byte-for-byte unchanged for the same
inputs — all 13 pre-existing `test_cross_account_summary_*` tests pass **unchanged**.

All changes are in one module, `src/meta_ads_analysis/account_discovery.py`:

- **`ResolvedScope`** (frozen dataclass): `account_ids: list[str]` (normalized `act_<id>`,
  de-duplicated, order-preserving), `metadata_by_id: dict[str, dict]` (prefetched `/me/adaccounts`
  rows when discovered, `{}` when explicit), `requested_all: bool` (True iff `account_ids=None`).
- **`resolve_scope(reader, account_ids=None)`** — the one seam. `None` → `list_ad_accounts` once
  (discovery-level `MetaApiError` still propagates as a whole-call failure), keeps each row's
  metadata, `requested_all=True`. Explicit list → normalize + order-preserving dedup, `{}` metadata,
  `requested_all=False`. **When a real grouping layer (`mcp-role-based-access-tiers`) lands, only this
  function changes.**
- **`fan_out_accounts(read_one, account_ids, *, max_workers=None)`** — generic bounded-concurrency
  map. Returns one `(ad_account_id, result_or_None, error_str_or_None)` tuple per id **in input
  order** regardless of completion order (index-disjoint writes into a pre-sized list, reassembled
  on the main thread). A per-account `MetaApiError` → the error slot; any other exception propagates
  (re-raised via `future.result()` in the `as_completed` loop). Empty input → `[]` with **no pool
  constructed**. Workers clamped to `min(resolved, len(account_ids))`.
- **`fanout_max_workers_from_env()`** + `FANOUT_MAX_WORKERS_ENV` (`META_FANOUT_MAX_WORKERS`) +
  `DEFAULT_FANOUT_MAX_WORKERS = 8` — token-free, reads the env var, defaults to 8, clamps to
  `[1, 32]`, never raises on garbage (mirrors `reader_backend_from_env`).
- **Rewired `cross_account_spend_summary`** — `resolve_scope` → `fan_out_accounts(read_one, ...)` →
  main-thread assembly of `accounts` / `errors` / `totals_by_currency`. `read_one` pulls metadata
  from `scope.metadata_by_id` on the discovery path (so it **never calls `get_account`** there) and
  falls back to `reader.get_account` on the explicit path. `reachable_count == account_count ==
  len(scope.account_ids)`; `note="no accounts reachable"` iff `scope.requested_all and not
  scope.account_ids`. `_parse_metric`, `_ad_account_id_from_row`, `normalize_ad_account` untouched.

Docs: `README.md` discovery bullet + `docs/META_API_SETUP.md` tool description now note the
concurrent fan-out and the `META_FANOUT_MAX_WORKERS` env var (default 8, clamp 1–32).

## Why threads (design rationale, already settled)

The reader (`MetaReaderProvider`) is synchronous and reads are I/O-bound HTTP GETs whose socket wait
releases the GIL, so a `ThreadPoolExecutor` gives real parallelism with no async rewrite. The shared
`requests.Session` is safe for concurrent GETs; the client's own 429/5xx back-off
(`meta_api._get_json`) runs independently per worker thread — only an exhausted retry reaches
`errors`. **No new locking is added in `src/`** (index-disjoint result writes; the discovery path's
`metadata_by_id` / `fields` are read-only in workers). See the `META_FANOUT_MAX_WORKERS` env for the
one operator knob.

## How to validate

Run the focused set, then the full file:

```
.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -k "cross_account or resolve_scope or fan_out or fanout" -q
.venv/bin/python -m pytest tests/ -q
```

Result at handoff: **focused 23 passed; full suite 497 passed** (Python 3.14.6 / pytest 9.1.1 in
`.venv`). The concurrency test was run 3× with no flakiness.

New tests added (alongside the unchanged existing block):
- `test_resolve_scope_discovery_returns_ids_metadata_and_flag` — discovery path shape + metadata +
  `requested_all`, and that `get_account` is never consulted.
- `test_resolve_scope_explicit_normalizes_and_dedups` — `["1","act_1","2"]` → `["act_1","act_2"]`,
  `{}` metadata, `requested_all False`, zero reader calls.
- `test_fan_out_accounts_preserves_input_order` — later ids finish first; results stay in input order.
- `test_fan_out_accounts_runs_concurrently_and_bounded` — `Barrier(4, timeout=5)` proves overlap
  (`max_seen > 1`, i.e. not serial) and a lock-guarded high-water mark proves the bound
  (`max_seen <= 4`); the timeout makes a regression to serial fail loudly instead of hanging.
- `test_fan_out_accounts_empty_input_short_circuits` — `[]` and `read_one` never invoked.
- `test_fan_out_accounts_meta_error_recorded_not_raised` — per-account `MetaApiError` → error slot.
- `test_fan_out_accounts_non_meta_error_propagates` — a `ValueError` is NOT swallowed.
- `test_fanout_max_workers_from_env_default_and_clamps` — default 8; `"1"/"32"/"999"/"0"/"garbage"`
  → clamp/default (via `monkeypatch.setenv`).
- `test_cross_account_summary_deterministic_under_reordering` — 5 accounts / 2 currencies with
  reordering delays: `accounts` in scope order, `totals_by_currency` byte-identical to the sequential
  expectation.
- `test_cross_account_summary_discovery_never_calls_get_account` — `get_account` left unstubbed as a
  built-in regression guard.

Manual scale smoke (not committed): 200 fake accounts × a 10 ms read completed in ~0.31 s (vs ~2.0 s
serial) with default 8 workers, order preserved, subtotals correct — confirms the ~792-account
all-accounts call now finishes in tens of seconds instead of timing out (MCP `-32001`).

## Use cases the reviewer should exercise / probe

- **The whole reason this ticket exists:** an all-accounts call at fleet scale (token reaches ~792
  accounts) must complete well under the client timeout. There is deliberately **no cap / no
  truncation** — every resolved account is covered. If a cap is ever wanted it must emit an explicit
  `truncated`/`omitted` signal and be logged; do not add a silent one here.
- **Determinism regardless of completion order** — the core correctness risk of the concurrency
  swap. `accounts` order and per-currency subtotals must match the sequential result for identical
  inputs.
- **Per-account failure isolation** — one account's `MetaApiError` lands in `errors`, others still
  return; a non-`MetaApiError` (a real bug) must surface, not be swallowed.
- **Discovery path must not double reads** — it uses prefetched metadata (`1 + N` reads), never
  `1 + 2N` via `get_account`.

## Honest gaps / things I did not do (treat as a floor)

- **No live/integration test** — MOCKS ONLY per repo rule; every test seeds a `FakeMetaReader`. Real
  Session thread-safety and the real 429 back-off interleaving under concurrency are argued from the
  code (shared urllib3 pool; per-thread `time.sleep`), **not** exercised against live Meta. A human
  should confirm the real all-accounts call once against the live token.
- **`FakeMetaReader.calls` ordering is nondeterministic under threads** — `list.append` is atomic
  under the GIL so the list won't corrupt, but call *order* is not stable. New and existing tests
  assert on call **sets/membership/counts**, never `reader.calls` order. A reviewer adding tests must
  keep that discipline; an order-sensitive assertion would be flaky, not wrong-once.
- **Concurrency test timing** — the bounded/concurrent test uses a `Barrier` (robust) but the
  order-preservation and determinism tests use small `time.sleep` skews (≤30 ms). They passed 3×
  with margin, but they are timing-shaped by nature; if CI is extremely slow they are the first
  candidates to watch. No `pytest-timeout` plugin is installed, so a true deadlock would rely on the
  runner's idle timeout — the `Barrier` timeout guards the one test that could otherwise hang.
- **`_parse_metric` int/float inference unchanged** — carried over as-is (whole-number spend parses
  to `int`); values are always numerically correct. Out of scope for this ticket.
- **No linter run** — the repo declares only `pytest` as a dev dep; `ruff`/`mypy` are not installed
  and there is no config for them. Verified via `py_compile` + full test run instead.
- **`resolve_scope`/`fan_out_accounts` have no downstream callers yet** besides
  `cross_account_spend_summary`. They are built as the shared seam for the tools named in the plan
  (`mcp-role-based-access-tiers` and siblings) but are currently proven only through this one caller
  plus their own unit tests.

## No pre-existing failures

No `.pre-existing-error.md` was written — the full suite was green before and after this change.
