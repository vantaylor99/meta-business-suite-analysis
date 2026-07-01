description: Review the read tools just added to our own Meta MCP server — the server can now pull ad data (insights, ads, campaigns, audiences, and more) directly instead of relying on an unvetted third-party server.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/meta_api.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What was built

Hung the Meta **read** surface on our own MCP server (the `mcp-server-scaffold`). Added, in
`src/meta_ads_analysis/mcp_server.py`:

- **`READ_TOOL_METHODS`** — `READ_METHODS` minus the raw `iter_paginated` escape hatch (13 reads).
- **`SERVER_TOOL_MAP: dict[str, str]`** — identity map `{m: m for m in READ_TOOL_METHODS}` (our
  tools are named exactly for their reader methods). Shipped as a module constant so a future
  consumer wiring an `MCPMetaReader` at our server has the full name map in one import.
- **`READ_TOOL_DESCRIPTIONS`** — one short human description per tool (passed to `add_tool`).
- **`build_read_tools(reader) -> dict[str, Callable]`** — the pure, FastMCP-free seam. 13
  hand-written thin wrappers, each delegating 1:1 to the same-named `MetaReaderProvider` method.
  Wrappers use **positional-or-keyword** params with accurate annotations (`fields: list[str]`, no
  comma-join); FastMCP derives each tool's JSON schema from the wrapper signature. A construction
  time `assert set(tools) == set(READ_TOOL_METHODS)` guards against drift.
- **`_wrap_tool_errors(func)`** — `functools.wraps` wrapper that catches `MetaApiError` and
  re-raises it as FastMCP `ToolError(str(exc))` so the server keeps serving on a bad token / scope.
- **`build_server`** now: adds `ToolError` to the guarded import (`None` when `mcp` absent); builds
  a single `DirectMetaReader.from_env()` (deliberately **not** `reader_from_env`, to avoid a
  recursive `mcp` backend); registers each read via `mcp.add_tool(_wrap_tool_errors(func), name=…,
  description=…)`.
- **`build_server_info()["live_calls_enabled"]`** flipped `False -> True` (capability flag; stays
  token-free).

This makes our server a **superset** of the parked community candidate: the 8 reads that package
could serve plus the 5 it could not (`list_custom_audiences`, `get_delivery_estimate`,
`search_targeting`, `list_pixels`, `list_custom_conversions`).

## How to validate

- `.venv/bin/python -m pytest tests/ -q` — **402 passed** (was 381 + the 2 updated
  `live_calls_enabled` tests). Note: tests run under **`.venv/bin/python`**, not the system
  `python3` (the system interpreter lacks `requests`/`duckdb`/the package).
- Read-tool tests live at the end of the MCP section in `tests/test_meta_ads_analysis.py`
  (search `Meta MCP server read tools`).

## Test coverage map (all MOCKS ONLY — no live Meta call anywhere)

- `test_every_read_tool_round_trips_to_direct_reader_shape` — parity loop over `READ_TOOL_METHODS`:
  each tool's output `==` the direct reader's over the same canned data. A newly added read that
  isn't wired fails here.
- `test_iter_paginated_not_exposed_and_server_tool_map_is_identity` — escape hatch absent from tools
  and map; `SERVER_TOOL_MAP` covers exactly `READ_TOOL_METHODS` and is an identity map.
- `test_superset_reads_present_beyond_community_candidate` — the 5 delta reads present.
- `test_read_tool_empty_result_is_not_an_error` — `[]` in, `[]` out.
- `test_read_tool_propagates_meta_api_error_from_reader` — scope error propagates from the pure
  wrapper unchanged.
- `test_read_tools_drain_multiple_pages_without_truncation` — 3-page drain via mocked `session` for
  both `fetch_ads` and `list_adsets`.
- `test_search_targeting_tool_signature_is_account_independent` /
  `test_get_delivery_estimate_tool_signature_returns_node` — the two distinct arg shapes.
- `test_no_write_tool_reachable_from_build_read_tools` — no `create_*`/`update_*`/`upload_*` tool;
  `MetaReaderProvider` has no write methods.
- `test_build_server_uses_direct_reader_not_recursive_mcp_backend` — with `META_READER_BACKEND=mcp`,
  `build_server` still builds a `DirectMetaReader` and does not raise the `reader_from_env`
  RuntimeError; `server_info` still reports `"mcp"`. Uses a fake FastMCP (portable, no socket).
- `test_wrap_tool_errors_maps_meta_api_error_to_tool_error` — `MetaApiError -> ToolError` via a fake
  `ToolError` (runs without the `server` extra).
- `test_read_tools_register_on_real_fastmcp_and_map_errors` — **real** FastMCP integration:
  `mcp.add_tool` registration, schema derived from the wrapper signature, and the real
  `MetaApiError -> ToolError` mapping. Guarded with `pytest.importorskip("mcp")`.
- Updated `test_build_server_info_defaults_when_env_unset` and (renamed)
  `test_build_server_info_live_calls_enabled_true` for `live_calls_enabled: True`.

## Known gaps / things for the reviewer to probe

- **FastMCP glue coverage is env-dependent.** The ticket assumed `mcp` is absent in test/CI. In this
  repo's **`.venv`, `mcp` IS installed**, so `test_read_tools_register_on_real_fastmcp_and_map_errors`
  actually runs and covers real `add_tool` + real `ToolError` locally. In a CI env without the
  `server` extra it **skips** (importorskip). So: the real glue is covered where `mcp` is present and
  falls back to the fake-`ToolError` / fake-FastMCP tests everywhere else. Reviewer should decide
  whether CI should install `.[server]` to make the integration test non-skipped.
- **The real integration test pokes FastMCP internals** (`mcp._tool_manager.list_tools()` /
  `.get_tool().fn` / `.parameters`) — there is no sync public tool-enumeration API. This could break
  on an `mcp` major bump; it is guarded/skippable, so a break degrades to a skip, not a hard failure.
- **No end-to-end HTTP smoke in CI** (inherited from the scaffold). The wire path
  (`meta_mcp_server` process ← MCP client over HTTP → tool call) is not automated.
- **`MCPMetaReader` is NOT a drop-in against our server** — deliberate (plan decision 1, non-goal).
  Our tools use natural argument shapes (`fields: list[str]`, separate `date_from`/`date_to`,
  `search_type`), not the community dialect (`act_id`, comma-joined `fields`, `time_range`, `type`).
  A `tool_map` only remaps names, not argument shapes, so pointing the existing `MCPMetaReader` at
  our server would mis-call. Verify this is understood as intended, not a regression.

## Manual smoke checklist (out-of-band; needs the `server` extra)

1. `pip install -e .[server]`
2. `META_ACCESS_TOKEN=<real> meta_mcp_server`
3. Connect an MCP client; confirm the 13 read tools + `server_info` appear with sensible schemas.
4. Call a read (e.g. `fetch_ads`) with a valid account → data returns.
5. Call a read with a **bad token** → a clean `ToolError`, not a crash / traceback; server keeps
   serving subsequent calls.
