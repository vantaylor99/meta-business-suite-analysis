description: Add a tool that lists every ad account the access token can reach, so nobody has to hand-list accounts in the config file before working with them.
prereq:
files: src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/account_discovery.py (new), src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Goal

Add a `list_ad_accounts` MCP read tool that takes **no `account` argument** — the one read
tool that works before any config exists. It calls the Graph `/me/adaccounts` edge with the
shared env token and returns one normalized row per reachable ad account, with a human-readable
`account_status_label` alongside the raw `account_status` code so Cowork can relay it in plain
language.

This is Part A of the cross-account read work (decisions locked in the plan ticket
`mcp-cross-account-read-tools`, 2026-07-09): reads are intentionally open to every account the
token can reach — do **not** add a registry gate. The cross-account aggregate tool that builds on
this lands in the follow-up ticket `mcp-cross-account-summary`.

## Architecture

The read seam already has a strict shape (see `src/meta_ads_analysis/reader_provider.py`):
every read is a `MetaReaderProvider` method listed in `READ_METHODS`, implemented 1:1 on
`DirectMetaReader` / `FakeMetaReader` / `MCPMetaReader`, and mapped in `DEFAULT_MCP_TOOL_MAP`.
`list_ad_accounts` joins that seam as a first-class read method **returning raw Graph rows**
(byte-for-byte passthrough, like every other reader method).

**Status-label normalization does NOT belong on the reader** — `DirectMetaReader` is a
zero-transformation passthrough and the test `test_direct_meta_reader_delegates_each_read_method_one_to_one`
enforces verbatim delegation. Normalization lives in a **new library module**
`account_discovery.py` (testable, and reused by the follow-up aggregate tool).

**The tool wrapper does NOT go in `build_read_tools`.** That builder's tools are asserted to
return exactly what the reader returns (`test_every_read_tool_round_trips_to_direct_reader_shape`);
a tool that adds a status label would break that invariant. So — exactly like `iter_paginated` —
`list_ad_accounts` is **excluded from `READ_TOOL_METHODS`** and exposed through a **new
`build_discovery_tools(reader)` builder** whose wrapper delegates to
`account_discovery.list_ad_accounts(reader, ...)`.

```
Graph /me/adaccounts
      │  (raw rows: id="act_<n>", account_id, name, account_status, currency, …)
      ▼
MetaMarketingApiClient.list_ad_accounts(*, fields)          # meta_api.py  — thin, list_* style
      ▼
MetaReaderProvider.list_ad_accounts(*, fields)              # reader_provider.py — seam, 1:1
  ├── DirectMetaReader   → client.list_ad_accounts          # verbatim passthrough
  ├── FakeMetaReader     → canned/callable stub
  └── MCPMetaReader      → _call_list("list_ad_accounts")   # None-mapped → NotImplementedError
      ▼
account_discovery.list_ad_accounts(reader, *, fields=None)  # account_discovery.py — normalizes rows
      ▼
build_discovery_tools(reader)["list_ad_accounts"]           # mcp_server.py — FastMCP tool wrapper
```

### Interfaces / signatures

`src/meta_ads_analysis/meta_api.py` — add near the other `list_*` reads:
```python
def list_ad_accounts(self, *, fields: list[str]) -> list[dict[str, Any]]:
    """List every ad account the token can reach, via the /me/adaccounts edge."""
    params = {"fields": ",".join(fields), "limit": 200}
    return list(self.iter_paginated("/me/adaccounts", params=params))
```
Signature MUST be `(self, *, fields)` — `test_reader_signatures_match_client_exactly` and
`test_mcp_reader_signatures_match_client_exactly` compare the reader signatures against the client
param-for-param.

`src/meta_ads_analysis/reader_provider.py`:
- Add `"list_ad_accounts"` to `READ_METHODS` (place it **before** `"iter_paginated"` to keep that
  escape hatch last).
- Add the abstract method to `MetaReaderProvider`: `list_ad_accounts(self, *, fields) -> list[dict[str, Any]]`.
- `DirectMetaReader.list_ad_accounts` → `return self._client.list_ad_accounts(fields=fields)`.
- `FakeMetaReader.list_ad_accounts` → `return self._result("list_ad_accounts", fields=fields)`.
- `MCPMetaReader.list_ad_accounts` → `return self._call_list("list_ad_accounts", {"fields": self._join_fields(fields)})`
  (no community equivalent → `_tool_for` raises `NotImplementedError` naming the read; same pattern
  as `list_pixels` et al).
- `DEFAULT_MCP_TOOL_MAP`: add `"list_ad_accounts": None` (no community MCP tool → fall back to `direct`).

`src/meta_ads_analysis/account_discovery.py` (new module):
```python
# Meta ad-account account_status codes → human labels. Unknown codes → "UNKNOWN".
ACCOUNT_STATUS_LABELS: dict[int, str] = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    8: "PENDING_SETTLEMENT", 9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE",
    101: "CLOSED", 201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}

# Default fields requested from /me/adaccounts (plan decision A).
DEFAULT_AD_ACCOUNT_FIELDS: list[str] = [
    "account_id", "name", "account_status", "currency",
    "timezone_name", "amount_spent", "business",
]

def account_status_label(code: Any) -> str:
    """Human label for a Meta account_status code; 'UNKNOWN' for anything unmapped/None."""

def normalize_ad_account(row: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of a /me/adaccounts row with account_status_label added
    alongside the raw account_status (raw code preserved, never replaced)."""

def list_ad_accounts(reader: MetaReaderProvider, *, fields: list[str] | None = None) -> list[dict[str, Any]]:
    """Discovery: every reachable account as a normalized row. Empty reach → []."""
    rows = reader.list_ad_accounts(fields=fields or DEFAULT_AD_ACCOUNT_FIELDS)
    return [normalize_ad_account(r) for r in rows]
```
Keep the module import-light (no FastMCP, no token lookup) so it is unit-testable with a
`FakeMetaReader`. Import `MetaReaderProvider` for typing only.

`src/meta_ads_analysis/mcp_server.py`:
- Import `account_discovery`.
- Change the `READ_TOOL_METHODS` exclusion to drop **both** the raw escape hatch and the
  discovery read:
  ```python
  READ_TOOL_METHODS = tuple(m for m in READ_METHODS if m not in ("iter_paginated", "list_ad_accounts"))
  ```
  Update the adjacent comment to explain `list_ad_accounts` is surfaced via `build_discovery_tools`
  (no account arg + label normalization), not the 1:1 `build_read_tools` seam.
- `build_mock_reader`: seed `list_ad_accounts=lambda *a, **k: [dict(MOCK_ACCOUNT)]`. Confirm
  `MOCK_ACCOUNT` carries `account_status` and `currency` (it does: `account_status=1, currency="USD"`)
  so normalization yields `account_status_label="ACTIVE"`. Add `"account_id": "mock001"` to
  `MOCK_ACCOUNT` for a realistic discovery row.
- Add `DISCOVERY_TOOL_DESCRIPTIONS` and `build_discovery_tools(reader)`:
  ```python
  DISCOVERY_TOOL_DESCRIPTIONS = {
      "list_ad_accounts": (
          "List every ad account this access token can reach (no account argument needed). "
          "Returns account id, name, currency, and a human-readable status for each — use it to "
          "discover accounts before any are added to the config file."
      ),
  }

  def build_discovery_tools(reader):
      def list_ad_accounts(fields: list[str] | None = None) -> list[dict[str, Any]]:
          return account_discovery.list_ad_accounts(reader, fields=fields)
      return {"list_ad_accounts": list_ad_accounts}
  ```
  (The follow-up ticket adds `cross_account_spend_summary` to this same builder + descriptions.)
- In `build_server`, after registering the read tools, register the discovery tools the same way:
  ```python
  for name, func in build_discovery_tools(reader).items():
      mcp.add_tool(_wrap_tool_errors(func), name=name,
                   description=DISCOVERY_TOOL_DESCRIPTIONS.get(name) or f"Meta discovery: {name}")
  ```
  `_wrap_tool_errors` already maps `MetaApiError`/`ValueError` → `ToolError`, so a
  permission/Graph failure surfaces cleanly to Cowork. No change to `build_write_tools` or
  `server_info`.

## Edge cases & interactions

- **Empty reach:** `/me/adaccounts` returns `{"data": []}` → `list_ad_accounts` returns `[]`, never
  an exception. (`iter_paginated` already yields nothing for an empty data array.)
- **Token lacks permission / Graph error:** `MetaApiError` propagates out of the pure library and
  the read tool wrapper unchanged; the FastMCP `_wrap_tool_errors` layer maps it to a `ToolError`
  with an operator-readable message. Do not swallow it in `account_discovery`.
- **Unknown / missing `account_status`:** `account_status_label` returns `"UNKNOWN"` (no KeyError,
  no crash on a `None` or unexpected code); the raw `account_status` value is still passed through.
- **Row missing fields:** normalization must not assume every requested field is present (Meta omits
  empty fields) — copy the row and add the label; never index a missing key.
- **Mock mode:** `--mock` must serve `list_ad_accounts` with zero live calls (single seeded
  account). `build_mock_reader` stub above covers this; `build_server(mock=True)` must register the
  discovery tool too.
- **`server_info` unaffected:** it stays token-free; do not make it depend on account discovery.
- **Write path untouched:** no change to `build_write_tools` / `_resolve_account`.
- **Reader-seam parity:** adding to `READ_METHODS` fans out to four classes + `DEFAULT_MCP_TOOL_MAP`
  + several parity tests — miss one and a parity test fails loudly (that is the safety net; keep them
  all in sync).

## Existing tests to update (they iterate the read surface)

- `_READER_CALL_SPECS` (~line 7458): add `"list_ad_accounts": ((), {"fields": ["account_id", "name"]})`.
  `test_reader_call_specs_cover_every_read_method` asserts this dict == `READ_METHODS`.
- `test_direct_meta_reader_delegates_each_read_method_one_to_one` (~7503): passes automatically once
  the spec + `DirectMetaReader.list_ad_accounts` exist (proves verbatim 1:1 delegation — the reason
  normalization is NOT on the reader).
- `test_reader_signatures_match_client_exactly` (~7513) and
  `test_mcp_reader_signatures_match_client_exactly` (~7705): pass once all impls use `(self, *, fields)`.
- `test_mcp_reader_unsupported_reads_raise_naming_the_method` (~7812): add a
  `"list_ad_accounts": lambda: reader.list_ad_accounts(fields=["account_id"])` case (None-mapped →
  `NotImplementedError` naming the read, executor never invoked).
- `test_iter_paginated_not_exposed_and_server_tool_map_is_identity` (~9238): update the exclusion
  assertion to `set(READ_METHODS) - {"iter_paginated", "list_ad_accounts"}`, and add
  `assert "list_ad_accounts" not in tools` (it is a discovery tool, not a `build_read_tools` tool).
  SERVER_TOOL_MAP identity still holds because `list_ad_accounts` is excluded from `READ_TOOL_METHODS`.
- `test_build_mock_reader_all_stubs_present` (~10653): still iterates `READ_TOOL_METHODS`
  (unchanged), but the mock reader must now stub `list_ad_accounts` — add an explicit assertion
  `reader.list_ad_accounts(fields=["account_id"])` returns the seeded account.

## Docs

- `docs/META_API_SETUP.md`: at the `/me/adaccounts` sanity-check curl (~line 62-68), add a note that
  the server now exposes this as the `list_ad_accounts` tool (no account argument), so operators can
  discover reachable accounts from inside Cowork rather than by hand-running curl.
- `README.md`: in the Hybrid Meta integration / reads section (~line 36-40), note the new
  `list_ad_accounts` discovery tool (reads reach every account the token sees; config is only needed
  for writes).

## TODO

### Phase 1 — reader seam
- [ ] Add `MetaMarketingApiClient.list_ad_accounts(*, fields)` to `meta_api.py`.
- [ ] Add `"list_ad_accounts"` to `READ_METHODS`; add abstract + `DirectMetaReader` + `FakeMetaReader`
      + `MCPMetaReader` impls; add `"list_ad_accounts": None` to `DEFAULT_MCP_TOOL_MAP`.

### Phase 2 — discovery library + tool
- [ ] Create `src/meta_ads_analysis/account_discovery.py` with `ACCOUNT_STATUS_LABELS`,
      `DEFAULT_AD_ACCOUNT_FIELDS`, `account_status_label`, `normalize_ad_account`, `list_ad_accounts`.
- [ ] `mcp_server.py`: exclude `list_ad_accounts` from `READ_TOOL_METHODS`; add `MOCK_ACCOUNT.account_id`
      + mock reader stub; add `DISCOVERY_TOOL_DESCRIPTIONS` + `build_discovery_tools`; register in
      `build_server`.

### Phase 3 — tests (TDD)
- [ ] `list_ad_accounts` returns normalized rows with `account_status_label` from a `FakeMetaReader`
      seeded with multiple accounts of differing statuses (1→ACTIVE, 101→CLOSED, unmapped→UNKNOWN).
- [ ] Empty-reach: `FakeMetaReader(list_ad_accounts=[])` → discovery returns `[]`.
- [ ] Permission/Graph error: a `list_ad_accounts` stub raising `MetaApiError` propagates through
      `account_discovery.list_ad_accounts` unchanged (ToolError mapping is the FastMCP layer's job).
- [ ] `build_discovery_tools(reader)["list_ad_accounts"]()` returns the normalized rows and is NOT in
      `build_read_tools`.
- [ ] `--mock` smoke: `build_mock_reader().list_ad_accounts(...)` and the discovery tool both return
      the single seeded account with `account_status_label="ACTIVE"`, zero live calls.
- [ ] Update the existing parity tests listed above.

### Phase 4 — validate
- [ ] Run the suite, streaming output:
      `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/mcp-list-ad-accounts.log`
      (the `mcp`-gated FastMCP registration test uses `pytest.importorskip("mcp")`; if `mcp` is not
      installed it skips — that is expected, not a failure).
- [ ] Update `docs/META_API_SETUP.md` and `README.md` as above.
