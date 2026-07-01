description: Add a mock launch mode to the MCP server and write a step-by-step local-run guide so you can try the connect → read → propose → approve → execute loop without a real Meta account.
files: src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, .mcp.json, README.md, docs/META_ACTION_WORKFLOW.md, pyproject.toml
difficulty: medium
----

## Context

The custom MCP server (`meta_mcp_server`) is fully built — reads + guarded writes + HMAC approval gate —
but stays parked in `_candidateMcpServers` and has no mock posture, so there is no documented path to
actually try it. This ticket adds:

1. **Mock mode** (`--mock` / `META_MCP_MOCK=1`): server launches without a real `META_ACCESS_TOKEN`,
   uses a `FakeMetaReader` pre-seeded with one fake account (`act_mock001`), and routes `execute_plan`
   through a `MockWriteClient` that records ops and returns success without calling Meta.

2. **"Run the Meta MCP server locally" guide** in `docs/META_API_SETUP.md`: install → generate secret →
   mock launch → connect → scripted first session (`server_info` → read → propose → approve → execute)
   → go-live steps. Mock by default; live is an explicit opt-in.

3. **`.mcp.json` promotion**: move `meta-suite` from `_candidateMcpServers` → `mcpServers` so Claude
   Code sees it (HTTP URL, server must be started first).

4. **Cross-links** in `README.md` (under Hybrid Meta integration) and `docs/META_ACTION_WORKFLOW.md`
   (under the MCP guarded-write path section).

## Architecture decisions

### Mock mode design (`mcp_server.py`)

`build_server(host, port)` → `build_server(host, port, *, mock=False)`.

When `mock=True`:
- Skip `DirectMetaReader.from_env()` (no `META_ACCESS_TOKEN` required).
- Use `build_mock_reader()` (returns a `FakeMetaReader` pre-seeded for `act_mock001`).
- Build a `MockWriteClient` and pass it through `build_write_tools(reader, gate, client=mock_client)`.
- Log a startup banner: `"[mock mode] No live Meta calls will be made. Account: act_mock001"`.

`build_write_tools(reader, approval_gate, client=None)` gets an optional `client` param. The
`execute_plan` closure captures it and passes it on to `proposals.execute_plan(..., client=client)`.
`proposals.execute_plan` already has `client=None` → `client_from_env()` fallback (line 793); the
mock client short-circuits that fallback without any change to `proposals.py`.

`main()` adds:
```
--mock          flag   (also read META_MCP_MOCK=1)
```

### `build_mock_reader()` — pre-seeded stubs

`FakeMetaReader` stubs for `act_mock001`. The scripted session uses `propose_set_status` on `ad_mock001`
(pausing the only ACTIVE ad in `adset_mock001`), which drives the deepest read path:
`build_single_op_plan` (no live reads — structural abstain) → `append_last_active_ad_pause` (reads
`get_ad` + `iter_paginated` for all ads). All other stubs cover the read-tool call in step 2 of
the session.

Required stubs (minimum; add others as `[]` or `{}` no-ops for completeness):

| Method | Return value |
|---|---|
| `get_account` | `{"id": "act_mock001", "name": "Demo Account", "account_status": 1, "currency": "USD"}` |
| `list_campaigns` | `[{"id": "campaign_mock001", "name": "Demo Campaign", "status": "ACTIVE", "effective_status": "ACTIVE", "objective": "OUTCOME_TRAFFIC"}]` |
| `list_adsets` | `[{"id": "adset_mock001", "name": "Demo Ad Set", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "campaign_mock001", "daily_budget": "10000", "targeting": {}}]` |
| `fetch_ads` | `[{"id": "ad_mock001", "name": "Demo Ad", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "adset_mock001"}]` |
| `get_ad` | `{"id": "ad_mock001", "name": "Demo Ad", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "adset_mock001"}` |
| `get_adset` | `{"id": "adset_mock001", "name": "Demo Ad Set", "status": "ACTIVE", "effective_status": "ACTIVE", "campaign_id": "campaign_mock001", "daily_budget": "10000", "targeting": {}}` |
| `get_campaign` | `{"id": "campaign_mock001", "name": "Demo Campaign", "status": "ACTIVE", "effective_status": "ACTIVE"}` |
| `iter_paginated` | `[{"id": "ad_mock001", "name": "Demo Ad", "status": "ACTIVE", "effective_status": "ACTIVE", "adset_id": "adset_mock001"}]` (callable so it returns regardless of path arg) |
| `fetch_insights` | `[{"date_start": "2026-06-01", "date_stop": "2026-06-30", "spend": "100.00", "impressions": "5000", "clicks": "200"}]` |
| `list_custom_audiences` | `[]` |
| `list_pixels` | `[]` |
| `list_custom_conversions` | `[]` |
| `search_targeting` | `[]` |
| `get_delivery_estimate` | `{"estimate_dau": 10000, "estimate_mau": 50000}` |

`iter_paginated` stub must be a **callable** (`lambda path, params=None: [...]`) because
`FakeMetaReader._result` calls it with positional path arg. All other stubs can be plain values.

### `MockWriteClient`

New class in `mcp_server.py` (private, mock-only). Implements the write methods `apply_ops_plan`
calls via `_update_entity` → `client.update_ad / update_adset / update_campaign`. Also needs
`get_ad / get_adset / get_campaign` for the re-read that `as_reader(client)` would perform if
the explicit `reader` weren't passed — but since we always pass a `reader`, only the update methods
are strictly needed. Implement all read methods too (`get_ad`, `get_adset`, `get_campaign`) for
safety, returning the same mock data as `build_mock_reader()` so post-execute verification works.

```python
class _MockWriteClient:
    """No-op write client for --mock mode: records calls, returns success, never contacts Meta."""
    def __init__(self):
        self.writes: list[tuple[str, str, dict, bool]] = []  # (method, id, params, validate_only)
    def update_ad(self, ad_id, *, params, validate_only=False):
        self.writes.append(("update_ad", ad_id, params, validate_only)); return {"success": True}
    def update_adset(self, adset_id, *, params, validate_only=False):
        self.writes.append(("update_adset", adset_id, params, validate_only)); return {"success": True}
    def update_campaign(self, campaign_id, *, params, validate_only=False):
        self.writes.append(("update_campaign", campaign_id, params, validate_only)); return {"success": True}
    def get_ad(self, ad_id, *, fields): return MOCK_AD
    def get_adset(self, adset_id, *, fields): return MOCK_ADSET
    def get_campaign(self, campaign_id, *, fields): return MOCK_CAMPAIGN
    # Authoring creates (for authoring plans in mock mode):
    def create_campaign(self, *a, **kw): return {"id": "campaign_mock_new"}
    def create_adset(self, *a, **kw): return {"id": "adset_mock_new"}
    def create_ad(self, *a, **kw): return {"id": "ad_mock_new"}
    def create_lookalike_audience(self, *a, **kw): return {"id": "audience_mock_new"}
```

Define `MOCK_AD`, `MOCK_ADSET`, `MOCK_CAMPAIGN` as module-level constants (dicts) shared by both
`build_mock_reader()` and `_MockWriteClient`. This keeps mock data in sync.

### `.mcp.json` change

Move `meta-suite` from `_candidateMcpServers` into `mcpServers`. Keep its `type: "http"` URL config
unchanged — Claude Code connects to the already-running process. Add a brief inline comment explaining
that the server must be started first (`meta_mcp_server --mock` for mock, or with a real token for
live). Leave `code-search` unchanged. Move the verbose `_meta_suite_note` comment into the new section
(updated to reflect promoted status).

```jsonc
"mcpServers": {
  "code-search": { ... },
  "meta-suite": {
    "_note": "Our custom Meta MCP server. Start it first: `meta_mcp_server --mock` (mock mode, no live calls) or `META_ACCESS_TOKEN=<token> meta_mcp_server` (live). See docs/META_API_SETUP.md.",
    "type": "http",
    "url": "http://127.0.0.1:8765/mcp"
  }
}
```

Note: JSON does not allow comments — use a `_note` key (non-standard but tolerated by most MCP
clients) or strip the comment before shipping. Strip the comment; move the note to an outer
`_notes` section or just to a README reference.

### Documentation section (META_API_SETUP.md)

Add a new H2 section **"Run the Meta MCP server locally"** after the existing "Our custom Meta MCP
server (local)" subsection (approximately line 168). The section has these sub-steps:

1. **Install the server extra** — `pip install -e .[server]`
2. **Generate an HMAC approval secret** — one-time, store out of repo:
   ```powershell
   python -c "import secrets; print(secrets.token_hex(32))"
   # -> copy the output as META_APPROVAL_SECRET
   ```
3. **Launch in mock mode** (no real token, no live calls):
   ```powershell
   $env:META_APPROVAL_SECRET="<hex from above>"
   meta_mcp_server --mock
   # Server starts at http://127.0.0.1:8765/mcp
   # [mock mode] No live Meta calls. Account: act_mock001
   ```
4. **Connect Claude Code** — the `meta-suite` entry is already in `.mcp.json`; no edit needed.
   Confirm by asking Claude: `call server_info` or via `/mcp`.
5. **Scripted first session** (exact tool calls for the copy-paste experience):
   ```
   # Step 1: health check
   server_info()
   # → {"name":"meta-ads-mcp","live_calls_enabled":true,"approval_configured":true,...}

   # Step 2: read tool
   list_campaigns(ad_account_id="act_mock001", fields=["id","name","status"])
   # → [{"id":"campaign_mock001","name":"Demo Campaign","status":"ACTIVE",...}]

   # Step 3: propose a write
   propose_set_status(account="act_mock001", id="ad_mock001", level="ad", status="PAUSED")
   # → {"plan_id":"<uuid>","ops":[{"op":"set_status","id":"ad_mock001","status":"proposed",...},...]}
   # Note: a companion adset pause is appended (ad_mock001 is the only ACTIVE ad in its set).

   # Step 4: preview — write-free dry run (no shell needed)
   preview_plan(plan_id="<uuid from step 3>")
   # → shows the exact PATCH request that execute would send, without touching Meta.

   # Step 5: approve — OUT OF BAND in a separate shell (this is the human's step)
   $env:META_APPROVAL_SECRET="<same hex>"
   approve_plan --plan-id <uuid> --all
   # → Approved 2 ops. Signature written to proposal.

   # Step 6: execute
   execute_plan(plan_id="<uuid>")
   # [mock mode] → MockWriteClient recorded the write, verified via FakeMetaReader.
   # → {"executed":true,"plan_id":"<uuid>","results":[...]}
   ```
6. **Troubleshooting** — two quick bullets:
   - "Connection refused / server not found": make sure `meta_mcp_server --mock` is running in a
     terminal before connecting.
   - "Wrong port": check `--port` / `MCP_SERVER_PORT` vs. the URL in `.mcp.json`.
7. **Go live (opt-in)** — clearly marked opt-in section:
   - Use a **sandbox / test ad account** for the first live run (Meta's Ads Sandbox or a low-budget
     real account you control).
   - Set `META_ACCESS_TOKEN` to a token with `ads_read` (reads work; execute fails validation with a
     clear scope error — zero spend risk).
   - To actually execute: the token also needs `ads_management`. Verify next-day spend = $0 after the
     first pause. Consistent with the repo's build-safety rule.
   - Single-operator only — no multi-user/roles/headers. That's a separate backlog item.
8. **Single-operator note** — "This is a single-operator, local setup. Multi-user auth, roles, and
   Azure-hosted approval state are a separate backlog item (`mcp-role-based-access-tiers`). Do not
   treat this local rig as the production shape."

### Cross-links

- **`README.md`** — under the Hybrid Meta integration section, add one sentence after the existing
  docs cross-link: "For the local server run guide (mock mode + scripted first session) see
  [docs/META_API_SETUP.md → Run the Meta MCP server locally](docs/META_API_SETUP.md#run-the-meta-mcp-server-locally)."

- **`docs/META_ACTION_WORKFLOW.md`** — at the end of the "MCP guarded-write path" section (after
  the "local loop is: propose → approve_plan → execute_plan" block), add: "See [META_API_SETUP.md →
  Run the Meta MCP server locally](META_API_SETUP.md#run-the-meta-mcp-server-locally) for the
  step-by-step local launch, `.mcp.json` wiring, and scripted first session."

## Edge cases & interactions

- `_resolve_account("act_mock001", ...)` — the existing `act_`-prefix branch handles raw ids, so
  `act_mock001` routes cleanly (slug=None, ad_account_id="act_mock001"). No code change needed.
- `propose_set_status` for `ad_mock001` appends a companion ad-set pause. The scripted session
  should show both ops in the summary so the user understands the cascade. Document this in step 3.
- `iter_paginated` stub must be a **callable** (not a plain list) because FakeMetaReader calls
  `value(*args, **kwargs)` on callables — a plain list returned as a non-iterable-call would work
  too, but a callable avoids a subtle gotcha.
- `_MockWriteClient.update_*` with `validate_only=True` returns `{"success": True}` — the validate
  pass in `execute_plan` treats non-failure results as validation passed. Ensure none of these return
  error shapes.
- JSON does not support comments: the `_note` key in `.mcp.json` is a non-standard workaround. Strip
  it or use a top-level `_notes` object if MCP clients reject unknown keys.
- If `META_APPROVAL_SECRET` is not set in mock mode, `server_info()` returns
  `approval_configured: false` and `execute_plan` will be refused by `DeniedApprovalGate`. The guide
  MUST tell the user to set it before launching. Validate in tests: `build_server(mock=True)` with no
  secret should start and `server_info` should return `approval_configured: false`.
- The `meta-suite` entry in `mcpServers` will show a connection error in Claude Code whenever the
  server isn't running. This is expected; document it in the troubleshooting note.
- `MockWriteClient` authoring methods (`create_*`) return minimal `{"id": "..."}` dicts. If
  `apply_authoring_plan` reads more fields back in its outcome verification, the mock may return `{}`
  for unknown fields — acceptable for mock mode; add `"effective_status": "PAUSED"` to the returns
  to avoid surprises.

## TODO

Phase 1 — code changes (mcp_server.py)
- Define `MOCK_AD`, `MOCK_ADSET`, `MOCK_CAMPAIGN` module-level constants with realistic mock shapes
- Implement `_MockWriteClient` class (update + read methods)
- Implement `build_mock_reader()` → `FakeMetaReader(**stubs)` using the constants above
- Add optional `client=None` param to `build_write_tools`; update the `execute_plan` closure to pass it to `proposals.execute_plan`
- Add `mock=False` param to `build_server`; add the conditional branch (mock reader + mock client + startup banner)
- Add `--mock` arg to `main()`; read `META_MCP_MOCK` env var as fallback

Phase 2 — config (.mcp.json)
- Move `meta-suite` object from `_candidateMcpServers` into `mcpServers`
- Update its surrounding note to reflect promoted status and "start server first" requirement
- Leave `code-search` and the community `meta-ads-read` candidate untouched

Phase 3 — documentation
- Add "Run the Meta MCP server locally" H2 section to `docs/META_API_SETUP.md` (all sub-steps above)
- Add cross-link sentence to `README.md` under Hybrid Meta integration
- Add cross-link sentence to `docs/META_ACTION_WORKFLOW.md` at end of MCP guarded-write path section

Phase 4 — tests
- `test_build_mock_reader_all_stubs_present()` — instantiate `build_mock_reader()`, verify it covers all `READ_METHODS` keys that need stubs for the scripted session
- `test_mock_write_client_update_returns_success()` — `_MockWriteClient().update_ad(...)` → `{"success": True}`
- `test_build_server_mock_skips_token()` — `build_server(mock=True)` without `META_ACCESS_TOKEN` set should not raise
- `test_build_server_mock_server_info()` — server_info via `build_server_info()` works in mock mode (token-free, independent of reader)
- `test_build_write_tools_mock_client_passed_to_execute()` — `build_write_tools(fake_reader, AlwaysApproveGate(), client=mock_client)["execute_plan"]("plan_id")` calls `proposals.execute_plan` with the mock client (patch `proposals.execute_plan` to verify the kwarg)
