description: Our own Meta MCP server can now make reversible account changes (pause/enable, budget, rename, creative, targeting), but only by routing every change through a propose → human-approve → validate → execute → verify safety flow — never as a raw one-shot API call.
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/control.py, .claude/settings.json, .mcp.json, docs/META_ACTION_WORKFLOW.md, docs/META_API_SETUP.md, AGENTS.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## What shipped

The guarded control-ops write surface now lives behind our custom MCP server. Every write is a
capability boundary enforced *in the server* (not a prompt rule): `propose_* → preview_plan →
execute_plan`, with `execute_plan` the only tool that writes. `execute_plan` loads a persisted proposal
**by id** (never a caller-supplied body — the anti-forgery seam), refuses a plan with zero approved ops
or an already-executed plan, runs a mandatory `validate_only` pass, aborts on any validation failure,
applies the approved ops, writes an audit artifact, and re-reads each entity to verify the outcome.

The implementation is faithful to the ticket. See the implement commit (`git show 62a6758`) for the
full surface: `proposals.py` (store + execute orchestration + approval seam), `control.py`
(`build_single_op_plan` / `append_last_active_ad_pause` / `plan_id` in the audit), `mcp_server.py`
(`build_write_tools`, `_resolve_account`, `_proposal_summary`, widened `_wrap_tool_errors`), and the
config/docs updates.

## Review findings

**Read the implement diff first, then verified every referenced symbol and ran the suite.** Reviewed
from correctness, DRY, modularity, resource cleanup, error handling, type safety, and doc-currency
angles.

### Fixed inline (minor)

- **Stale docs the ticket should have touched.** The implementer updated `docs/META_ACTION_WORKFLOW.md`,
  `.mcp.json`, and `.claude/settings.json`, but **left two authoritative docs factually wrong**:
  - `AGENTS.md` — its own line 223 designates it the single source of truth for "the full set of
    guarded writes," yet it still read *"Guarded writes are still CLI-only … no write travels through
    this server"* and *"it stays parked / not launched pending the guarded-write ticket,"* plus
    *"Writes are deliberately not part of this seam … the MCP read path is reads-only."* Rewrote the
    two paragraphs to describe the now-live `propose_*` / `preview_plan` / `execute_plan` surface, that
    writes still use the direct Graph client behind the gate (the *reader* seam stays reads-only), and
    the deliberate `upload_*`-not-exposed exception.
  - `docs/META_API_SETUP.md` — still said *"Guarded writes are still CLI-only and land in the
    `mcp-guarded-write-tools` follow-on ticket"* and *"the read tools have landed, but it stays parked
    … pending the guarded-write ticket."* Updated the capability description, the `server_info` example
    (now `write_tools_enabled: true`), and the parked-status rationale (parked pending rollout, not
    pending this ticket). Left the `META_READER_BACKEND=mcp` reader-backend paragraph (lines 131–134)
    untouched — it is correctly scoped to the read backend, which remains reads-only.

### Verified correct (checked, no change needed)

- **Every referenced symbol resolves.** `control` internals (`_build_request`, `_get_entity`,
  `_optional_str`, `default_ops_results_path`, `apply_ops_plan`, status constants, `TARGETING_OPS`,
  `AD_FIELDS`, `validate_op`), `account_registry.slugify_name` / `DEFAULT_ACCOUNTS_CONFIG_PATH` /
  `_normalize_ad_account_id` / `resolve_account`, `meta_api.client_from_env`, `utils.ensure_dir` /
  `write_json`, `config.DEFAULT_REPORTS_ROOT` all exist with matching signatures. The bulk builders
  `build_enable_ads_plan` / `build_pause_plan` / `build_budget_plan` accept exactly the kwargs the tool
  wrappers pass.
- **`preview_plan` is genuinely write-free.** `_build_request` only reads via the reader (creative
  re-attach, targeting read-modify-write, budget cap) and never calls `update_*`; the test asserts
  `client.updates == []`.
- **Audit artifacts don't clobber.** `default_ops_results_path` stamps a per-second UTC timestamp and
  embeds `plan_id`, so two same-day executes for one account produce distinct files. (Sub-second
  collision is possible in theory but not for a human-approved flow.)
- **Two-pass validate→execute, scope pre-flight, idempotency, and per-op partial-failure** all behave
  as documented and are tested.

### Accepted limitations (documented, not defects — no ticket filed)

- **Partial failure returns `executed: True` and stamps the plan executed.** A failed op cannot be
  retried by re-executing the same id (idempotency refuses); the operator must re-propose. This is the
  intended non-transactional-write safety posture — a crash between the execute pass and the executed
  stamp could still permit a re-apply, which is inherent to Meta's non-transactional writes and
  documented as such.
- **Default `PlanStatusApprovalGate` is forgeable** (a filesystem-write agent could hand-edit
  `status:"approved"`). Already scoped to ticket 13 (`mcp-local-approval-gate`) behind the same seam;
  the real 0-approved-ops refusal on a fresh proposal is tested.
- **Single `propose_set_status(status="ACTIVE")` abstains structurally** rather than enforcing the
  cold-ad grounding boundary that data-driven `propose_enable_ads` does. Accepted: a single explicit
  status change on a named entity is a direct operator instruction and still requires human approval.
- **Bulk `propose_pause_ads` does not cascade the last-active companion** — only single
  `propose_set_status(PAUSED)` does. Cross-op reasoning within one bulk plan is out of scope; the
  single-op path covers the common case.
- **`.claude/settings.json` `_comment` lives under `permissions`, not top-level** (the harness schema
  rejects unknown top-level keys). Documentation-only; no functional impact.
- **`find_proposal_path` interpolates the caller-supplied `plan_id` into a glob.** Low risk: generated
  ids are already sanitized to `[alnum/-/_]`, the lookup is confined to `reports_root`, and a
  non-matching or metachar id fails closed ("No proposal found"). Not worth hardening for a local
  single-operator flow.

### Test gaps (minor, noted — core paths well covered)

The new tests cover the core safety invariants thoroughly (no-approval refusal, validate-then-execute,
anti-forgery signature, CBO refusal, partial failure, idempotency, read-only-token scope error, last-
active companion + negative case, pause follow-up, write-free preview, missing id, no-upload-tool,
delegation). Not exercised end-to-end: the `propose_enable_ads` / `propose_pause_ads` tool wrappers
(their builders are tested elsewhere), `preview_plan`'s not-approved / build-error branches, an
ambiguous `plan_id`, `_verify_outcomes`' `verify_error` re-read-failure branch, and `_resolve_account`'s
unknown-account `ValueError`. None guard a safety-critical path; left for a future hardening pass rather
than blocking this ticket.

## Validation

- **Tests:** `pytest -q` → **419 passed** (run in `.venv`). Docs-only edits in the review pass do not
  affect tests.
- **Lint:** no Python linter is configured in this project (no `ruff` / `mypy` / `flake8` in
  `pyproject.toml`, `.github`, or the venv; the only `lint_*` entry is the domain-specific `lint_vault`
  console script, unrelated to code style). `python -m py_compile` on the three source files is clean.

## Follow-on work (unchanged from implement handoff)

- Authoring (`create_*`) + audience-rotation propose/execute branches → **ticket 12.5**
  (`mcp-guarded-write-authoring-rotation`); they register their own `PLAN_APPLIERS` entries. Note the
  `apply_authoring_plan` signature has **no** `reader=` kwarg (rotation/ops do) — the dispatch wrapper
  must absorb that.
- Un-forgeable approval source → **ticket 13** (`mcp-local-approval-gate`).
- Media upload tools deliberately never exposed over MCP (operator uploads via CLI, agent proposes
  `create_*` with asset ids).
