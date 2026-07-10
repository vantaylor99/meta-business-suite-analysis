description: Make the "summarize spend across all my ad accounts" feature return instead of timing out when the token reaches hundreds of accounts, and give every future multi-account tool one shared way to say which accounts a request covers.
prereq:
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/meta_api.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

`account_discovery.cross_account_spend_summary` fans out per-account reads **sequentially**. With
no `account_ids` it targets **every** reachable account; the token reaches ~792 accounts, and the
all-accounts call **times out** (observed MCP `-32001`). It only returns when handed a small explicit
list (~20). This ticket swaps the sequential engine for a **bounded concurrent fan-out** and
introduces the shared `resolve_scope` seam every future multi-account tool will call.

This is an **engine swap under the existing contract**, not a shape change: every field
`cross_account_spend_summary` returns today (`date_from`/`date_to`, per-account `accounts` rows,
`totals_by_currency` grouped per currency with no cross-currency grand total, `errors`,
`account_count`, `reachable_count`, and the `note="no accounts reachable"` case) stays byte-for-byte
the same for the same inputs. All existing `test_cross_account_summary_*` tests must pass unchanged.

## Investigation results (design is settled — build to this)

- **No concurrency exists in the repo today.** The reader interface (`MetaReaderProvider`) is
  synchronous. Reads are I/O-bound HTTP calls.
- **`MetaMarketingApiClient` is safe to call concurrently.** It holds one shared
  `requests.Session` (`meta_api.py:96`), whose urllib3 connection pool is thread-safe for concurrent
  GETs. Its 429/5xx retry (`_get_json`, `meta_api.py:388`) is a **per-call blocking**
  `time.sleep(2**attempt)` loop — each worker thread backs off independently, which is exactly the
  "cooperate with the existing 429 retry" the plan asked for. The GIL is released during the socket
  wait, so threads give real I/O parallelism here.
- **Therefore: use `concurrent.futures.ThreadPoolExecutor`.** Threads, not asyncio — the reader is
  synchronous and rewriting it async would ripple through every call site for no benefit on an
  I/O-bound workload.
- **The discovery path must keep its metadata prefetch.** Today `account_ids=None` costs `1`
  (`list_ad_accounts`) `+ N` (`fetch_insights`) reads, because `list_ad_accounts` already returns
  each account's `currency`/`name`/`account_status`. The explicit path costs `N` (`get_account`)
  `+ N` (`fetch_insights`). If `resolve_scope` returned only ids and the worker always called
  `get_account`, the discovery path would jump to `1 + 2N` reads — doubling the load on the exact
  hot path this ticket exists to speed up, and introducing a new per-account failure mode (an
  account you can *list* but whose `get_account` errors would newly land in `errors`). **This is
  why `resolve_scope` returns prefetched metadata, not just ids (see below).**

## Design

### The shared scope seam

Finalize the plan's tentative `resolve_scope(...) -> list[str]` as a small frozen dataclass so the
one seam can carry the metadata the discovery path already paid to fetch. This supersedes the
`-> list[str]` sketch in the plan ticket; the reason is the read-doubling analysis above.

```python
@dataclass(frozen=True)
class ResolvedScope:
    account_ids: list[str]              # normalized act_<id>, de-duplicated, order-preserving
    metadata_by_id: dict[str, dict]     # prefetched /me/adaccounts rows when discovered; {} when explicit
    requested_all: bool                 # True when the account_ids arg was None

def resolve_scope(
    reader: "MetaReaderProvider", account_ids: list[str] | None = None
) -> ResolvedScope: ...
```

- `account_ids=None`: call `list_ad_accounts(reader)` **once** (may raise `MetaApiError` →
  whole-call failure, unchanged). Derive `account_ids` via `_ad_account_id_from_row` for each row,
  de-duplicated order-preserving; build `metadata_by_id={id: normalized_row}`; `requested_all=True`.
- `account_ids` given: normalize each via `account_registry._normalize_ad_account_id` and
  de-duplicate order-preserving (so `["1", "act_1"]` collapses to one — keep this fixed);
  `metadata_by_id={}`; `requested_all=False`.

Every future multi-account tool calls `resolve_scope` and reads `.account_ids`. When a real grouping
layer (`mcp-role-based-access-tiers`) lands, it changes only this function. `.metadata_by_id` is a
free optimization for tools that need per-account metadata; a grouping layer that has no prefetched
rows simply returns `{}` and callers fetch on demand.

### The fan-out helper

A generic bounded-concurrency map, reusable by the downstream tools named in the plan:

```python
DEFAULT_FANOUT_MAX_WORKERS = 8

def fanout_max_workers_from_env() -> int:
    """Token-free; reads META_FANOUT_MAX_WORKERS, defaults to DEFAULT_FANOUT_MAX_WORKERS,
    clamps to [1, 32]. Mirrors reader_backend_from_env: never raises on a bad value."""

def fan_out_accounts(
    read_one: Callable[[str], Any],
    account_ids: list[str],
    *,
    max_workers: int | None = None,
) -> list[tuple[str, Any | None, str | None]]:
    """Map read_one(ad_account_id) over account_ids with bounded concurrency.

    Returns one tuple per input id, IN INPUT ORDER: (ad_account_id, result_or_None, error_str_or_None).
    A per-account MetaApiError is caught and returned as its str in the third slot (result None);
    any other exception propagates (a real bug must not be silently swallowed). An empty
    account_ids returns [] WITHOUT constructing a pool (ThreadPoolExecutor requires max_workers>=1).
    """
```

- Determinism: submit each id with its index; collect into a pre-sized list by index; return in
  input order. The caller then builds `accounts`/`errors`/`totals_by_currency` by iterating that
  ordered list on the **main thread**, so output never depends on which worker finished first.
- `max_workers` resolves to `fanout_max_workers_from_env()` when `None`, then is clamped to
  `min(resolved, len(account_ids))` (never spin more workers than accounts).
- Only `MetaApiError` is caught per account — a discovery-level `MetaApiError` from
  `list_ad_accounts` still surfaces earlier inside `resolve_scope` as a whole-call failure.

### Rewired `cross_account_spend_summary`

```
scope = resolve_scope(reader, account_ids)
def read_one(ad_account_id):
    meta_row = scope.metadata_by_id.get(ad_account_id)
    if meta_row is None:                       # explicit path: fetch metadata on demand
        meta_row = normalize_ad_account(reader.get_account(ad_account_id, fields=DEFAULT_AD_ACCOUNT_FIELDS))
    insight_rows = reader.fetch_insights(ad_account_id, fields=fields, date_from=..., date_to=...,
                                         level="account", time_increment="all_days")
    return (meta_row, insight_rows)
results = fan_out_accounts(read_one, scope.account_ids)
# main thread, in scope order: build accounts[], errors[], totals_by_currency{} exactly as today
```

- `reachable_count = account_count = len(scope.account_ids)` (attempted); `len(accounts)` =
  succeeded — the attempted-vs-succeeded distinction the plan requires.
- `note="no accounts reachable"` is set **iff** `scope.requested_all and not scope.account_ids`.
- Per-account subtotal accumulation (`_parse_metric`, per-currency `setdefault`, `account_count`
  increment, omit-blank-per-row-but-count-zero-in-subtotal) is unchanged — just moved to run over
  the ordered results instead of inline in the fan-out loop.

### No truncation

There is **no silent cap**: the fan-out covers every resolved account. Bounded concurrency is what
makes 792 accounts tractable (≈`ceil(792/8)` batches of one `fetch_insights` each, vs. 792 serial
calls). Do **not** add a hard ceiling in this ticket. If one is ever required, it must emit an
explicit truncation signal in the result (a `truncated`/`omitted` field) and be `log()`-ged — file
that separately rather than dropping accounts silently here.

## Edge cases & interactions

- **Very large scope (700+ accounts):** must complete well under the client timeout. Default 8
  workers turns the observed multi-minute serial run into tens of seconds; `META_FANOUT_MAX_WORKERS`
  lets an operator (e.g. the WWFT over ~200 managed accounts) tune up. Document the env var.
- **Mixed currencies across the fleet:** subtotals stay grouped by currency; never summed across.
  A missing/blank `currency` still groups under `"UNKNOWN"` (existing behavior).
- **Per-account 429 / transient / permission error:** caught as `MetaApiError` inside the worker,
  recorded in `errors` (`{"ad_account_id", "error"}`), other accounts still return. The client's
  own retry runs first inside the worker thread; only an exhausted retry reaches `errors`.
- **Duplicate ids in an explicit list (`["1","act_1"]`):** de-duped in `resolve_scope`, fanned out
  and subtotaled exactly once. Keep the existing regression test green.
- **Determinism regardless of completion order:** `accounts` rows appear in scope order and
  per-currency subtotals are identical to the sequential result for the same inputs — enforced by
  index-ordered reassembly on the main thread.
- **`account_count` vs `len(accounts)`** still distinguishes attempted vs. succeeded; `reachable_count`
  equals `account_count` (both = resolved scope size), as today.
- **Empty scope:** `fan_out_accounts([])` short-circuits (no pool); discovery-empty still yields the
  `note`; explicit-empty yields no note (matches `test_cross_account_summary_empty_reach_returns_note`
  and the explicit-empty behavior).
- **Discovery path must NOT call `get_account`:** metadata comes from the prefetched
  `scope.metadata_by_id`. Assert `list_ad_accounts` is called once and `get_account` zero times in
  the discovery path (a `FakeMetaReader` with `get_account` unstubbed would raise if touched — a
  built-in regression guard).
- **Thread-safety of the reader:** `DirectMetaReader`/`MetaMarketingApiClient` share one
  `requests.Session` (safe for concurrent GETs). No new locking needed in `src/`. **`FakeMetaReader`
  is a different story:** its `self.calls.append(...)` runs from worker threads. CPython
  `list.append` is atomic under the GIL so the list won't corrupt, but **call ORDER is
  nondeterministic** — tests must assert on call *sets/membership/counts*, never on `reader.calls`
  ordering (the existing tests already do; keep it that way for new tests).
- **Mock reader path (`build_mock_reader`)** must still make zero live calls and produce
  deterministic output. The thread pool only ever invokes the fake stubs; output determinism holds
  via ordered reassembly. No change to `build_mock_reader` needed.
- **MCP surface unchanged:** `build_discovery_tools`' `cross_account_spend_summary` wrapper keeps its
  signature `(date_from, date_to, account_ids=None)`; only the engine underneath changes.

## Tests (write these; TDD-style expectations)

Add alongside the existing `test_cross_account_summary_*` block in
`tests/test_meta_ads_analysis.py`. Existing tests stay unchanged and must remain green.

- **`resolve_scope` — discovery:** `None` → ids from `list_ad_accounts` in row order,
  `metadata_by_id` keyed by normalized id, `requested_all is True`.
- **`resolve_scope` — explicit + dedup:** `["1","act_1","2"]` → `["act_1","act_2"]`,
  `metadata_by_id == {}`, `requested_all is False`.
- **`fan_out_accounts` preserves input order** even when `read_one` returns out of arrival order
  (e.g. a stub that sleeps ~`(N-index)*small` so later ids finish first) → result tuples still in
  input order.
- **`fan_out_accounts` runs concurrently and bounded:** a stub that increments a lock-guarded
  counter, records the max concurrent, and briefly sleeps. With `max_workers=4` over ≥8 ids, assert
  observed max concurrency is `> 1` (proves it is not serial) and `<= 4` (proves the bound). Keep
  sleeps tiny (≤50ms) so the test stays fast; guard any barrier with a timeout so a regression to
  serial fails loudly instead of hanging.
- **`fan_out_accounts` empty input** → `[]`, and (assert) no pool work / no `read_one` calls.
- **`fan_out_accounts` non-MetaApiError propagates** (e.g. `read_one` raises `ValueError`) — not
  swallowed into the error slot.
- **Summary determinism:** 5 accounts across 2 currencies with reordering delays → `accounts` in
  scope order and `totals_by_currency` byte-identical to the sequential expectation.
- **Discovery path avoids `get_account`:** `FakeMetaReader(list_ad_accounts=..., fetch_insights=...)`
  with `get_account` unstubbed; discovery call succeeds and never raises `NotImplementedError`;
  assert no `get_account` in `reader.calls`.
- **`fanout_max_workers_from_env`:** default 8; `META_FANOUT_MAX_WORKERS="1"/"32"/"999"/"garbage"`
  → `8` default / clamp to `[1,32]` / no raise on garbage (falls back to default). Use
  `monkeypatch.setenv`.

## TODO

### Phase 1 — seam + engine in `account_discovery.py`
- Add `from __future__` already present; add imports: `concurrent.futures`, `os`, `dataclasses.dataclass`, `collections.abc.Callable`.
- Add `ResolvedScope` dataclass and `resolve_scope(reader, account_ids=None)`.
- Add `DEFAULT_FANOUT_MAX_WORKERS`, `fanout_max_workers_from_env()`, and `fan_out_accounts(...)`.
- Rewrite `cross_account_spend_summary` to: `resolve_scope` → `fan_out_accounts(read_one, scope.account_ids)` → main-thread assembly of `accounts`/`errors`/`totals_by_currency`, preserving every existing output field and the `note` rule. Keep `_parse_metric`, `_ad_account_id_from_row`, `normalize_ad_account` as-is.

### Phase 2 — tests
- Add the tests listed above. Run `pytest tests/test_meta_ads_analysis.py -k "cross_account or resolve_scope or fan_out or fanout" 2>&1 | tee /tmp/fanout.log`, then the full file to confirm no regressions.

### Phase 3 — docs
- `docs/META_API_SETUP.md` and `README.md`: note `cross_account_spend_summary` now fans out
  concurrently and that `META_FANOUT_MAX_WORKERS` (default 8) tunes the worker pool for very large
  fleets. Keep it brief; this is an internal-behavior + one-env-var note, not a new feature surface.

### Validation
- `ruff`/type-check per repo norms and the full `pytest tests/test_meta_ads_analysis.py` (stream with `tee`). If a failure is clearly outside this diff, follow the pre-existing-error procedure.
