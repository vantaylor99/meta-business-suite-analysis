description: Add a tool that answers a spend/performance question across every ad account the token can reach in one call, correctly keeping different currencies apart instead of adding them together.
prereq: mcp-list-ad-accounts
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Goal

Add a cross-account aggregate read tool — `cross_account_spend_summary` — that answers a
spend/performance question over **all reachable accounts** (or an explicit subset of account ids)
in a single call. It discovers accounts via `account_discovery.list_ad_accounts` (from the prereq
ticket), fans out existing per-account reads, and returns a combined table plus **per-currency
subtotals**.

This is Part B of the plan ticket `mcp-cross-account-read-tools` (decisions locked 2026-07-09).
The prereq `mcp-list-ad-accounts` supplies the discovery seam, the `account_discovery` module, and
the `build_discovery_tools(reader)` builder this ticket extends.

## Architecture

The fan-out logic lives in the **library layer** (`account_discovery.py`) so it is fully testable
with a `FakeMetaReader`, never inline in the FastMCP closure. The MCP tool wrapper in
`build_discovery_tools` is a thin delegate, registered alongside `list_ad_accounts`.

```
cross_account_spend_summary(reader, *, date_from, date_to, account_ids=None, ...)   # account_discovery.py
   │
   ├─ resolve target accounts:
   │     account_ids given?  → build rows via reader.get_account(act_id) per id
   │     else                → account_discovery.list_ad_accounts(reader)   (all reachable)
   │
   ├─ for each account (SEQUENTIAL — rely on the client's 429 retry, no new concurrency):
   │     try:  fetch_insights(act_id, level="account", time_increment="all_days", date_from, date_to)
   │           → one aggregated row; extract additive metrics (spend, impressions, clicks)
   │     except MetaApiError as e:  record a per-account error marker, keep going
   │
   ├─ group/subtotal additive metrics BY currency (never across currencies)
   │
   ▼
{ accounts:[...], totals_by_currency:{...}, errors:[...], counts..., date_from, date_to }
```

### Interface (add to `account_discovery.py`)

```python
DEFAULT_SUMMARY_INSIGHT_FIELDS: list[str] = ["spend", "impressions", "clicks"]
# Only additive metrics are subtotaled. Ratio metrics (cpc/ctr/roas) MUST NOT be summed across
# accounts — if added to the per-row output later, keep them OUT of totals_by_currency.

def cross_account_spend_summary(
    reader: MetaReaderProvider,
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    insight_fields: list[str] | None = None,
) -> dict[str, Any]:
    ...
```

Returned shape:
```python
{
    "date_from": "2026-06-01",
    "date_to": "2026-06-30",
    "account_count": 4,            # accounts we attempted the fan-out over
    "reachable_count": 5,          # accounts discovered (== account_count when account_ids given)
    "accounts": [
        {
            "ad_account_id": "act_123", "account_id": "123", "name": "Acme",
            "currency": "USD", "account_status": 1, "account_status_label": "ACTIVE",
            "spend": 123.45, "impressions": 5000, "clicks": 200,
        },
        ...
    ],
    "totals_by_currency": {
        "USD": {"spend": 456.78, "impressions": 12000, "clicks": 500, "account_count": 3},
        "EUR": {"spend": 12.00,  "impressions": 300,   "clicks": 10,  "account_count": 1},
    },
    "errors": [
        {"ad_account_id": "act_999", "error": "(#200) ... permission ..."},
    ],
    "note": "no accounts reachable",   # present ONLY when accounts == [] and no ids were given
}
```

Design points:
- **No grand total.** `totals_by_currency` is the only aggregate. When every account shares one
  currency the dict simply has one key — this is the plan's mandated way to avoid summing across
  currencies. Do NOT emit a single top-level `total_spend`.
- **Additive-only subtotals.** Sum only `spend` / `impressions` / `clicks` (whichever additive
  fields are requested). Parse numeric strings (Meta returns `spend` as a string) to float/int
  defensively; a missing/blank metric counts as 0 for the subtotal but the per-row value should
  reflect what Meta returned (0 or absent).
- **Account metadata source.** When `account_ids` is omitted, reuse the normalized rows from
  `list_ad_accounts` (they already carry name/currency/status/label). When `account_ids` is given,
  call `reader.get_account(act_id, fields=[...])` per id and normalize with
  `account_discovery.normalize_ad_account` so currency/label are present for grouping. Normalize the
  incoming ids through `account_registry._normalize_ad_account_id` so a bare numeric id or `act_`
  form both work.
- **`time_increment`.** Use `time_increment="all_days"` at `level="account"` so each account yields a
  single aggregated insights row for the whole window (not one row per day). If Meta returns zero
  rows for an account (no delivery in range), treat metrics as 0 — not an error.
- **currency key.** Group by the row's `currency`; if an account somehow has no currency, group it
  under a `"UNKNOWN"` currency bucket rather than dropping it or merging it into another currency.

### MCP tool wrapper (`mcp_server.py`)

Extend `build_discovery_tools(reader)` (created in the prereq) to also return
`cross_account_spend_summary`, and add its `DISCOVERY_TOOL_DESCRIPTIONS` entry:
```python
def cross_account_spend_summary(
    date_from: str, date_to: str, account_ids: list[str] | None = None,
) -> dict[str, Any]:
    return account_discovery.cross_account_spend_summary(
        reader, date_from=date_from, date_to=date_to, account_ids=account_ids,
    )
```
Description (operator-readable): "Summarize spend/performance across every ad account this token can
reach (or an explicit list of account ids) for a date range. Groups totals by currency and never
sums across different currencies; reports any accounts that could not be read."

No new registration wiring is needed beyond the prereq's `build_server` loop over
`build_discovery_tools` — this tool rides the same loop and `_wrap_tool_errors` mapping.

## Edge cases & interactions

- **Empty reach (no ids given):** `list_ad_accounts` returns `[]` → `accounts=[]`,
  `totals_by_currency={}`, `errors=[]`, plus `"note": "no accounts reachable"`. Never raises.
- **Partial fan-out failure:** one account's `fetch_insights`/`get_account` raising `MetaApiError`
  (permission, rate limit that exhausted the client's retry) must NOT fail the whole call — record
  `{"ad_account_id", "error"}` in `errors`, exclude that account from `accounts` and from subtotals,
  and continue. This is the central correctness requirement of this ticket — test it explicitly.
- **Mixed currencies:** subtotal per currency only; the shape above enforces it. A test MUST assert
  two accounts with different currencies never land in the same subtotal and no grand total exists.
- **Discovery-level failure:** if `list_ad_accounts` itself raises `MetaApiError` (bad token / no
  scope), let it propagate — the FastMCP `_wrap_tool_errors` maps it to a `ToolError`. This is a
  whole-call failure, distinct from a per-account partial failure.
- **Many accounts / rate limits:** keep the fan-out **sequential**; rely on the client's existing
  `429` retry. Do NOT add concurrency. (If a future need arises, that is a separate ticket.)
- **Explicit ids that the token cannot read:** each such id fails its `get_account`/`fetch_insights`
  and lands in `errors` — the same partial-failure path, not a hard error.
- **Numeric-string metrics:** Meta returns `spend` as a string; a naive `sum` of strings would
  concatenate. Parse before summing (this is a real bug class — cover it in a test).
- **Mock mode:** must work under `--mock` with the single seeded account and zero live calls. The
  mock reader already stubs `list_ad_accounts` (prereq), `get_account`, and `fetch_insights` — verify
  the summary returns one USD row and a one-key `totals_by_currency`.
- **Write path / `server_info` untouched.**

## Docs

- `README.md`: alongside the `list_ad_accounts` note from the prereq, mention
  `cross_account_spend_summary` — a one-call cross-account spend view that keeps currencies separate.
  (`docs/META_API_SETUP.md` was already updated by the prereq for the discovery edge; extend only if
  a summary example adds clarity.)

## Key tests (TDD)

- Cross-account summary over a `FakeMetaReader` seeded with 3 accounts (2 USD, 1 EUR), each with a
  canned account-level `fetch_insights` row: `totals_by_currency` has exactly `USD` and `EUR`,
  USD subtotal == sum of the two USD accounts, no grand total key present.
- Additive-metric parsing: `spend` returned as `"100.50"` strings sums to `150.75` (float), not a
  concatenated string.
- Partial failure: one account's `fetch_insights` raises `MetaApiError` → that account appears in
  `errors` with its id + message, is absent from `accounts`, and the other accounts' subtotals are
  unaffected.
- Explicit `account_ids` subset: fan-out targets only those ids (via `get_account` + `fetch_insights`),
  `reachable_count == account_count == len(ids)`, and `list_ad_accounts` is not consulted (assert via
  `FakeMetaReader.calls`).
- Empty reach (no ids): returns `accounts=[]`, `totals_by_currency={}`, `note="no accounts reachable"`.
- `account_status`/`currency` label propagation: rows carry `account_status_label` and `currency`
  from normalization.
- `--mock` smoke: `cross_account_spend_summary(build_mock_reader(), date_from=..., date_to=...)`
  returns one USD account row and a one-key `totals_by_currency`, zero live calls.
- Tool-surface: `build_discovery_tools(reader)` now exposes both `list_ad_accounts` and
  `cross_account_spend_summary`; `build_server` registers both (extend the mock `build_server`
  registration assertion).

## TODO

- [ ] Add `DEFAULT_SUMMARY_INSIGHT_FIELDS` + `cross_account_spend_summary(...)` to
      `account_discovery.py` (sequential fan-out, per-currency subtotals, per-account error markers,
      numeric-string parsing, empty-reach note).
- [ ] Extend `build_discovery_tools` + `DISCOVERY_TOOL_DESCRIPTIONS` in `mcp_server.py` with the
      `cross_account_spend_summary` wrapper (no extra `build_server` wiring needed).
- [ ] Write the TDD tests above.
- [ ] `README.md` note for the summary tool.
- [ ] Run the suite, streaming output:
      `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/mcp-cross-account-summary.log`.
