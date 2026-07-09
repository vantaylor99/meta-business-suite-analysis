description: Review the new tool that lists every ad account an access token can reach, so nobody has to hand-list accounts in the config file first.
prereq:
files: src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What was built

Part A of the cross-account read work: a `list_ad_accounts` MCP **discovery** tool that takes
**no `account` argument**, calls the Graph `/me/adaccounts` edge with the shared env token, and
returns one normalized row per reachable ad account — each carrying a human-readable
`account_status_label` alongside the raw `account_status` code. Reads are intentionally open to
every account the token can reach; **no registry gate** was added (locked plan decision).

The follow-up aggregate tool (`2-mcp-cross-account-summary`, still in `implement/`) builds on this.

### The data path (all four layers landed)

```
Graph /me/adaccounts
  → MetaMarketingApiClient.list_ad_accounts(*, fields)          meta_api.py       (thin list_* read, drains pagination)
  → MetaReaderProvider.list_ad_accounts(*, fields)              reader_provider.py (seam, 1:1, before iter_paginated)
      Direct → client.list_ad_accounts   (verbatim passthrough)
      Fake   → canned/callable stub
      MCP    → _call_list; None-mapped in DEFAULT_MCP_TOOL_MAP → NotImplementedError naming the read
  → account_discovery.list_ad_accounts(reader, *, fields=None)  account_discovery.py (NEW — normalizes rows)
  → build_discovery_tools(reader)["list_ad_accounts"]           mcp_server.py     (FastMCP wrapper, NOT build_read_tools)
```

### Key design points to verify against the plan

- **Normalization is NOT on the reader.** `DirectMetaReader` stays a zero-transformation passthrough
  (parity test `test_direct_meta_reader_delegates_each_read_method_one_to_one` enforces verbatim 1:1
  delegation). The label logic lives in the new import-light `account_discovery.py` (no FastMCP, no
  token lookup — unit-testable with a `FakeMetaReader`, reusable by the follow-up aggregate tool).
- **Not in `build_read_tools`.** That builder's tools are asserted to return exactly what the reader
  returns; a status label would break that invariant. So `list_ad_accounts` is excluded from
  `READ_TOOL_METHODS` (alongside `iter_paginated`) and exposed through the new
  `build_discovery_tools(reader)` builder + `DISCOVERY_TOOL_DESCRIPTIONS`, registered in `build_server`
  after the read tools using the existing `_wrap_tool_errors` (so a permission/Graph `MetaApiError`
  maps to a clean `ToolError`).
- **`(self, *, fields)` signature everywhere** so the signature-parity tests
  (`test_reader_signatures_match_client_exactly` / `test_mcp_reader_...`) pass client-for-client.

## How to validate

Full suite (the `mcp`-gated FastMCP registration test runs when the `server` extra is installed —
it ran, not skipped, in this environment):

```
python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/mcp-list-ad-accounts.log
```

Result at handoff: **474 passed** (log at `/tmp/mcp-list-ad-accounts.log`).

### Test coverage added

New tests (all MOCKS ONLY — no live Meta call):
- `test_meta_api_client_list_ad_accounts_hits_me_adaccounts_edge` — client targets `/me/adaccounts`,
  joins `fields` to a comma string, drains pagination.
- `test_account_status_label_maps_codes_and_defaults_unknown` — mapped code + None/garbage/unmapped → `UNKNOWN`, never raises.
- `test_normalize_ad_account_adds_label_preserving_raw_code` — label added, raw code preserved, source row not mutated.
- `test_normalize_ad_account_tolerates_missing_status_field` — Meta-omitted field → `UNKNOWN`, code not fabricated.
- `test_list_ad_accounts_returns_normalized_rows_for_mixed_statuses` — 1→ACTIVE, 101→CLOSED, 555→UNKNOWN; default fields requested.
- `test_list_ad_accounts_empty_reach_returns_empty_list` — empty reach → `[]`.
- `test_list_ad_accounts_honors_explicit_fields_override` — caller `fields` override reaches the reader.
- `test_list_ad_accounts_propagates_meta_api_error_unchanged` — permission failure propagates as `MetaApiError` (not swallowed).
- `test_build_discovery_tools_returns_normalized_rows_and_is_not_a_read_tool` — no account arg; not in `build_read_tools`; in `DISCOVERY_TOOL_DESCRIPTIONS`.
- `test_discovery_tool_mock_smoke_returns_single_seeded_account` — `--mock` returns the single seeded ACTIVE account, zero live calls.

Existing parity tests updated: `_READER_CALL_SPECS`, `test_mcp_reader_unsupported_reads_raise_naming_the_method`,
`test_iter_paginated_not_exposed_and_server_tool_map_is_identity`,
`test_read_tools_register_on_real_fastmcp_and_map_errors` (exact tool-name set now includes discovery),
`test_build_mock_reader_all_stubs_present` (explicit `list_ad_accounts` assertion — it is not in the
`READ_TOOL_METHODS` loop). `MOCK_ACCOUNT` gained `"account_id": "mock001"` for a realistic discovery row.

## Known gaps / things a reviewer should probe

- **No live-Graph verification of `/me/adaccounts` row shape.** Per the repo's MOCKS-ONLY rule, every
  test seeds a fake. The code assumes Graph always returns the node `id` (`act_<n>`) even though it is
  not in `DEFAULT_AD_ACCOUNT_FIELDS` (true for the Graph node-id, but untested against a real
  response). `account_status` is assumed to arrive as an int (or int-coercible) — `account_status_label`
  coerces via `int(code)` so a string `"1"` also maps correctly, but a real payload was not observed.
- **`business` / `amount_spent` pass through un-normalized.** `business` is a nested object and
  `amount_spent` a string-cents value; both are returned verbatim (matching every other reader). If the
  follow-up aggregate or Cowork needs these flattened, that is out of scope here — flag if the plan
  expected otherwise.
- **`ACCOUNT_STATUS_LABELS` completeness.** The map is the documented Meta code set (1,2,3,7,8,9,100,101,
  201,202). If Meta has added codes since, unmapped ones surface as `UNKNOWN` (safe, but a reviewer
  familiar with current codes may want to extend the map).
- **No end-to-end test through a live FastMCP request** (only the registration/`_tool_manager` path is
  exercised). The wrapper→normalizer→reader chain is covered by unit tests, not a real MCP round-trip.
