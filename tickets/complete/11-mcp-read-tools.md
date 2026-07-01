description: Our own Meta MCP server can now pull ad data (insights, ads, campaigns, audiences, and more) directly, reviewed and confirmed working, with startup errors and docs cleaned up.
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/meta_api.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, AGENTS.md
----
## What shipped

The custom Meta MCP server now exposes the full live **read** surface — `server_info` plus 13
read tools (`READ_TOOL_METHODS`), each a 1:1 wrapper over a shared `DirectMetaReader`, with
`MetaApiError` mapped to a clean FastMCP `ToolError`. This is a superset of the parked community
candidate (the 8 reads it could serve plus 5 it could not). Writes remain CLI-only.

See the implement commit `7e81d92` for the full build. The review pass below hardened startup
error handling and brought the docs in line with the new reality.

## Review findings

Reviewed the implement diff (`7e81d92`) with fresh eyes against SPP/DRY/modularity, error
handling, type safety, resource cleanup, test coverage, and doc accuracy. The core
implementation is sound: the pure `build_read_tools(reader)` seam is clean and well-tested, the
`SERVER_TOOL_MAP`/`READ_TOOL_METHODS` drift guards are real (construction-time `assert` + parity
test), the no-recursive-`mcp`-backend decision is correct and tested, and the read-only-by-
construction property is asserted. The `mcp` extra **is** installed in `.venv`, so the real
FastMCP integration test (`test_read_tools_register_on_real_fastmcp_and_map_errors`) runs (not
skipped) and confirms real `add_tool` registration + real `MetaApiError -> ToolError` mapping,
including that all 13 tools — with union (`int | str`) and `list[str]` params — register without a
schema-derivation error.

**Findings and disposition:**

- **[minor — FIXED] Missing-token startup leaked a bare traceback.** `build_server` calls
  `DirectMetaReader.from_env()` eagerly, and `client_from_env` raises `MetaApiError` when
  `META_ACCESS_TOKEN` is unset/empty. `main()` wraps only `OSError`, so launching the server
  without a token produced an uncaught `MetaApiError` traceback — violating the module's stated
  "actionable `SystemExit`, never a bare traceback" contract (which the SDK-missing branch already
  honors). Fixed: `build_server` now catches `MetaApiError` from reader construction and re-raises
  an actionable `SystemExit` naming `META_ACCESS_TOKEN` and the `ads_read` scope. Added
  `test_build_server_missing_token_raises_actionable_systemexit` (fake FastMCP, no socket, off the
  `server` extra). Eager construction at startup is the intended design (the plan mandated it and
  the smoke checklist assumes a token present), so only the error *presentation* was changed — not
  the design.

- **[minor — FIXED] Stale docs across three files.** The change flipped the server from
  scaffold to live-reads but left the old "scaffold / zero live Meta calls / single `server_info`
  tool / `live_calls_enabled: false`" wording in place. Updated: the module docstring in
  `mcp_server.py`, the "Our custom Meta MCP server" section in `docs/META_API_SETUP.md` (now
  documents the 13 read tools, `live_calls_enabled: true`, the required token at startup, and that
  the `.mcp.json` `meta-suite` entry stays parked pending the write ticket/rollout), and the
  equivalent paragraph in `AGENTS.md`.

- **[verified OK] Error-catch scope.** `_wrap_tool_errors` catches only `MetaApiError`. Confirmed
  in `meta_api.py` that the client wraps **all** failure modes — network/`requests` exceptions
  (`except Exception` in `_get_json`/`_post_json`), non-JSON bodies, bad shapes, and HTTP >= 400 —
  in `MetaApiError`, so no raw exception escapes a read tool. Non-retryable errors (e.g. a 400 bad
  token) raise immediately without the retry `sleep`, so a bad token does not stall the server.

- **[verified OK] Test coverage.** Happy path, empty-result-≠-error, `MetaApiError` propagation
  from the pure wrapper, `MetaApiError -> ToolError` at the glue layer (fake + real), multi-page
  drain (≥3 pages, two distinct list reads), the two distinct arg shapes
  (`search_targeting` / `get_delivery_estimate`), escape-hatch exclusion, identity `SERVER_TOOL_MAP`,
  the 5-read superset, no-write-reachable, and no-recursive-`mcp`-backend are all covered.
  `_READER_CALL_SPECS` covers the full read surface (guarded by
  `test_reader_call_specs_cover_every_read_method`). Full suite: **403 passed** (was 402; +1 new
  test) under `.venv/bin/python -m pytest tests/ -q`.

- **[accepted, not changed] `assert set(tools) == set(READ_TOOL_METHODS)` in `build_read_tools`.**
  Stripped under `python -O`, but it is a belt-and-suspenders developer-error guard; the parity
  test `test_every_read_tool_round_trips_to_direct_reader_shape` provides the real, always-on
  coverage. Left as-is.

- **[noted, low risk] Single shared reader / `requests.Session` under concurrent MCP requests.**
  One `DirectMetaReader` (one `requests.Session`) is shared across tool calls. `requests.Session`
  is not strictly thread-safe, but the wrappers are stateless and the target is a single-operator
  local server; this is inherited from the scaffold's one-process design. Not worth a ticket at
  this stage — flag if the server is ever fronted for concurrent multi-user load.

- **[no action] Lint.** No lint tooling is configured in this repo (no ruff/flake8/mypy in
  `pyproject.toml`); "lint" here is the test suite plus an AST/compile check, both green.

**No major findings — no downstream fix/plan/backlog tickets filed.** The read surface matches the
plan (natural argument shapes, deliberately *not* a drop-in for the community `MCPMetaReader`
dialect — verified as intended, not a regression), the guarded-write surface is correctly still
CLI-only and belongs to the `mcp-guarded-write-tools` ticket, and the inherited "no end-to-end HTTP
smoke in CI" gap remains a documented manual smoke step, not a regression introduced here.

## Manual smoke checklist (out-of-band; needs the `server` extra)

1. `pip install -e .[server]`
2. `META_ACCESS_TOKEN=<real> meta_mcp_server` (omit the token → clean `SystemExit`, not a traceback)
3. Connect an MCP client; confirm the 13 read tools + `server_info` appear with sensible schemas.
4. Call a read (e.g. `fetch_ads`) with a valid account → data returns.
5. Call a read with a **bad token** → a clean `ToolError`; server keeps serving subsequent calls.

## End
