description: Review the new "read Meta ads data through an MCP server" option — the code that translates between our app and the server, the config that points at a community server, and the docs explaining how to switch to Meta's official login-based server later.
prereq:
files: src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/rotation.py, .mcp.json, docs/META_API_SETUP.md, AGENTS.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What landed

Wired the **read seam** so Meta reads can come from an MCP server instead of the direct Graph client,
defaulting to direct so nothing changes until an operator opts in. **No live MCP/Meta call is made
anywhere** — config + translation code + mock-only tests only.

### 1. `MCPMetaReader(MetaReaderProvider)` — `src/meta_ads_analysis/reader_provider.py`
- Implements the full `MetaReaderProvider` ABC. Takes an injected `tool_executor` callable
  (`(tool_name, arguments) -> raw`); it **never constructs or connects a transport**.
- **Arg translation:** each read maps to a named MCP tool via `DEFAULT_MCP_TOOL_MAP` (overridable at
  construction). `fields=[...]` → comma string; `fetch_insights` window → `time_range`
  `{since, until}`; `breakdowns` → list; account id → `act_id`.
- **Result translation:** normalizes the executor's return (bare list, `{"data":[...]}` envelope, a
  single node dict, or a JSON **string**) back into the exact dict/list shapes `DirectMetaReader`
  returns — so downstream parsers are backend-agnostic.
- **Pagination:** candidate server does not auto-paginate → `MCPMetaReader` drains `paging.next` via
  the server's `meta_ads_fetch_pagination_url` tool, and **raises rather than silently truncating** if
  a page is dropped with no pagination tool configured. Runaway guard at `MAX_PAGES=1000`.
- **Partial coverage:** reads the candidate doesn't expose (`list_custom_audiences`,
  `get_delivery_estimate`, `search_targeting`, `list_pixels`, `list_custom_conversions`) and the raw
  `iter_paginated` escape hatch raise `NotImplementedError` **naming the read**, so a caller can fall
  back to `direct` for that one read.

### 2. Provider selection — `reader_from_env()` in `reader_provider.py`
- Single selection point: `META_READER_BACKEND` = `direct` (default) | `mcp`. Unset/`direct` →
  `DirectMetaReader.from_env()` (byte-for-byte today). `mcp` → `MCPMetaReader(tool_executor)`, and
  **raises `RuntimeError` if no executor is injected** (the CLI can't synthesize the agent MCP
  surface). Unknown value → `ValueError`.
- The two `*.from_env()` construction points were rewired to it: `actions.py:265`
  (`enrich_action_plan_with_live_state`) and `rotation.py:762` (`resolve_rotation_inputs`). Supplied
  readers still short-circuit before any env/token lookup (laziness preserved).

### 3. `.mcp.json`
- `code-search` entry is **byte-for-byte untouched** (verified via `git diff`).
- The `meta-ads-read` candidate is parked under a **non-active** `_candidateMcpServers` key with
  `_README`/`_candidate`/`_TODO` notes. **Only servers under `mcpServers` are launched**, so this
  guarantees the community server is never auto-executed — honoring "do NOT run npx". Token passes via
  `${META_ACCESS_TOKEN}` interpolation; no literal secret in the committed file.

### 4. Docs
- `docs/META_API_SETUP.md`: new "Read backend: direct vs MCP" section — the toggle, the community
  candidate (marked unvetted), covered/uncovered reads, pagination behavior, reads-only/token-scoping,
  and the **official Meta hosted OAuth server as a config-only drop-in** (not required, not wired).
- `AGENTS.md`: a "Read backend (direct vs MCP)" pointer cross-referencing the docs and the
  `hybrid-model-docs-and-tool-catalog` ticket.

## Candidate package (UNVETTED — operator must review before enabling)
`meta-ads-mcp-server@1.5.1` (npm). Token-based (`META_ADS_ACCESS_TOKEN`, no OAuth), registers a
read-only tool set by default, no auto-pagination (provides `meta_ads_fetch_pagination_url`).

## Validation
- `.venv/bin/python -m pytest tests/ -q` → **208 passed** (15 new). No pre-existing failures.
- New tests (all mock-only, `tests/test_meta_ads_analysis.py`):
  - `test_mcp_reader_signatures_match_client_exactly` — MCPMetaReader is a true drop-in.
  - `test_mcp_reader_translates_fields_list_to_comma_string_without_dropping_any` — field round-trip.
  - `test_mcp_reader_insights_translates_window_and_breakdowns`.
  - `test_mcp_reader_list_result_shape_matches_direct_reader` / `..._node_..._matches_direct_reader`
    — result-shape parity with `DirectMetaReader`.
  - `test_mcp_reader_accepts_bare_list_and_json_string_results`.
  - `test_mcp_reader_drains_pagination_so_no_page_is_dropped` /
    `test_mcp_reader_refuses_to_truncate_when_pagination_tool_disabled`.
  - `test_mcp_reader_unsupported_reads_raise_naming_the_method`.
  - `reader_from_env`: default-to-direct, explicit direct, mcp-requires-executor, mcp-with-executor,
    unknown-backend, and `test_entry_point_default_reads_through_direct_when_backend_unset` (the
    default-off behavioral guarantee through `enrich_action_plan_with_live_state`).

## Known gaps / things to scrutinize (work is a starting point, not a finish line)
- **The candidate package was never run or source-inspected.** Tool names (`meta_ads_*`,
  `meta_ads_fetch_pagination_url`), the `act_id` arg key, and the `META_ADS_ACCESS_TOKEN` env var all
  come from **web docs + npm metadata**, not from executing/reading the package. The real package's
  schema may differ — the operator MUST verify (and likely adjust `DEFAULT_MCP_TOOL_MAP` and the
  per-method arg keys) before enabling. Treat the translation arg names as best-effort.
- **Zero live-integration coverage by design.** "Parity" is parity with `DirectMetaReader`'s *contract*
  against a **fake** executor seeded with my assumptions — not proof the real server returns those
  shapes. First real run should be a careful manual smoke test by the operator (out of scope here).
- **No production caller builds `MCPMetaReader` yet.** It's only constructible by an agent runtime that
  injects a tool-executor; the CLI path deliberately raises on `mcp`. So `META_READER_BACKEND=mcp`
  currently has no working caller — the seam is ready, the agent-side wiring is a separate concern.
- **Arg-schema overrides are coarse.** `tool_map` is overridable, but per-method argument *key* names
  (e.g. `act_id`) are not parameterized; slotting a package with different arg names needs a small
  edit to the method bodies. Flag if the reviewer wants that made data-driven.
- **JSON has no comments**, so the "commented-out placeholder" from the ticket is expressed as a
  non-launched `_candidateMcpServers` key. Confirm the reviewer is comfortable that MCP clients ignore
  sibling keys (verified: only `mcpServers` is launched; `code-search` connected, candidate did not).

## Suggested review focus
- Does parking under `_candidateMcpServers` truly prevent auto-launch in every harness that reads this
  file? (Confirmed for the tess/agent-SDK client here.)
- Is raising on `META_READER_BACKEND=mcp` in the CLI the right failure mode vs. silently falling back
  to direct? (Chose raise = loud, to avoid surprise silent behavior.)
- Translation arg-key correctness against the real package once vetted (the biggest live-risk).
- Major findings → new `fix/`/`plan/` ticket(s); minor → fix inline.
