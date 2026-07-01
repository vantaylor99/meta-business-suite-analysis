description: Stand up the empty-but-running skeleton of our own Meta MCP server on this machine — a process Claude can connect to that exposes one health/info tool and makes zero live account calls — so later tickets can hang the real read and write tools on it.
prereq:
files: pyproject.toml, .mcp.json, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, AGENTS.md
difficulty: medium
----
## Why

We are moving the operational code (reads + guarded writes) out of the CLI and into a **custom Meta
MCP server we own**, so every specialist gets identical tools + guardrails through a connector rather
than a checked-out repo (long-term: bundled into Claude Cowork accounts). This ticket is only the
**foundation**: a server process that starts, reports health, and can be connected to from an MCP
client over HTTP — **no Meta reads or writes yet**. Those land in `mcp-read-tools` and
`mcp-guarded-write-tools`, which depend on this scaffold.

## Design (resolved — build exactly this)

### New module: `src/meta_ads_analysis/mcp_server.py` (thin entrypoint)

A FastMCP server (official Python `mcp` SDK) exposing **one** tool, `server_info`. The module must
**not** embed any Meta/business logic — it imports and exposes existing package functions only. Shape:

```python
"""Custom Meta MCP server — thin entrypoint (scaffold; no live Meta tools yet)."""
from __future__ import annotations
import argparse, os

# Mirror the requests-missing pattern in meta_api.py: import guarded at module load,
# actionable SystemExit raised at use site — never a bare ImportError traceback.
try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - exercised only without the `server` extra
    FastMCP = None

from . import __version__
from .meta_api import meta_api_version_from_env          # NEW helper, see below
from .reader_provider import reader_backend_from_env     # NEW helper, see below

SERVER_NAME = "meta-ads-mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

def build_server_info() -> dict:
    """Pure, token-free health/info payload. Unit-testable without binding a socket."""
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "meta_api_version": meta_api_version_from_env(),   # no token required
        "read_backend": reader_backend_from_env(),         # "direct" | "mcp"
        "live_calls_enabled": False,   # scaffold makes ZERO live Meta calls; later tickets own this flag
    }

def build_server(host: str, port: int):
    if FastMCP is None:
        raise SystemExit(
            "The 'mcp' package is required to run the Meta MCP server. "
            "Install the server extra with `pip install -e .[server]`."
        )
    mcp = FastMCP(SERVER_NAME, host=host, port=port)

    @mcp.tool()
    def server_info() -> dict:
        """Report server identity, configured Meta API version, selected read backend, and whether
        live Meta calls are enabled (always false in the scaffold)."""
        return build_server_info()

    return mcp

def main() -> None:
    parser = argparse.ArgumentParser(description="Run the custom Meta MCP server (HTTP).")
    parser.add_argument("--host", default=os.environ.get("MCP_SERVER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_SERVER_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    mcp = build_server(args.host, args.port)
    try:
        mcp.run(transport="streamable-http")
    except OSError as exc:  # port in use / bad host bind
        raise SystemExit(f"Could not start MCP server on {args.host}:{args.port}: {exc}") from exc
```

> **Verify against the installed SDK version.** Confirm the pinned `mcp` version accepts
> `FastMCP(name, host=..., port=...)` and supports `mcp.run(transport="streamable-http")`. If a given
> version instead exposes host/port via `mcp.settings.host` / `mcp.settings.port`, set those after
> construction — but keep the CLI-flag / `MCP_SERVER_HOST` / `MCP_SERVER_PORT` precedence exactly as
> above (explicit flag > env > local default). The streamable-http mount path is `/mcp`, so the client
> URL is `http://<host>:<port>/mcp`.

### Two small shared helpers (keep env-reading out of the server module — DRY)

The `server_info` payload must work **with no `META_ACCESS_TOKEN` set**, so it cannot call
`client_from_env` (that constructs a client and raises without a token) or `reader_from_env` with
`mcp` (raises without an executor). Extract the token-free bits both the existing constructors and the
server can share:

- **`meta_api.py` → `meta_api_version_from_env(api_version: str | None = None) -> str`**: return
  `api_version or os.environ.get("META_API_VERSION") or DEFAULT_META_API_VERSION`. Then refactor
  `client_from_env` to call this helper for its `effective_version` line (no behavior change — it
  currently inlines that exact expression). This is the single source of the version-resolution rule.
- **`reader_provider.py` → `reader_backend_from_env() -> str`**: return
  `(os.environ.get(READER_BACKEND_ENV) or "direct").strip().lower()` normalized to the same values
  `reader_from_env` accepts. Refactor `reader_from_env` to obtain its `backend` string from this helper
  (no behavior change). `server_info` reports this raw string; it does **not** validate/raise on an
  unknown backend (that stays `reader_from_env`'s job — `server_info` is a health probe, not a
  constructor). Report whatever is configured, verbatim-normalized.

### `pyproject.toml`

- Add a **new optional extra** so the CSV/analysis install stays lean:
  ```toml
  [project.optional-dependencies]
  server = [
    "mcp>=1.9,<2",
  ]
  ```
  (Verify `mcp>=1.9` provides `mcp.server.fastmcp.FastMCP` + streamable-http on Python 3.13; bump the
  floor if the installed resolver pulls something without streamable-http. Do **not** add `mcp` to the
  base `dependencies`.)
- Add a console script:
  ```toml
  [project.scripts]
  meta_mcp_server = "meta_ads_analysis.mcp_server:main"
  ```

### `.mcp.json` — parked client entry (NOT launched)

Add our own server under the existing non-launched `_candidateMcpServers` block (only entries under
`mcpServers` are started). **Use a server key distinct from `meta-ads`** — the official/community
connector already owns the `mcp__meta-ads__*` prefix and its write tools are deny-listed in
`.claude/settings.json`. Our server's tools must carry a different prefix so our *sanctioned gated*
tools are never caught by that deny-list. Use key **`meta-suite`** (tools become `mcp__meta-suite__*`).
Entry (HTTP / streamable-http URL form, no token embedded):

```jsonc
"meta-suite": {
  "type": "http",
  "url": "http://127.0.0.1:8765/mcp"
  // Parked until mcp-read-tools / mcp-guarded-write-tools land. Launch locally with
  // `meta_mcp_server` (install the `server` extra first). The role/token header for
  // multi-user/hosted use is a later concern (see backlog mcp-role-based-access-tiers);
  // local single-operator use needs no header.
}
```
Extend the `_README` / add a short `_meta_suite_note` string in `_candidateMcpServers` explaining this
is **our own** server (distinct from the community `meta-ads-read` read candidate), parked until the
read/write tool tickets land.

### Docs

- **`docs/META_API_SETUP.md`**: add a short section ("Our custom Meta MCP server (local, scaffold)")
  covering: install the extra (`pip install -e .[server]`), launch (`meta_mcp_server --host 127.0.0.1
  --port 8765`, with `MCP_SERVER_HOST` / `MCP_SERVER_PORT` fallbacks), the client URL
  (`http://127.0.0.1:8765/mcp`), and that it currently exposes only `server_info` and makes **zero live
  Meta calls**. Note it's distinct from the community `meta-ads-read` candidate documented above it.
- **`AGENTS.md`**: under **Hybrid Meta integration**, add one short paragraph (or a pointer bullet)
  noting the custom server scaffold exists (`meta_mcp_server`, key `meta-suite`, HTTP, `server_info`
  only, mocks-only) and that reads/writes land in the follow-on tickets. Keep it a pointer — the write
  catalog table stays the source of truth.

## Edge cases & interactions

- **`server` extra / SDK not installed** → launching `meta_mcp_server` must produce the clear
  actionable `SystemExit` (`pip install -e .[server]`), **not** an `ImportError` traceback. Achieved by
  the module-level guarded import + raise-at-use-site pattern (mirrors `meta_api.py` `requests`).
- **`server_info` with no `META_ACCESS_TOKEN`** → returns the full payload (backend + version), never
  raises. This is why it uses the token-free helpers, not `client_from_env`.
- **Port already in use / bad host** → fail fast with a readable `SystemExit` naming host:port (the
  `OSError` wrapper), not a raw uvicorn/anyio traceback.
- **Unknown `META_READER_BACKEND`** → `server_info` reports the normalized string as-is and does not
  raise (health probe, not constructor). Only `reader_from_env` validates and raises.
- **Coexistence with the stdio `code-search` server** → our server is a separate HTTP process on its
  own port; the parked `.mcp.json` entry is not auto-launched, and only `code-search` runs under
  `mcpServers`. Confirm by reasoning + a manual local launch (do not add an automated test that binds a
  socket — tests must not open network ports). No shared state, no interference.
- **`build_server_info` purity** → it must be callable and assertable without constructing `FastMCP`
  or binding a socket, so it stays a plain function the tool merely wraps.
- **Refactor safety** → extracting `meta_api_version_from_env` / `reader_backend_from_env` must be
  behavior-preserving; the existing reader/version tests (see `tests/test_meta_ads_analysis.py`
  ~line 7794–7828 for `reader_from_env`) must still pass unchanged.

## Build-safety (carried, non-negotiable)

- **MOCKS ONLY.** This scaffold makes **zero live Meta calls** (it has no Meta tools yet). No test may
  open a network socket or touch a real account/token. `live_calls_enabled` is hardcoded `False`.
- The server module is a **thin entrypoint**: no Meta/business logic; it imports existing package
  functions. Keep the CLI and the server as two frontends over one library.

## TODO

### Phase 1 — shared helpers (behavior-preserving refactor)
- Add `meta_api_version_from_env(api_version=None)` to `meta_api.py`; refactor `client_from_env` to use it.
- Add `reader_backend_from_env()` to `reader_provider.py`; refactor `reader_from_env` to use it.
- Run the existing suite to confirm no behavior change: `python -m pytest tests/ -q 2>&1 | tee /tmp/pytest-refactor.log`.

### Phase 2 — server module + packaging
- Create `src/meta_ads_analysis/mcp_server.py` with `build_server_info`, `build_server`, `main` as above.
- Add the `server` optional extra and the `meta_mcp_server` console script to `pyproject.toml`.
- `pip install -e '.[server]'` and verify the pinned `mcp` version's `FastMCP(host=, port=)` +
  `run(transport="streamable-http")` API; adjust host/port wiring (constructor vs `.settings`) to match.

### Phase 3 — client config + docs
- Add the parked `meta-suite` HTTP entry (+ note) under `_candidateMcpServers` in `.mcp.json`.
- Extend `docs/META_API_SETUP.md` and add the pointer to `AGENTS.md`.

### Phase 4 — tests (mocks only, no sockets)
- `build_server_info` with `META_ACCESS_TOKEN` / `META_READER_BACKEND` / `META_API_VERSION` unset →
  `{name: "meta-ads-mcp", version: <__version__>, meta_api_version: DEFAULT_META_API_VERSION,
  read_backend: "direct", live_calls_enabled: False}`.
- `build_server_info` with `META_READER_BACKEND=mcp` → `read_backend == "mcp"`; with
  `META_API_VERSION=v99.0` → `meta_api_version == "v99.0"`.
- `meta_api_version_from_env` / `reader_backend_from_env` return the expected values under
  set/unset/override env (monkeypatch).
- SDK-missing path: monkeypatch `mcp_server.FastMCP = None`, assert `build_server(...)` raises
  `SystemExit` whose message names `.[server]`.
- Assert `live_calls_enabled` is `False` (mocks-only guardrail made explicit in a test).
- Run: `python -m pytest tests/ -q 2>&1 | tee /tmp/pytest-scaffold.log`.

### Handoff
- Do a manual local smoke launch (`meta_mcp_server`) only if convenient and it returns quickly; if it
  blocks (it serves forever), skip it under the runner and document that a human should connect a client
  and call `server_info` to confirm end-to-end. Do **not** leave a server process running.
