description: Review the empty-but-running skeleton of our own Meta MCP server — a local process that starts, answers a single health/info query, and makes zero live account calls — before later tickets hang the real read and write tools on it.
prereq:
files: pyproject.toml, .mcp.json, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, AGENTS.md
difficulty: medium
----
## What landed

The scaffold for **our own** Meta MCP server — a FastMCP (official Python `mcp` SDK) HTTP process
exposing exactly **one** tool, `server_info`, with **zero live Meta calls**. Reads/writes are
follow-on tickets (`mcp-read-tools`, `mcp-guarded-write-tools`). Built exactly to the resolved design
in the implement ticket; no design deviations.

### Files changed
- **`src/meta_ads_analysis/mcp_server.py`** (NEW) — thin entrypoint. `build_server_info()` (pure,
  token-free health payload), `build_server(host, port)` (constructs FastMCP, registers `server_info`),
  `main()` (argparse + `mcp.run(transport="streamable-http")`, `OSError`→`SystemExit` wrapper). The
  `mcp` SDK import is **guarded at module load** (`FastMCP = None` if absent); the actionable
  `SystemExit` naming `pip install -e .[server]` is raised at use site in `build_server` — mirrors the
  `requests`-missing pattern in `meta_api.py`. No Meta/business logic in the module.
- **`src/meta_ads_analysis/meta_api.py`** — new `meta_api_version_from_env(api_version=None)` (token-free
  version resolution: explicit arg > `META_API_VERSION` > `DEFAULT_META_API_VERSION`); `client_from_env`
  refactored to call it (behavior-preserving — it previously inlined the exact expression).
- **`src/meta_ads_analysis/reader_provider.py`** — new `reader_backend_from_env()` (token-free,
  construction-free; returns the normalized backend string, **does not validate/raise**);
  `reader_from_env` refactored to obtain its `backend` from it (behavior-preserving). Validation of an
  unknown backend stays in `reader_from_env`.
- **`pyproject.toml`** — new optional extra `server = ["mcp>=1.9,<2"]` (NOT added to base deps); new
  console script `meta_mcp_server = "meta_ads_analysis.mcp_server:main"`.
- **`.mcp.json`** — parked `meta-suite` HTTP entry (`http://127.0.0.1:8765/mcp`) under
  `_candidateMcpServers` (NOT launched) + a `_meta_suite_note`. Key deliberately distinct from the
  community `meta-ads`/`meta-ads-read` prefix so our `mcp__meta-suite__*` tools dodge the
  `mcp__meta-ads__*` write deny-list in `.claude/settings.json`.
- **`docs/META_API_SETUP.md`** — new "Our custom Meta MCP server (local, scaffold)" section (install,
  launch, host/port precedence, client URL, `server_info`-only, zero live calls).
- **`AGENTS.md`** — pointer paragraph under Hybrid Meta integration → Read model.

## Verified (what I actually ran)

- **Full suite green:** `python -m pytest tests/ -q` → **387 passed** (381 prior + 6 new;
  `/tmp/pytest-scaffold.log`). The pre-existing reader/version tests pass unchanged, confirming the
  Phase-1 refactor is behavior-preserving.
- **SDK API confirmed on the resolved version** (`mcp==1.28.1`, satisfies `>=1.9,<2`, Python 3.14 venv):
  `FastMCP(name, host=, port=)` accepted; `run(transport="streamable-http")` supported;
  `settings.streamable_http_path == "/mcp"` → client URL `http://<host>:<port>/mcp`. Host/port wiring is
  the plain constructor (no `.settings` fallback needed).
- **`build_server_info()` payload** (env unset): `{name: "meta-ads-mcp", version: "0.1.0",
  meta_api_version: "v22.0", read_backend: "direct", live_calls_enabled: False}`. Overrides reflected:
  `META_READER_BACKEND=mcp` → `read_backend=="mcp"`; `META_API_VERSION=v99.0` →
  `meta_api_version=="v99.0"`.
- **Tool registration (socket-free):** `await srv.list_tools()` → exactly `["server_info"]`.
- **Graceful degradation with the SDK absent** (simulated by blocking `import mcp`): the module still
  imports (`FastMCP is None`), `build_server_info()` still works, and `build_server(...)` raises
  `SystemExit` naming `.[server]`. So the tests run with **or without** the `server` extra installed.

### New tests (6, all MOCKS ONLY — no socket bound)
`test_meta_api_version_from_env_precedence`, `test_reader_backend_from_env_normalizes_and_defaults`,
`test_build_server_info_defaults_when_env_unset`, `test_build_server_info_reflects_backend_and_version_overrides`,
`test_build_server_info_live_calls_always_false`, `test_build_server_without_sdk_raises_actionable_systemexit`.

## Reviewer focus / use cases to exercise

- **Refactor safety (highest value):** confirm `meta_api_version_from_env` / `reader_backend_from_env`
  are pure extractions — diff `client_from_env` and `reader_from_env` against HEAD~; the only change
  should be delegation. The pre-existing `test_reader_from_env_*` tests are the guard.
- **`server_info` is token-free / never raises:** it must return the full payload with **no**
  `META_ACCESS_TOKEN` and even with an unknown `META_READER_BACKEND` (health probe, not constructor).
  Worth a test with `META_READER_BACKEND=garbage` asserting `read_backend=="garbage"` and no raise
  (I covered the helper's non-validation directly, but not the end-to-end `build_server_info` path with
  a garbage backend).
- **Prefix isolation:** verify `mcp__meta-suite__*` cannot collide with the `mcp__meta-ads__*`
  deny-list in `.claude/settings.json` — this is the whole reason for the distinct key and matters once
  write tools land.
- **`.mcp.json` still parses and only `code-search` launches** (under `mcpServers`); `meta-suite` sits
  under `_candidateMcpServers` and is not started.

## Known gaps (treat tests as a floor, not a finish line)

- **No end-to-end HTTP smoke in CI.** No automated test starts uvicorn and connects an MCP client over
  the wire — by design (tests must not bind sockets). I verified construction + tool registration +
  streamable-http path in-process only. **A human should do the manual smoke once:**
  `pip install -e .[server]` → `meta_mcp_server --host 127.0.0.1 --port 8765` → connect a client to
  `http://127.0.0.1:8765/mcp` and call `server_info`. Do **not** leave the process running (it serves
  forever). I did not run this under the runner because it blocks.
- **`main()` is untested.** The argparse host/port precedence (flag > `MCP_SERVER_HOST`/`MCP_SERVER_PORT`
  > default) and the `OSError`→`SystemExit` port-in-use wrapper are reasoned-only (both need process
  launch / socket bind). A reviewer could add a `monkeypatch`-of-`build_server`+`sys.argv` test for the
  precedence logic without binding a socket; the `OSError` wrapper is harder to hit without a real bind.
- **Only `mcp==1.28.1` was actually resolved/installed.** The `>=1.9` floor was not independently
  exercised; the FastMCP host/port + streamable-http API has been stable across that range, but if a
  reviewer wants certainty the floor could be pinned tighter or floor-tested.
- **`.venv` note:** `mcp` was installed into the local (gitignored) `.venv` for verification; it is not
  in the base deps and not committed. Other environments must `pip install -e .[server]` to run the
  server (but not to run the test suite).

## Build-safety (held)
MOCKS ONLY — zero live Meta calls, `live_calls_enabled` hardcoded `False` (explicit test). No test opens
a network socket. Server module is a thin entrypoint over existing library functions.
