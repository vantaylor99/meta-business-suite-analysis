description: Our own Meta MCP server can now make reversible account changes (pause/enable, budget, rename, creative, targeting), but only by routing every change through a propose → human-approve → validate → execute → verify safety flow — never as a raw one-shot API call.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/control.py, .claude/settings.json, .mcp.json, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## What shipped

The guarded control-ops write surface now lives behind our custom MCP server. Every write is a
capability boundary enforced *in the server* (not a prompt rule): `propose_* → preview_plan →
execute_plan`, with `execute_plan` the only tool that writes.

### New / changed code

- **`src/meta_ads_analysis/proposals.py`** (NEW, ~430 lines, pure library — no socket, no `mcp` SDK):
  - Proposal store: `save_proposal` (persists a reviewed plan under
    `reports/<slug>/<run_date>/proposals/<plan_id>.json`, returns a `plan_id` *reference*),
    `load_proposal` / `find_proposal_path` (resolve by id via `*/*/proposals/<id>.json` glob; clear
    `MetaApiError` on missing/ambiguous).
  - Approval seam: `ApprovalGate` Protocol + `ApprovalError` + default **`PlanStatusApprovalGate`**
    (no-op; relies on the apply invariant that only `status=="approved"` ops are sent).
  - `execute_plan(plan_id, *, approval_gate, reader, client=None, reports_root=...)`: load-by-id →
    idempotency guard → approval gate → **refuse if 0 approved ops** → lazy write client →
    **validate_only pass** → abort-if-any-failed → **execute pass** → audit artifact
    (`control.write_ops_results`, now carrying `plan_id`) → mark executed → **outcome verification**
    (re-read `effective_status`; emit `verify_next_day_spend` follow-up per executed PAUSE).
  - `preview_plan` (local, write-free dry run), `PLAN_APPLIERS` dispatch (only `"ops"` wired; map left
    open for authoring/rotation in ticket 12.5), scope-error helper (`scope_error_from_results` →
    `SCOPE_ERROR_MESSAGE`, surfaced as `MetaApiError` so the server maps it to a clean `ToolError`).
- **`control.py`**: `build_single_op_plan` (rename / set_creative / set_creative_features / the 4
  targeting ops — one op, structural-abstain grounding, `validate_op` + `review.review_ops_plan`);
  `append_last_active_ad_pause` (companion ad-set PAUSE when pausing a set's last ACTIVE ad);
  `write_ops_results` now includes `plan_id`.
- **`mcp_server.py`**: `build_write_tools(reader, approval_gate)` returns the 13 write callables;
  registered in `build_server` with `_wrap_tool_errors` (now also maps `ValueError` / `ApprovalError`);
  `build_server_info` gained `write_tools_enabled: True`; `_resolve_account` (slug/name or `act_` id);
  `_proposal_summary` (review-ready digest, never an approvable body).
- **`.claude/settings.json`**: intent `_comment` recording the deny-list rationale (see deviation below);
  every existing `mcp__meta-ads__*` deny rule kept.
- **`.mcp.json`**: refreshed the stale `_meta_suite_note` (server now exposes reads + gated writes).
- **`docs/META_ACTION_WORKFLOW.md`**: new "MCP guarded-write path" section (prefixes, tool surface,
  approval seam, media-upload exception, PAUSED≠delivery-stopped lesson).

## How to validate

- Full suite: `pytest -q` → **419 passed** (log: `/tmp/mcp-write-core.log`). Every new test is
  **mock-only**; no live Meta call anywhere.
- Key new tests (search `test_mcp_` / `test_build_write_tools`): no-approval refusal; approved →
  validate-then-execute → executed + audit + verify; `execute_plan` has **no plan-body param** (anti-
  forgery, via `inspect.signature`); CBO campaign op + ad-set write refused at apply; partial failure
  per-op; idempotency; read-only-token scope error at validate; last-active companion (and the negative
  case); pause follow-up marker; preview is write-free; missing-id error; no `upload` tool; propose
  never emits an `approved` op; execute/preview delegate with reader+gate.
- Two pre-existing tests updated for the new surface: `build_server_info` exact-dict (adds
  `write_tools_enabled`); the real-FastMCP registration test (now expects reads **+** write tools —
  this also proves FastMCP successfully derives JSON schemas for every write tool, incl. `dict`/`list`
  params).

## Known gaps / decisions to scrutinize (reviewer: treat as a floor)

1. **`propose_set_status` (single op) uses `build_single_op_plan` → structural abstain.** This means a
   single `propose_set_status(status="ACTIVE")` does **not** enforce the cold-ad grounding boundary
   that the data-driven bulk `propose_enable_ads` does (which cites a zero sample and blocks a cold
   enable). Rationale: a single explicit status change is a direct operator instruction on a named
   entity, treated like a safety PAUSE. **Confirm this is acceptable**, or route single ACTIVE enables
   through metric grounding. Documented in `build_write_tools`' docstring.
2. **Settings `_comment` is nested under `permissions`, not top-level.** The harness settings-schema
   validator rejects an unknown *top-level* key (despite the JSON Schema's `additionalProperties: {}`),
   so the ticket's literal "top-level `_comment`" was placed under `permissions` (which accepts extra
   keys). Same intent, legal location. Verify the auditor/readers look there.
3. **Bulk `propose_pause_ads` does NOT auto-cascade the last-active companion** — only the single
   `propose_set_status(PAUSED)` does. Cross-op reasoning (two ads in one set both paused in one plan)
   is out of scope here; the companion helper only counts *currently* ACTIVE siblings. Flag if bulk
   pause should also cascade.
4. **Default approval gate is forgeable by design.** `PlanStatusApprovalGate` is a no-op; an agent that
   can write the filesystem could hand-edit `status:"approved"` into a persisted proposal. This is the
   documented seam `mcp-local-approval-gate` (ticket 13) replaces with an un-forgeable source. The
   *core* refusal (0 approved ops on a fresh proposal) is real and tested.
5. **`preview_plan` "No Meta call" is "no *write* call".** Building a budget/targeting/creative request
   re-reads live state through the reader (read-only). Documented in the function docstring.
6. **Outcome-verify read in tests reflects the fake's unchanged status** (the fake client records
   updates but doesn't mutate stored entities), so `verify.effective_status` shows the pre-write value
   in tests. Against real Meta it reflects the applied change. The follow-up marker + re-read *plumbing*
   is what the tests assert, not a state transition.
7. **`_wrap_tool_errors` widened to catch `ValueError`/`ApprovalError`** for all tools (reads included).
   Reads raise only `MetaApiError` in practice, so behavior is unchanged, but confirm no read path
   should surface a raw `ValueError`.

## Not in scope (correctly deferred)

- Authoring (`create_*`) + audience-rotation propose/execute branches → **ticket 12.5**
  (`mcp-guarded-write-authoring-rotation`); they register their own `PLAN_APPLIERS` entries. The
  `apply_authoring_plan` signature has **no** `reader=` kwarg (rotation/ops do) — the dispatch wrapper
  must absorb that when 12.5 lands.
- Media upload tools (`upload_video`/`upload_image`) — deliberately never exposed over MCP (ungated,
  create unreferenced assets). Operator uploads via CLI; agent proposes `create_*` with the asset ids.
- Un-forgeable approval source → ticket 13 (`mcp-local-approval-gate`).
