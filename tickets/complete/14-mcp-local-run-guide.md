description: A mock launch mode was added to the custom Meta MCP server plus a step-by-step guide so anyone can try the connect → read → propose → approve → execute loop locally without a real Meta account.
files: src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md, .mcp.json, README.md, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What shipped

A **mock launch mode** (`--mock` / `META_MCP_MOCK=1`) for the custom `meta-suite` MCP server plus a
step-by-step local run guide, so an operator can exercise the full guarded write loop (connect → read →
propose → approve → execute → verify) with **no `META_ACCESS_TOKEN` and zero live Meta calls**.

- `mcp_server.py`: shared mock entity constants (`MOCK_ACCOUNT`/`MOCK_CAMPAIGN`/`MOCK_ADSET`/`MOCK_AD`
  + insight/delivery-estimate), `build_mock_reader()` (a `FakeMetaReader` seeded for every read method),
  `_MockWriteClient` (records writes, returns `{"success": True}` for both the validate and execute
  passes, PAUSED-by-default creates), an optional `client` param on `build_write_tools` threaded into
  `proposals.execute_plan` (no change to `proposals.py`), a `mock=` branch on `build_server` (skips
  `DirectMetaReader.from_env()`, wires the mock reader + write client, stderr banner), and a `--mock`
  flag on `main()` (default from `META_MCP_MOCK` via `_env_flag`). The guard pipeline and approval gate
  are untouched and real — only reads and the write itself are faked; `META_APPROVAL_SECRET` is still
  required to execute.
- `.mcp.json`: `meta-suite` promoted from `_candidateMcpServers` into `mcpServers`; note moved to a
  root-level `_meta_suite_note`.
- Docs: new H2 **"Run the Meta MCP server locally"** in `META_API_SETUP.md` (install → secret → mock
  launch → connect → scripted 6-step session → troubleshooting → go-live → single-operator note), plus
  cross-links from `README.md` and `META_ACTION_WORKFLOW.md`.
- Tests: 6 net-new mock tests. Full suite **456 passed**.

## Review findings

**Verdict: accept. No major findings; no new tickets filed. No inline changes were required.**

Checked with fresh eyes against the implement diff (`5cd4b16`), then the handoff. Ran the full suite
(`python3 -m pytest tests/test_meta_ads_analysis.py -q` → **456 passed**), byte-compiled both changed
source files (OK), and validated `.mcp.json` (valid JSON; `mcpServers` = `code-search` + `meta-suite`;
old `_candidateMcpServers._meta_suite_note` removed, new root-level `_meta_suite_note` present). No
ruff/mypy/pre-commit is configured in this repo, so lint = compile + the test suite.

What was scrutinized and found:

- **Adversarial: unstubbed reads in mock mode (implementer's flagged angle).** Drove the propose surface
  (`propose_set_status`, `set_daily_budget`, `rename`, `set_creative`, `set_age_range`, `set_genders`,
  `set_placements`, `enable_ads`, `pause_ads`, `create_campaign`, `create_adset`, `lookalike`,
  `audience_rotation`, `advantage_disable`, `duplicate_ad`) against `build_mock_reader()` with
  `save_proposal` redirected to a temp dir. **No `NotImplementedError` / unstubbed read surfaced** — the
  mock reader covers the whole read surface. Concern resolved.
- **Drift guard.** `test_build_mock_reader_all_stubs_present` iterates `READ_TOOL_METHODS` and calls each
  tool, so a future read added to `READ_METHODS` but not seeded in `build_mock_reader` fails the test.
  Good.
- **Client threading.** `build_write_tools(..., client=)` → `execute_plan(..., client=)` → both the
  `validate_only=True` and `validate_only=False` passes of `apply_ops_plan`/`_update_entity`; mock
  `update_*` returns `{"success": True}` matching the real success shape. Verified against
  `control.apply_ops_plan` and `proposals.execute_plan`. Live mode passes `client=None` (asserted).
- **Doc accuracy (docs treated as out-of-date until verified).** Confirmed live behavior matches the
  guide: `build_server_info()` returns exactly the documented shape (`name: meta-ads-mcp`,
  `live_calls_enabled: true`, `approval_configured: false` with no secret); `preview_plan` renders
  `would_send` **only** for approved items (`would_send: None` otherwise) — so the guide's judgment call
  to run preview *after* approve is correct; all three cross-links use the `#run-the-meta-mcp-server-locally`
  anchor that matches the new H2; PowerShell shell style is consistent with the rest of `META_API_SETUP.md`.
  `_resolve_account("act_mock001")` → `(None, "act_mock001")` (starts with `act_`), and the fake reader
  ignores the id, so the scripted `list_campaigns("act_mock001")` returns the mock campaign.
- **Judgment calls in the handoff** (approval-gate name `PlanStatusApprovalGate` vs the ticket's
  non-existent `AlwaysApproveGate`; doc-section placement at end of the Read-backend section to preserve
  heading hierarchy; preview-after-approve ordering; root-level `.mcp.json` note; `pyproject.toml`
  untouched because the `server` extra + entry point already exist) — all reviewed and **sound**.

Findings (all **minor / accepted**, nothing fixed inline — see rationale):

1. **Mock proposals persist to the real `reports/` tree.** `build_write_tools._finalize` calls
   `proposals.save_proposal` with no `reports_root`, and `preview_plan`/`execute_plan` default to
   `DEFAULT_REPORTS_ROOT` (`PROJECT_ROOT/reports`). A `propose_*` over a *running* mock server therefore
   writes to `reports/account/<run_date>/proposals/` (account_slug is `None` → `"account"`). **Not
   fixed:** this is identical to live-mode behavior (live also persists there), the tree is gitignored,
   and scoping a mock-only reports root would thread a new param through `_finalize`/`preview`/`execute`
   and change live semantics — disproportionate for a gitignored demo artifact. Documented here as an
   accepted risk; a future mock-scoped reports root is optional polish, not required.
2. **`propose_duplicate_ad` is not exercisable in mock mode.** It reads a creative id off the source ad;
   `MOCK_AD` carries none, so it raises a clean `ValueError` ("Could not read a creative id …"), not a
   crash. It is **not** part of the scripted first session (which uses `set_status`), so no action.
   Adding `"creative": {"id": ...}` to `MOCK_AD` would enable a duplicate-ad demo later if wanted —
   noted, not done (avoids churn / possible ripple on `set_creative` read-backs).

Coverage note: `test_mock_execute_records_writes_and_makes_no_live_call` uses the no-op
`PlanStatusApprovalGate`, so the mock full-loop test does not itself exercise the HMAC gate the running
server uses — but HMAC sign/verify is covered independently by
`test_approve_plan_cli_signs_and_gate_verifies`. Combined coverage is adequate.

Empty finding categories, stated explicitly: **no correctness bugs**, **no type-safety issues** (client
annotated `Any | None`, reader annotated in the mock branch), **no resource-cleanup issues** (mock opens
no sockets/files), and **no regressions** (all 456 pre-existing + new tests pass) were found.
