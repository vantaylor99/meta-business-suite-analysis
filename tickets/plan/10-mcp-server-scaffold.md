description: Stand up the skeleton of our own Meta MCP server — an empty-but-running server on my machine that Claude can connect to — so later tickets can hang the real read and write tools on it. Nothing account-touching yet.
prereq:
files: pyproject.toml, .mcp.json, src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, AGENTS.md
difficulty: medium
----
## Why

We are moving the operational code (reads + guarded writes) out of the CLI and into a **custom
Meta MCP server we own**, so every specialist gets identical tools + guardrails through a connector
rather than a checked-out repo (long-term: bundled into Claude Cowork accounts; see the backlog
`mcp-*` tickets). This ticket is only the **foundation**: a server process that starts, reports
health, and can be connected to from an MCP client — no Meta reads or writes yet. Those land in
`mcp-read-tools` and `mcp-guarded-write-tools`, which depend on this.

## Scope / what "done" looks like

- The Python MCP SDK (FastMCP / `mcp`) is a project dependency in `pyproject.toml` (its own optional
  extra, e.g. `[project.optional-dependencies] server`, so the CSV/analysis install stays lean).
- A new module `src/meta_ads_analysis/mcp_server.py` builds and runs an MCP server exposing **one
  trivial tool** (e.g. `server_info` → name, version, configured Meta API version, selected read
  backend, "no live calls made" flag). This proves connect + tool-call end to end.
- The server is runnable locally over **HTTP (streamable-http)**, not just stdio — we want to
  exercise the eventual hosted/remote shape from the start (auth, role headers, and multi-user come
  later). A `[project.scripts]` entry (e.g. `meta_mcp_server`) launches it; the bind host/port are
  configurable via env with local-only defaults.
- A `.mcp.json` client entry (kept **disabled/parked** like the existing `_candidateMcpServers`
  pattern until read/write tools exist) documents how a local client connects (HTTP url + how the
  role/token header will eventually be supplied).
- Short operator docs (extend `docs/META_API_SETUP.md`, pointer from `AGENTS.md`): how to install the
  `server` extra, launch locally, and connect a client.

## Interfaces (indicative, resolve in design)

```
# launch:  meta_mcp_server --host 127.0.0.1 --port 8765   (env fallbacks: MCP_SERVER_HOST/PORT)
# tool:    server_info() -> {
#   "name": "meta-ads-mcp", "version": "...", "meta_api_version": "...",
#   "read_backend": "direct|mcp|fake", "live_calls_enabled": false
# }
```

## Constraints carried from the codebase

- **MOCKS ONLY.** This scaffold must make **zero live Meta calls** (it has no Meta tools yet, so this
  is trivially true — but the `server_info` "live_calls_enabled" flag and the test posture must make
  the mocks-only stance explicit, matching the repo-wide build-safety rule).
- Reuse existing config plumbing (`config.py`, `client_from_env`, `reader_from_env`) for version/
  backend reporting rather than re-reading env directly in the server module.
- The server module is a **thin entrypoint**: it must not embed Meta/business logic. It imports and
  exposes existing package functions; keep it that way so the CLI and the server stay two frontends
  over one library.

## Edge cases & interactions

- Missing `server` extra / SDK not installed → a clear actionable error, not an import traceback, when
  launching (mirror the `requests`-missing message style in `meta_api.py`).
- Port already in use / bad host → fail fast with a readable message.
- `server_info` must work with **no `META_ACCESS_TOKEN` set** (report backend/version, not error) so
  the scaffold is testable without credentials.
- Confirm running the HTTP server does not interfere with the existing stdio `code-search` MCP server
  already in `.mcp.json`.
