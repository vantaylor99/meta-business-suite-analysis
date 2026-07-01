description: A mock launch mode was added to the custom Meta MCP server plus a step-by-step guide so anyone can try the connect → read → propose → approve → execute loop locally without a real Meta account.
files: src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, .mcp.json, README.md, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What was built

Four coordinated changes let an operator exercise the whole guarded write loop with **zero live Meta
calls** and no `META_ACCESS_TOKEN`:

1. **Mock mode in `mcp_server.py`** (`--mock` flag / `META_MCP_MOCK=1`):
   - Module-level shared entity constants `MOCK_ACCOUNT`, `MOCK_CAMPAIGN`, `MOCK_ADSET`, `MOCK_AD`
     (+ `MOCK_ACCOUNT_ID = "act_mock001"`, `MOCK_INSIGHT`, `MOCK_DELIVERY_ESTIMATE`) so the reader and
     the write client stay in sync.
   - `build_mock_reader()` → a `FakeMetaReader` pre-seeded for `act_mock001` covering **every**
     `READ_TOOL_METHODS` name. `iter_paginated` is seeded as a **callable** (FakeMetaReader invokes
     callable stubs with the positional path arg).
   - `_MockWriteClient` — records `(method, id, params, validate_only)` writes and returns
     `{"success": True}` for both the validate and execute passes; also implements the `get_*` read-backs
     and authoring `create_*` (returning `effective_status: "PAUSED"`), so PAUSED-by-default is never
     violated and outcome verification is consistent.
   - `build_write_tools(reader, approval_gate, client=None)` gained an optional `client`; its
     `execute_plan` closure passes it straight to `proposals.execute_plan(..., client=client)`. This
     short-circuits `proposals.execute_plan`'s lazy `client_from_env()` fallback **without any change to
     `proposals.py`**.
   - `build_server(host, port, *, mock=False)` — mock branch skips `DirectMetaReader.from_env()` (no
     token), wires the mock reader + `_MockWriteClient`, and prints the banner
     `[mock mode] No live Meta calls will be made. Account: act_mock001` to **stderr**.
   - `main()` adds `--mock` (default from `META_MCP_MOCK` via a new `_env_flag` helper) and threads it to
     `build_server`.
   - **The guard pipeline is untouched and real** — propose → human-approve → validate → execute →
     verify, the same approval gate selection, same grounding/review. Only reads and the write itself are
     faked. `META_APPROVAL_SECRET` is still required to execute (a missing secret leaves the fail-closed
     `DeniedApprovalGate` in place, and `server_info` reports `approval_configured: false`).

2. **`.mcp.json`** — `meta-suite` promoted from `_candidateMcpServers` into `mcpServers` (HTTP URL
   unchanged). The community `meta-ads-read` candidate and `code-search` are untouched.

3. **Docs** — new H2 **"Run the Meta MCP server locally"** in `docs/META_API_SETUP.md` (install → generate
   secret → mock launch → connect → scripted 6-step first session → troubleshooting → go-live opt-in →
   single-operator note), plus cross-links from `README.md` (Hybrid Meta integration) and
   `docs/META_ACTION_WORKFLOW.md` (end of the MCP guarded-write path section). The stale "parked under
   `_candidateMcpServers` / only code-search runs" paragraph in META_API_SETUP.md was corrected to
   "promoted; start the process first".

## How to validate

- **Full suite:** `python3 -m pytest tests/test_meta_ads_analysis.py -q` → **456 passed** (was 456 before
  the mock tests were added; net-new mock tests included). Requires `pip install -e '.[dev,server]'` first
  (this environment had no deps installed).
- **New tests** (end of the test file): `test_build_mock_reader_all_stubs_present`,
  `test_mock_write_client_update_returns_success_and_records`,
  `test_build_server_mock_skips_token_and_server_info_reports_no_secret`,
  `test_build_write_tools_threads_client_into_execute_plan`,
  `test_mock_execute_records_writes_and_makes_no_live_call` (full guarded loop on the mock rig with
  `reports_root=tmp_path`), `test_main_mock_flag_and_env_propagate_to_build_server`.
- **Real-FastMCP smoke** (performed, not a committed test): `build_server(mock=True)` with NO token +
  a secret set → registers `server_info`, all 13 reads, and the write surface; `server_info` reports
  `approval_configured: true`; `list_campaigns("act_mock001")` returns the mock campaign;
  `propose_set_status(ad_mock001, PAUSED)` returns **2 ops** (the ad + the appended companion ad-set
  pause). No socket is bound by `build_server`.

## Judgment calls / deviations from the ticket (please sanity-check)

- **Approval gate name.** The ticket's Phase-4 pseudocode references `AlwaysApproveGate()`, which **does
  not exist** in the codebase. Used the real `proposals.PlanStatusApprovalGate()` (the no-op gate the
  existing guarded-write tests use) throughout. Behavior is equivalent for the tests' intent.
- **Doc section placement.** The ticket said "H2, after the *Our custom Meta MCP server (local)* H3
  subsection". Placing an H2 immediately after that H3 would orphan the following *Official Meta hosted
  MCP server (OAuth)* H3 under the new section. To preserve heading hierarchy, the new H2 sits at the
  **end of the "Read backend" section** (after the OAuth subsection), before `## Notes`. The anchor
  `#run-the-meta-mcp-server-locally` (used by all three cross-links) is unaffected.
- **Scripted-session order.** The ticket ordered `preview_plan` (step 4) **before** `approve_plan` (step
  5). But `proposals.preview_plan` only renders `would_send` for ops whose status is already `approved`
  (before approval it reports "not approved — would be skipped"). So the guide runs preview **after**
  approval and calls this out explicitly. If the reviewer prefers the literal ticket order, revert — but
  the current order produces meaningful output.
- **`.mcp.json` note placement.** JSON has no comments. Rather than an inner `_note` key inside the
  `meta-suite` server object (which an MCP client's per-server schema might reject), the note lives in a
  **root-level `_meta_suite_note`** key — sibling to the existing root-level `_candidateMcpServers`, a
  pattern the repo already tolerates. The server object itself carries only `type` + `url`.
- **`pyproject.toml` not changed.** Listed in the ticket's `files:` but no edit was needed — the `server`
  extra and the `meta_mcp_server` entry point already exist.

## Known gaps / risks for the reviewer to probe

- **Mock proposals persist to the real `reports/` tree.** `build_write_tools`' `_finalize` calls
  `proposals.save_proposal(...)` with **no `reports_root`**, so a `propose_*` call over the *running*
  mock server writes to the default `reports/<slug|account>/<run_date>/proposals/` (gitignored). The
  committed end-to-end test avoids this by driving `save_proposal`/`execute_plan` directly with
  `reports_root=tmp_path`; the guarded closures themselves are **not** unit-tested end-to-end for that
  reason (only smoke-tested manually, which left a `reports/account/...` artifact that was removed). If
  mock runs should not touch the real reports tree, that is a follow-up (e.g. a mock-scoped reports root)
  — flag if it matters.
- **`test_read_tools_register_on_real_fastmcp_and_map_errors`** is `pytest.importorskip("mcp")`-guarded.
  It **ran** here because `.[server]` was installed; on a CI runner without the `server` extra it silently
  skips, so the real-FastMCP registration of the write surface is not a hard floor there.
- **Live path is unverified end-to-end** (no real token available). The go-live section documents that
  `ads_read` reads + validates (execute fails validation with a clear `ads_management` scope error → zero
  spend risk) and that `ads_management` is needed to actually write, but this was not exercised live.
- **Banner uses `print(..., file=sys.stderr)`** (no logging framework in this module) — intentional but
  worth a glance.
- Adversarial angles worth a look: does any exposed `propose_*` in mock mode read a method the mock reader
  does **not** stub (→ `NotImplementedError`)? The scripted path (`propose_set_status`) and all 13 read
  tools are covered; the bulk/rotation/authoring propose tools were not each driven in mock mode — a
  reviewer could fan those out against `build_mock_reader()` to confirm no unstubbed read surfaces.
