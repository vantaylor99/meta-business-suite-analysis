description: When the agent asks to execute a proposed change, make sure it can only go through after a real person has signed off out-of-band — the agent must have no way to approve its own work. This is the local, single-operator version; full role-based approval comes later.
prereq: mcp-guarded-write-tools
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/cli.py, pyproject.toml, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## Why

A guarded write is only safe if the **approval** between propose and execute comes from a human, not
from the agent. Over MCP the *agent* triggers `execute_plan`, so approval must be something the agent
structurally cannot forge. Today the seam exists but its default implementation is a forgeable no-op:

- `proposals.ApprovalGate` (Protocol, `proposals.py:59`) + `PlanStatusApprovalGate` (`proposals.py:69`,
  a **no-op**) that leans entirely on the apply invariant "only `status=="approved"` ops are sent."
- `execute_plan` consults it at step 3 (`proposals.py:606`), then refuses a plan with zero approved
  ops at step 4 (`proposals.py:610`).
- `mcp_server.build_server` wires the no-op gate (`mcp_server.py:678`).

Because **no `propose_*` tool ever flips an op to `approved`**, a freshly-proposed plan has zero
approved ops and execute refuses — the current safety. But an agent with filesystem write access to the
`reports/**/proposals/*.json` tree can hand-edit `"status": "approved"` into the persisted plan and the
no-op gate waves it through. That is the forgery this ticket closes.

This delivers the **local, single-operator** version only. The multi-user, supervisor-approves-
specialist, role-based version (Entra ID + server-side approval state in Azure) is the backlog
`mcp-role-based-access-tiers` ticket — it drops in behind this same seam without rewriting `execute_plan`.

## Chosen design (resolved — HMAC-signed approval, out-of-band CLI)

Of the three options in the plan (out-of-band CLI stamp / one-time token / HMAC-over-plan), we adopt
**option 3: an HMAC-SHA256 signature over the plan's approved content, keyed by a secret the agent's
MCP tool surface never holds**, produced by an **out-of-band human-run CLI** (`approve_plan`) and
verified by a new `HmacApprovalGate` inside `execute_plan`.

Why this over the alternatives:

- **A plain out-of-band CLI stamp (flip `status → approved` in the file) is insufficient** — the plan's
  own edge case says so. The agent can perform the identical file edit. The stamp must be *unforgeable*,
  which means it must carry a signature the agent cannot reproduce. So option-1-alone collapses into
  option 3 the moment you make it real.
- **A one-time token handed back by the human** is really "HMAC without binding to plan contents." To
  stop replay against a *mutated* plan you must bind the token to the plan's contents anyway (the plan's
  own edge case), i.e. sign the plan — which is option 3. HMAC is option-2-done-right.
- **HMAC (symmetric) vs asymmetric (Ed25519):** asymmetric is strictly stronger (the verifier holds only
  a public key), and the plan's principle ("a secret the agent process never holds") points at it. We
  deliberately choose **HMAC for the local playground** because: (a) it is stdlib-only (`hmac` +
  `hashlib`), no new dependency or keypair ceremony; (b) on the single-operator macOS box the realistic
  threat is the agent editing the proposal JSON through its bash/edit tools, **not** reading another
  process's environment (macOS does not expose `/proc`, and cross/same-user env of a live process is not
  readable via the agent's tool surface); (c) the whole point of the `ApprovalGate` seam is that the
  Azure/role-based ticket swaps in server-side state (and, if wanted, asymmetric signing) **without
  touching `execute_plan`**. We record HMAC's residual local limitation plainly (below) so the upgrade
  path is explicit.

### Trust boundary (what "the agent can't forge it" means here)

```
  operator's shell  ───────────────────────────────┐   holds META_APPROVAL_SECRET
    $ approve_plan --plan-id <id> --all             │   (sign)
      → loads proposal, shows summary,              │
        flips selected items → approved,            │
        writes plan["approval"]={signature,...}     │
                                                    ▼
  reports/<acct>/<date>/proposals/<id>.json  ◄── agent CAN edit this file
                                                    │
  MCP server process  ──────────────────────────────┘   holds META_APPROVAL_SECRET
    execute_plan → HmacApprovalGate.assert_approved      (verify, constant-time)
      recompute HMAC over the plan's CURRENT approved    → mismatch ⇒ ApprovalError
      items; compare to plan["approval"]["signature"]
```

The secret lives in `META_APPROVAL_SECRET` (or `META_APPROVAL_SECRET_FILE`), present in **both** the
operator's approve-CLI shell and the MCP server process environment, and **never**: returned by any MCP
tool, written into any plan/proposal/audit artifact, or reachable through the agent's tool surface. The
agent can freely edit the proposal JSON — but any edit to the approved set changes the recomputed HMAC,
and the agent cannot produce a matching signature without the secret. Reads are entirely unaffected.

**Residual local limitation (document, do not try to solve here):** on a single-user machine an actor
that can read the server process's environment or the secret file could forge. That is exactly what the
Azure/role-based ticket removes by moving approval state server-side behind Entra ID. Call this out in
the code docstring and the workflow doc; it is an accepted tradeoff for the local playground, not a bug.

### The parity contract (the correctness crux)

Signing (in the CLI) and verifying (in the gate) MUST canonicalize the approved content **identically**,
or every real approval fails verification. Put the canonicalization in **one shared function** used by
both sides — mirror the existing "CBO parity" (`classify_adset_budget`) and "review re-derivation"
patterns in this repo.

```python
APPROVED_STATUS = "approved"  # reuse control.APPROVED_STATUS

def canonical_approval_payload(plan: dict, approved_at: str) -> str:
    """Deterministic JSON over the plan's APPROVED content. Round-trip-stable: signing the in-memory
    plan and verifying the JSON-reloaded plan must produce byte-identical payloads."""
    approved = [it for it in plan_items(plan) if it.get("status") == APPROVED_STATUS]
    doc = {
        "plan_id": plan.get("plan_id"),
        "plan_type": plan.get("plan_type"),
        "approved_at": approved_at,
        "approved_items": approved,   # FULL item dicts (params, level, id, confidence, evidence, ...)
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def compute_approval_signature(plan: dict, approved_at: str, secret: bytes) -> str:
    return hmac.new(secret, canonical_approval_payload(plan, approved_at).encode("utf-8"),
                    hashlib.sha256).hexdigest()
```

Notes on what the payload covers and why:
- It uses `plan_items(plan)` (the existing plan-type-aware accessor, `proposals.py:219`), so it binds
  ops **and** rotation items — a rotation plan's approved items live under `"rotations"`/`"items"`, not
  `"ops"`. Do NOT hard-code `plan["ops"]`.
- It signs the **full approved item dicts** (not a hand-picked subset), so mutating *any* material field
  of an approved op (params, target `id`, `level`) — or stripping its `confidence`/`evidence` to dodge
  the separate write-grounding gate — changes the signature and is rejected.
- `plan_id` binds the signature to this specific plan (blocks copying an `approval` block from plan A
  onto plan B — the payload carries B's id, the signature was over A's).
- `approved_at` is inside the signed payload, so the agent cannot forward-date it to defeat the TTL.
- The `plan["approval"]` block and the `plan["execution"]` block are NEVER part of the payload
  (`plan_items` reads neither), so the signature is not self-referential.

### The gate

```python
class HmacApprovalGate:
    """Un-forgeable local approval: verify plan['approval'].signature against a fresh recompute over
    the plan's current approved items, keyed by a secret the agent never holds. Constant-time compare."""
    def __init__(self, secret: bytes, *, ttl_seconds: int | None = DEFAULT_APPROVAL_TTL_SECONDS,
                 now_fn=lambda: datetime.now(UTC)) -> None: ...
    def assert_approved(self, plan_id: str, plan: dict) -> None:
        approval = plan.get("approval")
        if not isinstance(approval, dict) or not approval.get("signature"):
            raise ApprovalError("no human approval on this plan — approve it out-of-band with "
                                 "`approve_plan --plan-id <id>` before executing.")
        approved_at = str(approval.get("approved_at") or "")
        expected = compute_approval_signature(plan, approved_at, self._secret)
        if not hmac.compare_digest(expected, str(approval.get("signature"))):
            raise ApprovalError("approval signature does not match the plan's approved ops — the plan "
                                "was modified after approval, or the approval is forged. Re-approve.")
        if self._ttl_seconds:  # 0/None disables expiry
            # parse approved_at; if now - approved_at > ttl -> ApprovalError("approval expired ...")
            ...
```

- A freshly-proposed plan (no `approval` block) → "no human approval" `ApprovalError`.
- An agent that flips a status to approved but can't sign → recompute over the new approved set ≠ stored
  signature → mismatch. (If it also strips the stale `approval` block, it's back to the no-approval case.)
- Empty approved set with a leftover signature → recompute over `[]` ≠ stored → mismatch. The existing
  step-4 "no approved ops" refusal stays as a second layer; the gate runs first.
- TTL default `DEFAULT_APPROVAL_TTL_SECONDS = 86400` (24 h), overridable via `META_APPROVAL_TTL_SECONDS`
  (empty/`0` disables). `now_fn` is injectable so tests are deterministic.
- The mandatory validate pass in `execute_plan` (step 6, `proposals.py:631`) already re-reads live state
  and re-detects drift (e.g. a CBO flip) — the plan's "account state moved on" edge case is covered by
  that plus the time-based TTL; do NOT add a second live-read here.

### Secret resolution + gate selection (single seam, mirror `reader_from_env`)

```python
APPROVAL_SECRET_ENV = "META_APPROVAL_SECRET"
APPROVAL_SECRET_FILE_ENV = "META_APPROVAL_SECRET_FILE"
APPROVAL_TTL_ENV = "META_APPROVAL_TTL_SECONDS"
MIN_APPROVAL_SECRET_LEN = 16

def approval_secret_from_env() -> bytes | None:
    """Resolve the raw secret from META_APPROVAL_SECRET, else the file at META_APPROVAL_SECRET_FILE
    (bytes, trailing newline stripped). None if neither is set. Raise a clear ValueError if a configured
    secret is shorter than MIN_APPROVAL_SECRET_LEN (a too-guessable secret is a misconfig, not a default)."""

def select_approval_gate_from_env() -> ApprovalGate:
    """The single selection point for build_server. Secret present -> HmacApprovalGate(secret, ttl=...).
    Secret absent -> DeniedApprovalGate (fail CLOSED): execute refuses with setup guidance; reads work."""
```

`DeniedApprovalGate` is a tiny gate whose `assert_approved` always raises `ApprovalError` naming
`META_APPROVAL_SECRET`. **Fail closed**: no secret ⇒ no write ever executes (the opposite of the current
forgeable-open default). `_wrap_tool_errors` (`mcp_server.py:609`) already maps `ApprovalError` → a clean
`ToolError`, so no server-layer change is needed for the error path.

Keep `PlanStatusApprovalGate` in place — it is still the explicit no-op used by the existing execute
tests (they pass their own gate and approve via `_approve_all`, so they are unaffected). Only
`build_server`'s one construction line changes from `PlanStatusApprovalGate()` to
`select_approval_gate_from_env()`.

### The out-of-band approve CLI

Library helper (testable without argparse), in `proposals.py`:

```python
def approve_proposal(plan_id: str, *, secret: bytes, op_ids: list[str] | None = None,
                     reports_root=DEFAULT_REPORTS_ROOT, now_fn=lambda: datetime.now(UTC)) -> dict:
    """Load the persisted proposal, flip the selected items (all proposed items if op_ids is None) to
    approved, sign the approved content, write plan['approval'], persist, and return the approval block.
    Refuse (MetaApiError) if the plan was already executed. op_ids match op_id OR adset_id."""
```

Thin CLI `approve_plan_main` in `cli.py` (mirror `apply_ops_main`): args `--plan-id` (required),
`--reports-root`, optional `--account`/`--run-date` (messaging only — `find_proposal_path` globs by id),
`--op-id` (repeatable) / `--all`, `--yes` (skip the interactive confirm). It prints the plan summary
(reuse the digest shape from `mcp_server._proposal_summary`; extract a shared renderer if convenient),
then — unless `--yes` — prompts the human on their own terminal ("Approve these N ops? [y/N]"); the
prompt is the human-confirmation channel the agent has no access to. `--yes` is for scripting; the
security comes from the secret, not the prompt. On approval it calls `approve_proposal` and prints the
`plan_id` + a reminder that the agent may now `execute_plan`. Register a `approve_plan` console script in
`pyproject.toml [project.scripts]` (underscore style, next to `apply_ops`).

### server_info health signal (small, token-free)

Add to `build_server_info` (`mcp_server.py:99`): `"approval_required": True` and
`"approval_configured": approval_secret_from_env() is not None` — a token-free health signal so an
operator can see at a glance whether the gate has a secret. Keep it non-raising (catch the short-secret
`ValueError` → report `False`, since server_info is a health probe not a constructor).

## Edge cases & interactions

- **Round-trip stability (parity):** signing the in-memory plan (in `approve_proposal`, which itself
  loaded from JSON) and verifying the JSON-reloaded plan (in the gate) must yield byte-identical
  payloads. `canonical_approval_payload` uses `sort_keys` + compact separators + `ensure_ascii`; all
  values are JSON-safe (they originate from JSON). Test: sign → persist → reload → verify passes.
- **Mutation after approval:** change an approved op's `params`/`id`/`level`, or strip its `confidence`
  → signature mismatch → `ApprovalError`. Test each.
- **Self-approval attempt (the incident this reopens):** with only `META_ACCESS_TOKEN` set (no approval
  secret), the agent flips `status:approved` in the file and calls execute → gate mismatch/absent →
  refused. Test: agent-forged approved plan + `HmacApprovalGate` with a secret → refused.
- **Add/remove an approved op:** agent adds a new approved op, or removes one from the human-approved
  set → recompute over the new set ≠ stored signature → refused.
- **Cross-plan replay:** copy a valid `approval` block from plan A into plan B → `plan_id` in B's payload
  ≠ A's → mismatch. Test.
- **Expiry:** approved_at older than TTL → refused; within TTL → executes. `approved_at` is inside the
  signed payload so it can't be forward-dated. Test with an injected `now_fn`.
- **Fail-closed on missing secret:** no `META_APPROVAL_SECRET` → `DeniedApprovalGate` → execute refuses
  with guidance; **reads still succeed**. Test both (execute refused; a read tool still returns).
- **Short/empty secret:** `approval_secret_from_env` raises a clear `ValueError`; `server_info` degrades
  to `approval_configured: False` rather than raising.
- **Already-executed plan:** `approve_proposal` refuses to (re-)approve an executed plan; the existing
  idempotency guard (`proposals.py:597`) still refuses a second execute regardless.
- **Rotation/authoring plans:** the payload uses `plan_items`, so approving/verifying a rotation
  (`"rotations"`/`"items"`) or authoring plan works with no special-casing. Test at least one non-ops
  plan-type through approve → execute.
- **Existing execute tests:** they pass `PlanStatusApprovalGate()` explicitly and approve via
  `_approve_all` — they must keep passing unchanged (do not migrate them; the no-op class stays).
- **`build_write_tools`/`build_server` tests:** the parity/registration tests that construct
  `PlanStatusApprovalGate()` directly (test file ~9354, ~9655) stay valid. Add NEW tests for the gate
  and the CLI rather than rewriting them.
- **`preview_plan` unaffected:** it is write-free and must remain usable pre-approval so the operator can
  review before signing. Do not gate it.

## Key tests (write these up front)

- `test_hmac_gate_accepts_signed_then_reloaded_plan` — sign via `approve_proposal`, persist, reload,
  `HmacApprovalGate(secret).assert_approved` returns (round-trip parity).
- `test_hmac_gate_rejects_agent_forged_approved_status` — plan with `status:approved` but no/invalid
  signature → `ApprovalError`; drive it through `execute_plan` → `refused`/raises, `client.updates == []`.
- `test_hmac_gate_rejects_mutated_approved_op` — approve, then mutate an approved op's params →
  `ApprovalError`.
- `test_hmac_gate_rejects_cross_plan_signature_replay` — approval block from plan A on plan B → rejected.
- `test_hmac_gate_rejects_expired_approval` / `..._accepts_within_ttl` — injected `now_fn`.
- `test_select_gate_denies_when_no_secret_but_reads_still_work` — no env secret → execute refused,
  a read tool from `build_read_tools` still returns.
- `test_approve_proposal_flips_only_selected_and_signs` — `op_ids` filter; only selected flip; signature
  present; `approve_proposal` refuses an already-executed plan.
- `test_approve_then_execute_end_to_end_ops` and one `..._rotation` (or authoring) — full propose (via a
  builder) → `approve_proposal` → `execute_plan(HmacApprovalGate)` → executed + audit + verify.
- `test_approval_secret_from_env_rejects_short_secret` and file-based resolution.

## TODO

### Phase 1 — library (proposals.py) + tests
- Add `canonical_approval_payload`, `compute_approval_signature`, `HmacApprovalGate`, `DeniedApprovalGate`,
  `approval_secret_from_env`, `select_approval_gate_from_env`, `approve_proposal`, and the
  `APPROVAL_SECRET_ENV`/`APPROVAL_SECRET_FILE_ENV`/`APPROVAL_TTL_ENV`/`MIN_APPROVAL_SECRET_LEN`/
  `DEFAULT_APPROVAL_TTL_SECONDS` constants. Reuse `control.APPROVED_STATUS`, `plan_items`,
  `find_proposal_path`, `load_proposal`, `write_json`. Update the module docstring (the block at
  `proposals.py:9-16` currently says the local-approval ticket "replaces" the no-op — make it describe
  the shipped `HmacApprovalGate`).
- Write the gate/approve tests above.

### Phase 2 — wiring (mcp_server.py) + health
- `build_server`: swap `PlanStatusApprovalGate()` (`mcp_server.py:678`) → `select_approval_gate_from_env()`;
  update the surrounding comment.
- `build_server_info`: add `approval_required` + `approval_configured` (non-raising).
- Test: `server_info` reports the new fields; `build_server` still registers `execute_plan`.

### Phase 3 — out-of-band CLI + docs
- `cli.py`: add `approve_plan_main` (thin over `approve_proposal`, with the interactive confirm).
- `pyproject.toml`: register `approve_plan = "meta_ads_analysis.cli:approve_plan_main"`.
- `docs/META_ACTION_WORKFLOW.md`: document the local loop — **propose (agent) → review + `approve_plan`
  (you) → `execute_plan` (agent)** — including how to generate/set `META_APPROVAL_SECRET`
  (`python -c "import secrets; print(secrets.token_hex(32))"`), that it must be set in BOTH the server
  and approve-CLI shells and kept out of the repo, and the residual local limitation + the Azure upgrade
  path. Add a CLI test for `approve_plan_main` (approve → the persisted plan verifies under the gate).

### Validation
- Run the focused suite streamed: `python -m pytest tests/test_meta_ads_analysis.py -k "approval or approve or gate or mcp or execute" 2>&1 | tee /tmp/approval.log`, then the full file once. Run ruff/type checks per AGENTS.md. MOCKS ONLY — no live Meta call.
