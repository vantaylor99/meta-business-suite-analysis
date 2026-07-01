description: Give our own MCP server the ability to read Meta ad data (insights, ads, campaigns, audiences, etc.) by wrapping the API code we already have — so it can stand in for the third-party Meta read server we've been relying on.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/meta_api.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why

We already have a full Meta read client (`meta_api.py`) and a swappable read seam
(`reader_provider.py`) whose `MCPMetaReader` was built to consume an **external** community MCP
server (`meta-ads-mcp-server`, parked/unvetted in `.mcp.json`). This ticket flips that around: expose
**our own** reads as MCP tools so we own the read surface end to end — no unvetted npm dependency, and
we can cover the reads the community package doesn't.

## Scope / what "done" looks like

- The server registers one MCP tool per read in the app's actual read surface — the `READ_METHODS`
  tuple in `reader_provider.py` (`fetch_insights`, `fetch_ads`, `list_campaigns`, `get_campaign`,
  `list_adsets`, `get_adset`, `get_ad`, `list_custom_audiences`, `get_account`,
  `get_delivery_estimate`, `search_targeting`, `list_pixels`, `list_custom_conversions`; decide
  explicitly whether to expose the raw `iter_paginated` escape hatch — default **no**, matching
  `MCPMetaReader`).
- Each tool is a **thin wrapper** over the existing read method (via a `MetaReaderProvider`), returns
  the same dict/list shapes `DirectMetaReader` returns, and drains pagination internally (no silent
  truncation) — the invariants `MCPMetaReader` already documents.
- Tool **names and argument shapes** are chosen so this server can be a drop-in for the reader seam.
  Decide in design: either (a) mirror `DEFAULT_MCP_TOOL_MAP`'s names so an `MCPMetaReader` pointed at
  our server needs no custom map, or (b) pick natural names and ship the matching `tool_map`. State
  the choice and why.
- The 5 reads the community server couldn't serve (`list_custom_audiences`, `get_delivery_estimate`,
  `search_targeting`, `list_pixels`, `list_custom_conversions`) **are** exposed here — a deliberate
  superset of the parked candidate.
- Mock-only tests: every read tool is exercised through a `FakeMetaReader` / fake executor; assert the
  tool output round-trips to the same shape the direct reader produces. **No live Meta call anywhere.**

## Design notes / interfaces

- The server should build its reader once from config (`reader_from_env` / a `DirectMetaReader` over
  `client_from_env`) and share it across tool calls. `fields` arrives from the client as a list and
  must reach the client as a list (the existing `_join_fields` 1:1 rule — a dropped field silently
  blanks a downstream metric, which the confidence engine punishes).
- Reads are **read-only by construction**: they route through a `MetaReaderProvider`, never the
  write methods on `MetaMarketingApiClient`. Keep that seam — do not let a read tool reach a
  `create_*`/`update_*`/`upload_*`.

## Edge cases & interactions

- A read whose underlying token scope is insufficient (`ads_read` vs `ads_management`) should surface
  the `MetaApiError` message cleanly as a tool error, not crash the server.
- Empty result sets vs. genuine API errors must be distinguishable to the caller (empty list ≠ error).
- Multi-page results (`paging.next`) must drain fully; add a test with ≥3 pages like the existing
  `MCPMetaReader` pagination test.
- `search_targeting` / `get_delivery_estimate` have different arg shapes than the account-scoped
  reads — cover each signature.
- Confirm the read backend selection (`META_READER_BACKEND`) interplay is coherent: this server *is*
  a direct reader internally; it should not recursively select an `mcp` backend.
