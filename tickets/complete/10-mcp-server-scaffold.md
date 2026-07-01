description: Built and reviewed the empty-but-running skeleton of our own Meta MCP server — a local process that starts, answers a single health/info query, and makes zero live account calls — ready for later tickets to hang the real read and write tools on it.
prereq:
files: pyproject.toml, .mcp.json, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, AGENTS.md
difficulty: medium
----
## What landed

The scaffold for **our own** Meta MCP server — a FastMCP (official Python `mcp` SDK) HTTP process
exposing exactly **one** tool, `server_info`, with **zero live Meta calls**. Reads/writes are follow-on
tickets (`mcp-read-tools`, `mcp-guarded-write-tools`).

- **`src/meta_ads_analysis/mcp_server.py`** (NEW) — thin entrypoint: `build_server_info()` (pure,
  token-free health payload), `build_server(host, port)` (FastMCP + `server_info` registration; raises
  actionable `SystemExit` if the `server` extra is absent), `main()` (argparse host/port precedence +
  `mcp.run(transport="streamable-http")` with `OSError`→`SystemExit`). SDK import guarded at module load.
- **`src/meta_ads_analysis/meta_api.py`** — new token-free `meta_api_version_from_env`; `client_from_env`
  delegates to it (behavior-preserving).
- **`src/meta_ads_analysis/reader_provider.py`** — new token-free, non-validating `reader_backend_from_env`;
  `reader_from_env` delegates to it (behavior-preserving; validation stays in `reader_from_env`).
- **`pyproject.toml`** — optional `server = ["mcp>=1.9,<2"]` extra + `meta_mcp_server` console script.
- **`.mcp.json`** — parked `meta-suite` HTTP entry under `_candidateMcpServers` (NOT launched); key
  deliberately distinct from the community `meta-ads` prefix.
- **`docs/META_API_SETUP.md`**, **`AGENTS.md`** — scaffold documentation.

## Review findings

Adversarial pass over the implement diff (`b06bba0`), read before the handoff summary. Angles exercised:
SPP/DRY (helper extraction), error handling, resource cleanup, type safety, test coverage (happy /
edge / error paths), doc accuracy, and the security-relevant prefix-isolation claim.

**Checked and confirmed sound (no action needed):**
- **Refactor safety (highest-value check).** Diffed `client_from_env` and `reader_from_env` against
  `HEAD~`. Both are pure delegations — `client_from_env` now calls `meta_api_version_from_env(api_version)`
  (identical expression to the inlined original); `reader_from_env` obtains `backend` from
  `reader_backend_from_env()` (identical normalization) and keeps its own unknown-backend validation.
  The pre-existing `test_reader_from_env_*` / version tests pass unchanged → behavior-preserving.
- **`server_info` is token-free and never raises.** Uses only the token-free `*_from_env` helpers; no
  `META_ACCESS_TOKEN` access, no constructor call. Verified by code + tests (defaults, overrides,
  live-calls-always-false, and — added this pass — end-to-end with a garbage backend).
- **Prefix isolation.** `.claude/settings.json` deny-list is entirely `mcp__meta-ads__*`; our config key
  is `meta-suite` → `mcp__meta-suite__*`. The Claude Code tool namespace derives from the `.mcp.json`
  connection key (not the server's self-reported name), so our sanctioned gated write tools cannot be
  caught by the community deny-list once they land.
- **`.mcp.json` validity.** Parses; `meta-suite` sits under `_candidateMcpServers` and is not launched
  (only `code-search` runs under `mcpServers`).
- **SDK-missing graceful degradation.** Module imports with `FastMCP = None`; `build_server_info()` still
  works; `build_server(...)` raises an actionable `SystemExit` naming `pip install -e .[server]`.

**Minor — fixed inline this pass:**
- Closed the implementer's two flagged test gaps and one it noted for `build_server_info`. Added 3 tests
  (no socket bound): `test_main_host_port_precedence_flag_over_env_over_default` (flag > env > default,
  via `monkeypatch` of `build_server` + `sys.argv`), `test_main_wraps_oserror_as_actionable_systemexit`
  (`OSError` from `run()` → `SystemExit` naming host:port), and
  `test_build_server_info_does_not_raise_on_unknown_backend` (end-to-end health-probe path, not just the
  helper). `main()` is no longer untested.

**Observations (no action — acceptable for a scaffold):**
- `SERVER_NAME = "meta-ads-mcp"` (the FastMCP internal name echoed in the `server_info` payload) is
  cosmetically close to the community `meta-ads` prefix. It does **not** affect the tool namespace
  (which comes from the `.mcp.json` key `meta-suite`), so deny-list isolation is unaffected. Left as-is.
- `main()` parses `MCP_SERVER_PORT` with a bare `int(...)`, so a non-numeric env value would raise an
  uncaught `ValueError`. Acceptable for a local single-operator scaffold; not worth guarding yet.

**Major — none.** No new fix/plan/backlog tickets filed.

## Known gap carried forward (not blocking)
- **No end-to-end HTTP smoke in CI** — by design (tests must not bind sockets). Construction, tool
  registration, and the streamable-http path were verified in-process only. A human should do the manual
  smoke once when convenient: `pip install -e .[server]` → `meta_mcp_server --host 127.0.0.1 --port 8765`
  → connect an MCP client to `http://127.0.0.1:8765/mcp`, call `server_info`, then stop the process (it
  serves forever). This carries into `mcp-read-tools` / `mcp-local-run` where a live process is exercised
  anyway.

## Verified (what I ran)
- **Full suite green:** `python -m pytest tests/ -q` → **390 passed** (387 prior + 3 new this review;
  `/tmp/pytest-review-scaffold.log`).
- **No lint tooling configured** (no ruff/flake8/black/mypy in `pyproject.toml` or `AGENTS.md`); pytest is
  the validation surface.

## End
