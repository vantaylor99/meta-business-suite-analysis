description: Review the new local approval gate that stops an AI agent from rubber-stamping its own proposed ad-account changes — a human must sign off out-of-band before the change can run.
prereq: mcp-guarded-write-tools
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/cli.py, pyproject.toml, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## What shipped

The local, single-operator **HMAC-signed approval gate** that closes the self-approval hole in the MCP
guarded-write flow. Over MCP the *agent* triggers `execute_plan`, so approval must be something the agent
structurally cannot forge. Before this ticket the `ApprovalGate` seam existed but its wired default
(`PlanStatusApprovalGate`) was a **no-op** — an agent with filesystem write access could hand-edit
`"status": "approved"` into the persisted proposal JSON and the gate would wave it through.

Now `execute_plan` verifies an **HMAC-SHA256 signature over the plan's approved content**, keyed by a
secret (`META_APPROVAL_SECRET`) the agent's MCP tool surface never holds, produced **out-of-band** by a
human-run `approve_plan` CLI. The agent can still edit the proposal JSON — but any edit to the approved
set changes the recompute and fails a constant-time compare, and it cannot forge a matching signature
without the secret. **No secret set ⇒ fail-closed** (`DeniedApprovalGate`: execute refused, reads
unaffected) — the opposite of the old forgeable-open default.

### Concrete changes

- **`proposals.py`**
  - `canonical_approval_payload(plan, approved_at)` + `compute_approval_signature(...)` — the **shared
    canonicalization** both sides use (the parity crux). Signs the FULL approved item dicts via
    `plan_items` (binds ops AND rotation items), plus `plan_id` (blocks cross-plan replay) and
    `approved_at` (blocks forward-dating past the TTL). The `approval`/`execution` blocks are never in
    the payload (not self-referential).
  - `HmacApprovalGate(secret, *, ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS, now_fn=None)` — verify +
    constant-time compare + TTL (injectable clock for deterministic tests). `ttl_seconds=0/None` disables.
  - `DeniedApprovalGate` — fail-closed, error names `META_APPROVAL_SECRET`.
  - `approval_secret_from_env()` (`META_APPROVAL_SECRET` else file at `META_APPROVAL_SECRET_FILE`, trailing
    newline stripped; short secret → clear `ValueError`), `approval_ttl_from_env()`,
    `select_approval_gate_from_env()` (the single selection seam `build_server` calls).
  - `approve_proposal(plan_id, *, secret, op_ids=None, reports_root, now_fn=None)` — the library helper the
    CLI stands on: flip selected items → `approved`, sign, write `plan['approval']`, persist; refuses an
    already-executed plan; matches items by `op_id` OR `adset_id`.
  - Constants `APPROVAL_SECRET_ENV` / `APPROVAL_SECRET_FILE_ENV` / `APPROVAL_TTL_ENV` /
    `MIN_APPROVAL_SECRET_LEN` (16) / `DEFAULT_APPROVAL_TTL_SECONDS` (86400) / `APPROVAL_KEY` /
    `APPROVAL_ALGORITHM`. Module docstring + `execute_plan` step-3 comment updated. `PlanStatusApprovalGate`
    **kept** (still the explicit no-op the existing execute tests pass in).
- **`mcp_server.py`** — `build_server` swaps `PlanStatusApprovalGate()` → `select_approval_gate_from_env()`
  (comment updated). `build_server_info` adds `approval_required: True` + `approval_configured` (via the
  non-raising `_approval_configured()` helper — a too-short/unreadable secret degrades to `False`, never
  raises, since server_info is a health probe). `_wrap_tool_errors` already maps `ApprovalError` →
  `ToolError`, so no server error-path change was needed.
- **`cli.py`** — `approve_plan_main` (thin over `approve_proposal`): args `--plan-id` (required),
  `--reports-root`, `--account`/`--run-date` (messaging only — proposals are found by id), `--op-id`
  (repeatable) / `--all`, `--yes`. Prints the per-item digest (reuses `mcp_server._proposal_summary`),
  prompts on the human's terminal unless `--yes`, then signs. Exits clearly if the secret is unset.
- **`pyproject.toml`** — registers `approve_plan = "meta_ads_analysis.cli:approve_plan_main"`.
- **`docs/META_ACTION_WORKFLOW.md`** — rewrote the "Approval seam" section: the local loop
  (propose → `approve_plan` → execute), how to generate/set the secret (in BOTH shells, out of the repo),
  the `server_info` health signal, and the residual local limitation + the Azure upgrade path.
- **Test helper** — `_clear_scaffold_env` now also clears the three approval env vars (hermetic tests);
  `test_build_server_info_defaults_when_env_unset` updated for the two new fields.

## How to validate (what the reviewer should re-run / probe)

- **Focused + full suite (mocks only, no live Meta):**
  `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -k "approval or approve or gate or mcp or execute"`
  (88 passed) then the full file (447 passed) and the whole suite (447 passed).
- **Key new tests** (all under the "MCP local approval gate" block at the end of the test file):
  `test_hmac_gate_accepts_signed_then_reloaded_plan` (round-trip parity),
  `test_approve_then_execute_end_to_end_ops` / `..._rotation`,
  `test_hmac_gate_rejects_agent_forged_approved_status` (the incident this reopens — status flipped, no
  signature → refused, `client.updates == []`), `..._mutated_approved_op`, `..._added_approved_op`,
  `..._cross_plan_signature_replay`, `..._expired_approval_and_accepts_within_ttl`,
  `..._empty_approved_set_with_leftover_signature_rejected`,
  `test_approve_proposal_flips_only_selected_and_refuses_executed`,
  `test_approval_secret_from_env_env_file_and_short`, `test_select_approval_gate_from_env_hmac_and_denied`,
  `test_select_gate_denies_when_no_secret_but_reads_still_work`, `test_server_info_reports_approval_fields`,
  `test_build_server_wires_selected_approval_gate`, `test_approve_plan_cli_signs_and_gate_verifies`,
  `test_approve_plan_cli_requires_secret`.

## Known gaps / honest flags (treat tests as a floor)

- **No lint / type pass was run.** `ruff` / `mypy` / `pyright` are not installed in this environment and
  the repo has no `[tool.ruff]` (or mypy) config. Validation was AST-parse (clean) + the full pytest
  suite. A reviewer with the tooling should run the project's real lint/type check; watch specifically the
  new imports in `proposals.py` (`hashlib`, `hmac`, `json`, `os`, `collections.abc.Callable`) and the
  removed inline `import json` in `load_proposal`.
- **"Strip confidence/evidence to dodge the grounding gate" is covered *indirectly*.**
  `test_hmac_gate_rejects_mutated_approved_op` proves any material field change breaks the signature (the
  full item dict is signed, so it generalizes), but there is **no** dedicated test that builds a *grounded*
  plan (e.g. via `build_pause_plan` / `build_enable_ads_plan` / a duplicate-ad plan), signs it, strips the
  real `confidence`/`evidence` block, and asserts the gate rejects. Worth adding to make that specific
  claim end-to-end explicit.
- **The interactive `input()` branch of `approve_plan_main` is untested.** Both CLI tests use `--yes`. A
  monkeypatched-`input` test for the "y" (approves) and "n" (aborts, nothing written) branches would close
  that path.
- **File-based secret is tested only at `approval_secret_from_env`,** not end-to-end through
  `select_approval_gate_from_env` / the CLI.
- **`build_server` gate wiring is tested against a fake `FastMCP` (`_SpyMcp`),** not real `mcp.add_tool`.
  The pre-existing real-FastMCP integration test (`test_read_tools_register_on_real_fastmcp_and_map_errors`)
  still constructs `PlanStatusApprovalGate()` directly and passes unchanged — it does not exercise the new
  gate. Fine per the ticket (it said add new tests rather than rewrite that one), but note the real-SDK
  path never touches `HmacApprovalGate`.
- **Residual local limitation is documented, not solved (by design).** On a single-user box, an actor that
  can read the server process's environment or the secret file could forge a signature. That is exactly
  what the backlog `mcp-role-based-access-tiers` ticket removes (Entra ID + server-side approval state),
  dropping in behind this same `ApprovalGate` seam without touching `execute_plan`. Confirm the code
  docstring + workflow doc both call this out (they do).
- **`datetime.fromisoformat` on the `"...Z"` timestamp** relies on Python ≥ 3.11 "Z" support; project pins
  `requires-python >=3.13` and tests ran on 3.14, so this is safe, but note it if the floor ever drops.
