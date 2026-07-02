description: Added a local HMAC-signed approval gate so an AI agent can propose and execute Meta ad-account changes but cannot approve its own work — a human signs off out-of-band before any change runs. Reviewed and hardened.
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/cli.py, pyproject.toml, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
----
## What shipped

The local, single-operator **HMAC-signed approval gate** that closes the self-approval hole in the MCP
guarded-write flow. Over MCP the *agent* triggers `execute_plan`, so approval must be something the agent
structurally cannot forge. `execute_plan` now verifies an **HMAC-SHA256 signature over the plan body**,
keyed by a secret (`META_APPROVAL_SECRET`) the agent's MCP tool surface never holds, produced
**out-of-band** by a human-run `approve_plan` CLI. **No secret set ⇒ fail-closed** (`DeniedApprovalGate`:
execute refused, reads unaffected).

Core pieces (unchanged from the implement handoff except where the review touched them):
`canonical_approval_payload` / `compute_approval_signature` (shared canonicalization — the parity crux),
`HmacApprovalGate` (verify + constant-time compare + injectable-clock TTL), `DeniedApprovalGate`
(fail-closed), the `*_from_env` selection seam (`select_approval_gate_from_env`) wired by `build_server`,
`approve_proposal` (the library helper the `approve_plan` CLI stands on), `server_info` health fields
(`approval_required` + non-raising `approval_configured`), and the rewritten "Approval seam" doc section.

## Review findings

Reviewed the full implement diff (`80db549`) with fresh eyes before the handoff summary, then probed the
crypto design and every file the change touched (and the appliers it *should* have touched).

### MAJOR — found and fixed inline (security gap in the security-critical feature)

**The signature covered only the approved items + `plan_id`/`plan_type`/`approved_at`, leaving
execution-affecting PLAN-LEVEL fields unsigned.** All four appliers (`control.apply_ops_plan`,
`rotation.apply_rotation_plan`, `rotation.apply_advantage_disable_plan`, `authoring.apply_authoring_plan`)
read `plan["guardrails"]["requires_grounding"]` to decide whether the apply-time **grounding gate** runs,
and `apply_authoring_plan` POSTs creates to plan-level `plan["ad_account_id"]`. Neither was in the signed
payload. So an agent with filesystem write could, *without touching any approved item* (which the
implementer's tests correctly cover), flip `requires_grounding` to `false` — disabling the documented
hard gate that blocks abstain/thin ops even when approved — or redirect `ad_account_id`, and the old
signature would **still verify**. This was an easier bypass than the "strip confidence/evidence" path the
implementer's docstring claimed to close.

*Fix (this pass):* `canonical_approval_payload` now signs the **entire plan body** minus the two
self-referential blocks (`approval`, `execution`) plus `approved_at`, instead of just the approved items.
This closes the whole class of "unsigned plan-level field" bypass (guardrails, ad_account_id, and any
future applier-read field) in one stroke, is strictly safer (nothing legitimately edits the plan between
approval and execute), and preserves round-trip parity (all values are JSON-origin; both sides recompute
identically). Updated the docstrings (module, `canonical_approval_payload`, `HmacApprovalGate`,
`approve_proposal`), the `mcp_server.build_server` comment, and `docs/META_ACTION_WORKFLOW.md` to describe
whole-body signing and name the guardrail/ad_account_id cases. Added regression tests
`test_hmac_gate_rejects_flipped_plan_level_guardrail` and `test_hmac_gate_rejects_redirected_ad_account_id`
(both would have passed — i.e. failed to reject — against the pre-fix code). Verified all pre-existing
approval round-trip / e2e tests still pass, confirming parity was preserved.

### MINOR — fixed inline

- **Interactive `input()` branch of `approve_plan_main` was untested** (implementer flagged it). Added
  `test_approve_plan_cli_interactive_confirm_and_abort` covering both the `n` branch (aborts, nothing
  written — asserts no `approval` block persisted) and the `y` branch (signs; the persisted plan verifies
  under the gate).

### MINOR — reviewed, left as-is (acceptable, with reason)

- **No lint/type pass** — `ruff`/`mypy`/`pyright` are not installed in this environment and the repo has
  no `[tool.ruff]`/mypy config (confirmed). Validation was `py_compile` (clean on all three changed
  modules) + the full pytest suite. A maintainer with the tooling should still run the project's real
  lint/type check; the new imports in `proposals.py` (`hashlib`, `hmac`, `json`, `os`,
  `collections.abc.Callable`) and the removed inline `import json` in `load_proposal` are the things to
  watch. (Carried forward from the implement handoff — unchanged by this pass except the whole-body edit
  added no new imports.)
- **File-based secret is tested only at `approval_secret_from_env`**, not end-to-end through
  `select_approval_gate_from_env`/the CLI. The unit-level coverage plus the env-var e2e path make the
  file path low-risk (same resolved-bytes → same gate); not worth a dedicated e2e test.
- **A dedicated "strip confidence/evidence on a *grounded* plan" e2e test** is still absent; it is covered
  indirectly by `test_hmac_gate_rejects_mutated_approved_op` (any material item-field change breaks the
  signature) and now more strongly by the whole-body signing. Left as-is.
- **`build_server` gate wiring is tested against a fake `FastMCP` (`_SpyMcp`)**, not real `mcp.add_tool`;
  the real-FastMCP integration test still constructs `PlanStatusApprovalGate()` directly. Fine per the
  ticket scope — the `HmacApprovalGate`/`DeniedApprovalGate` selection and behavior are covered by direct
  unit + e2e tests.
- **`datetime.fromisoformat` on the `"…Z"` timestamp** relies on Python ≥ 3.11 "Z" support; project pins
  `requires-python >=3.13` and tests ran on 3.14 — safe.

### Checked and clean (no action)

- **Crypto correctness / parity:** single shared canonicalization; signature computed post-status-flip on
  a JSON-origin plan, verified on the JSON-reloaded plan → byte-identical (e2e ops + rotation tests pass).
- **Gate ordering in `execute_plan`:** gate runs at step 3, before the write client is built and before
  any validate/execute round-trip; no path reaches a write without passing it. Idempotency (step 2) and
  the zero-approved refusal (step 4, second layer) intact.
- **Cross-plan replay / TTL / forged-status / added-op / removed-op / mutated-op:** covered by existing
  tests and re-verified.
- **`plan_id` binding:** `save_proposal` embeds `plan_id` into the stored body, so it is present at both
  sign and verify time — replay protection is real (not a no-op over `None`).
- **`_approval_configured` health probe:** catches the only exception `approval_secret_from_env` raises
  (`ValueError`, incl. the OSError-wrapped unreadable-file case) → degrades to `False`, never breaks
  `server_info`.
- **Docs:** `docs/META_ACTION_WORKFLOW.md` "Approval seam" section read in full and updated to match the
  hardened reality. `AGENTS.md` describes the flow as "propose → human-approve → validate → execute →
  verify" (generic, and now *more* accurate than before the gate was real) and points to the workflow doc
  for detail — not stale; it never described the old no-op default. (Nice-to-have, not done: AGENTS.md
  does not yet mention the `approve_plan` CLI / `META_APPROVAL_SECRET` env by name.)
- **Fail-closed + reads-unaffected:** `test_select_gate_denies_when_no_secret_but_reads_still_work` proves
  execute is refused with no secret while a read tool still returns live data.

### Residual limitation (documented, by design — not a review finding)

On a single-user box, an actor that can read the server process's environment or the secret file could
forge a signature. That is exactly what the backlog `mcp-role-based-access-tiers` ticket removes (Entra ID
+ server-side approval state), dropping in behind this same `ApprovalGate` seam without touching
`execute_plan`. Confirmed the code docstring + workflow doc both call this out.

## Validation

- `.venv/bin/python -m py_compile` on `proposals.py`/`mcp_server.py`/`cli.py` — clean.
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` — **450 passed** (447 pre-existing + 3 new
  review tests). Full repo suite (`pytest -q`) — **450 passed**.
- Lint/type: not runnable here (tooling absent, no config) — see MINOR above.

## End
