description: Add read tools to our own Meta MCP server so it can pull ad data (insights, ads, campaigns, audiences, and more) directly — replacing the unvetted third-party read server we were leaning on.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/meta_api.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What to build

Hang the Meta **read** surface on our own MCP server (the scaffold from `mcp-server-scaffold`).
Register one MCP tool per read in `READ_METHODS` (minus the raw `iter_paginated` escape hatch),
each a thin wrapper over an existing `MetaReaderProvider` method. This makes our server a
**superset** of the parked community `meta-ads-mcp-server` candidate — it serves the 8 reads that
package could plus the 5 it could not (`list_custom_audiences`, `get_delivery_estimate`,
`search_targeting`, `list_pixels`, `list_custom_conversions`).

## Resolved design decisions (do not re-litigate)

### 1. Tool naming & argument shapes — **Option (b): natural names + shipped `SERVER_TOOL_MAP`.**

Each MCP tool is named **exactly** for its reader method (`fetch_insights`, `fetch_ads`,
`list_campaigns`, `get_campaign`, `list_adsets`, `get_adset`, `get_ad`, `list_custom_audiences`,
`get_account`, `get_delivery_estimate`, `search_targeting`, `list_pixels`,
`list_custom_conversions`). Argument names/shapes mirror the `MetaReaderProvider` method
signatures directly — in particular **`fields` stays a `list[str]`** (no comma-join): the tool
passes straight into `DirectMetaReader`, so the lossy `_join_fields` round-trip that `MCPMetaReader`
must do never happens here. `date_from`/`date_to` stay separate ISO strings (not a `time_range`
dict); `search_type` stays `search_type` (not `type`).

**Why not option (a) (mirror `DEFAULT_MCP_TOOL_MAP`'s community names):** option (a)'s *only*
advantage is "an `MCPMetaReader` pointed at our server needs no custom `tool_map`." That advantage
is **already impossible** — the 5 superset reads are `None` in `DEFAULT_MCP_TOOL_MAP`, so any
`MCPMetaReader` consuming our server needs a custom map regardless. Option (a) would therefore buy
nothing while saddling our owned surface with the community package's ugly names *and* argument
dialect (the very dependency we are moving off of). The dominant consumer of our server is the LLM
agent runtime (Cowork / Claude), which calls tools from their generated JSON schema; natural,
self-describing argument shapes minimize mis-calls and keep the wrappers a zero-translation
pass-through.

Ship `SERVER_TOOL_MAP: dict[str, str] = {m: m for m in READ_TOOL_METHODS}` (an identity map, since
tool names == reader method names) as a **module constant in `mcp_server.py`** so a future consumer
wiring an `MCPMetaReader` at our server has the full name map (all 13 reads) in one import.

**Non-goal (documented, out of scope):** making the *existing* `MCPMetaReader` a byte-for-byte
drop-in against our server. `MCPMetaReader` hard-codes the community argument dialect (`act_id`,
comma-joined `fields`, `time_range`, `type`) in each method; matching that would couple our clean
surface to the unvetted package. A `tool_map` only remaps names, not argument shapes, so this is
not achievable by config alone and we deliberately do not contort the schema for it. Inside our own
process the reader is always a `DirectMetaReader` (see decision 3).

### 2. `iter_paginated` is **not** exposed.

Matches `MCPMetaReader`. It is a raw Graph-path escape hatch with no natural tool shape; the
high-level reads drain pagination internally. `READ_TOOL_METHODS` = `READ_METHODS` minus
`iter_paginated`.

### 3. Reader is built **once** as a `DirectMetaReader`, bypassing backend selection.

`build_server` constructs a single `DirectMetaReader.from_env()` and shares it across all tool
calls. **Do NOT call `reader_from_env`** — with `META_READER_BACKEND=mcp` that would try to build an
`MCPMetaReader` and require a `tool_executor`, i.e. our server would recursively try to be its own
MCP client. Our server *is* the direct reader that an `mcp` backend elsewhere points at; it must
never recursively select an `mcp` backend. `build_server_info()` still **reports**
`reader_backend_from_env()` verbatim as a health string (unchanged) — that reporting is independent
of the reader the tools actually use.

### 4. `live_calls_enabled` flips to `True`.

The scaffold hard-codes `build_server_info()["live_calls_enabled"] = False` with a comment that
"later tickets own this flag." Read tools make live Meta reads, so flip it to `True` (a capability
flag — the server *can* make live calls now; it is independent of whether a token is present, since
`build_server_info` stays token-free). **Two existing tests assert `False` and must be updated**
(see TODO): `test_build_server_info_live_calls_always_false` and
`test_build_server_info_defaults_when_env_unset` (asserts the whole dict).

## Interfaces / types (in `src/meta_ads_analysis/mcp_server.py`)

```python
from collections.abc import Callable
from typing import Any
from .reader_provider import MetaReaderProvider, DirectMetaReader, READ_METHODS

# READ_METHODS minus the raw escape hatch.
READ_TOOL_METHODS: tuple[str, ...] = tuple(m for m in READ_METHODS if m != "iter_paginated")

# reader-method -> MCP tool name. Identity (our tools are named for the reader methods).
SERVER_TOOL_MAP: dict[str, str] = {m: m for m in READ_TOOL_METHODS}

def build_read_tools(reader: MetaReaderProvider) -> dict[str, Callable[..., Any]]:
    """Return {tool_name: callable} bound to `reader`.

    PURE: no FastMCP import, no socket, no token lookup — unit-testable with a FakeMetaReader.
    Each callable mirrors its reader method's arguments and returns the identical dict/list the
    reader returns (empty list -> [], API failure -> MetaApiError propagates unchanged).
    """
```

Each wrapper is hand-written (13 of them), matching the explicit per-method style already used in
`DirectMetaReader`/`FakeMetaReader`/`MCPMetaReader`. Use plain (positional-or-keyword) params in the
wrappers — MCP arguments arrive as a flat object and FastMCP builds each tool's JSON schema from the
wrapper's own signature/annotations, so keep annotations accurate (`fields: list[str]`,
`breakdowns: list[str] | None = None`, `time_increment: int | str = 1`, etc.).

FastMCP wiring inside `build_server` (only runs when the `mcp` extra is installed):
- Add `ToolError` to the guarded import block alongside `FastMCP` (`from mcp.server.fastmcp.exceptions
  import ToolError`; set `ToolError = None` in the `ModuleNotFoundError` branch).
- Build `reader = DirectMetaReader.from_env()` once.
- For each `name, func` in `build_read_tools(reader).items()`, register with `mcp.add_tool(...)`
  wrapping `func` so `MetaApiError` is caught and re-raised as `ToolError(str(exc))` (clean tool
  error, server keeps serving). Use `functools.wraps` so FastMCP still reads the real signature.
  Attach a short human description per tool (a `READ_TOOL_DESCRIPTIONS: dict[str,str]` constant, or
  reader-method docstrings) passed via `description=` to `add_tool`.

## Edge cases & interactions (write tests for each — MOCKS ONLY, no live Meta call anywhere)

- **Every read tool round-trips to the direct reader's shape.** For each tool in
  `build_read_tools(FakeMetaReader(...))`, assert output `==` what `DirectMetaReader` over the same
  canned data returns. Iterate `READ_TOOL_METHODS` so a newly added read that is not wired shows up
  as a failure (parallel to `test_reader_call_specs_cover_every_read_method`).
- **`iter_paginated` is absent.** Assert `"iter_paginated" not in build_read_tools(...)` and not in
  `SERVER_TOOL_MAP`; assert `set(SERVER_TOOL_MAP) == set(READ_TOOL_METHODS)` and it is an identity map.
- **The 5 superset reads are present** (`list_custom_audiences`, `get_delivery_estimate`,
  `search_targeting`, `list_pixels`, `list_custom_conversions`) — explicit assertion, since these are
  the deliberate delta over the parked candidate.
- **Empty result ≠ error.** A tool over a reader stubbed to return `[]` returns `[]` (not an error);
  a tool over a reader whose method raises `MetaApiError` propagates `MetaApiError` from the pure
  wrapper. These are distinguishable to the caller.
- **Insufficient token scope surfaces cleanly.** Stub a reader method to raise
  `MetaApiError("... requires ads_management ...")`; assert the wrapper propagates it (does not crash
  / swallow). The `MetaApiError -> ToolError` mapping lives in the FastMCP layer (see Known gap).
- **Multi-page drain (≥3 pages), no truncation.** Because the tools wrap `DirectMetaReader` which
  drains via `iter_paginated`, build a `MetaMarketingApiClient("token", session=<Mock>)` whose
  `session.get.side_effect` returns **3 pages** (`data` + `paging.next`, then a final empty
  `paging`), wrap it in `DirectMetaReader`, call e.g. the `fetch_ads` / `list_adsets` tool, and assert
  all 3 pages' items come back in order. Reuse the `session = Mock()` pattern from
  `test_meta_api_client_paginates` (tests near line 663).
- **Distinct arg signatures.** Cover `search_targeting(query, search_type, limit)` (list result,
  account-independent) and `get_delivery_estimate(adset_id, fields)` (node/dict result) separately
  from the account-scoped `list_*`/`get_*` reads.
- **No write reachable.** Assert `build_read_tools` returns only reads: no key matches
  `create_*`/`update_*`/`upload_*`, and the reader type is a `MetaReaderProvider` (which has no write
  methods). This preserves the read-only-by-construction seam.
- **No recursive mcp backend.** With `META_READER_BACKEND=mcp` set in the env, `build_server` must
  still build a `DirectMetaReader` (assert type) and must **not** raise the "no tool-executor"
  `RuntimeError` that `reader_from_env` would. `build_server_info()["read_backend"]` still reports
  `"mcp"` verbatim.
- **`live_calls_enabled` is `True`** in `build_server_info()` (update the two existing tests).

## Known gap to carry into the review handoff

`mcp` (the `server` extra) is **not installed** in the test/CI env — the scaffold's `build_server`
tests only exercise the `FastMCP = None` path. Therefore the `MetaApiError -> ToolError` wrapping and
the actual `mcp.add_tool` registration are **not** unit-covered here; they are thin FastMCP glue. All
read-tool *logic* is covered via the pure `build_read_tools(reader)` seam with mocks. Note this in the
review ticket as a deliberate gap (parallel to the scaffold's "no end-to-end HTTP smoke in CI" gap),
and add it to the manual smoke checklist: `pip install -e .[server]` → run `meta_mcp_server` → connect
an MCP client → call a read tool → confirm a bad token yields a clean tool error, not a crash.

## Validation

- `python3 -m pytest tests/ -q 2>&1 | tee /tmp/pytest-mcp-read.log` — full suite green (stream, don't
  silently redirect). All prior tests plus the new read-tool tests pass; the two `live_calls_enabled`
  tests are updated, not broken.
- No lint tooling is configured (pytest is the validation surface).

## TODO

### Phase 1 — reader-tool seam (pure, no FastMCP)
- Add `READ_TOOL_METHODS`, `SERVER_TOOL_MAP`, and `build_read_tools(reader)` to `mcp_server.py`.
- Hand-write the 13 thin wrappers (one per `READ_TOOL_METHODS` entry), each delegating 1:1 to the
  reader method with accurate type annotations; `fields` stays `list[str]`.
- Add `READ_TOOL_DESCRIPTIONS` (short per-tool human descriptions).

### Phase 2 — FastMCP wiring
- Add `ToolError` to the guarded import block (None when `mcp` absent).
- In `build_server`: construct one `DirectMetaReader.from_env()` (NOT `reader_from_env`); register each
  read tool via `mcp.add_tool`, wrapping `MetaApiError -> ToolError` with `functools.wraps`.
- Flip `build_server_info()["live_calls_enabled"]` to `True` (update the comment).

### Phase 3 — tests (MOCKS ONLY)
- Add the edge-case tests enumerated above (round-trip parity loop over `READ_TOOL_METHODS`;
  `iter_paginated` absent + identity-map assertions; 5-superset-present; empty≠error; scope-error
  propagation; ≥3-page drain via mocked `session`; `search_targeting`/`get_delivery_estimate`
  signatures; no-write-reachable; no-recursive-mcp-backend).
- Update `test_build_server_info_live_calls_always_false` and
  `test_build_server_info_defaults_when_env_unset` for `live_calls_enabled: True`.
- Run the full suite green with `tee`.

## End
