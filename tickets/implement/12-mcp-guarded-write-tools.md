description: Let our own MCP server make reversible account changes (pause/enable, budget, rename, creative, targeting) — but only by running them through the same propose-review-approve-execute safety flow we already built, never as raw one-shot API calls. This ticket builds the shared propose/execute machinery plus the control-ops surface; authoring and audience-rotation tools follow in a chained ticket.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/write_grounding.py, .claude/settings.json, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## Why

Owning the server lets us move the **guarded write flow** behind it, so the guardrails are a capability
boundary enforced *in the server* rather than prompt rules an agent can be talked out of. This ticket
exposes the **reversible control-ops surface** as `propose_* → execute_plan` MCP tools that wrap the
existing guarded pipeline — never the raw Graph client. It also builds the shared propose/execute
machinery (proposal store, execute dispatch, audit, outcome verify, token-scope error) that the
authoring + rotation tools reuse in `mcp-guarded-write-authoring-rotation`.

This does NOT reopen the write-boundary rule. The rule bars **ungated** writes over MCP (the official
`mcp__meta-ads__*` connector, whose write tools fire immediately with no gate — the Jun-2026 $300/day
`IN_PROCESS` incident). A **custom** server whose write tools route through propose→approve→validate→
execute + confidence + review + audit + PAUSED-by-default is exactly what the rule always allowed. Our
tools carry the `mcp__meta-suite__*` prefix (client key `meta-suite` in `.mcp.json`), deliberately
distinct from the deny-listed `mcp__meta-ads__*`, so they are never caught by that deny-list — correct,
because ours are gated.

## Design (resolved)

### Tool surface (this ticket)

Control-ops propose tools, each a thin wrapper over an existing `control.py` builder (which already
attaches Evidence + a computed confidence band and runs the plan through `review.review_ops_plan`
before returning):

- `propose_set_status(account, id, level, status, run_date=None)` — pause/enable (ACTIVE|PAUSED).
- `propose_set_daily_budget(account, daily_budget_cents, adset_id=None, campaign_id=None, run_date=None, max_increase_percent=None, max_decrease_percent=None)` — wraps `control.build_budget_plan`; **exactly one** of `adset_id`/`campaign_id` (mirror the builder's `ValueError`). CBO handled by the builder: an ad set under CBO yields a non-executable ad-set pointer + an actionable **campaign-level** op — no ad-set budget write under CBO.
- `propose_rename`, `propose_set_creative`, `propose_set_creative_features`, and the targeting ops `propose_set_age_range` / `propose_set_genders` / `propose_set_geo_locations` / `propose_set_placements` — one op each, built via a small single-op ops-plan builder (see below).
- Bulk convenience wrappers already in `control.py`: `propose_enable_ads` (→ `build_enable_ads_plan`) and `propose_pause_ads` (→ the `pause_ads` builder).

Execution tools (shared machinery, live in `proposals.py`):

- `preview_plan(plan_id)` — **local** dry run: loads the persisted proposal, returns the request each
  approved op *would* send. No Meta call.
- `execute_plan(plan_id)` — the only tool that writes. Loads the persisted proposal **by id** (see
  anti-forgery below), runs a **validate_only pass first** (real round-trip, `execution_options=
  ['validate_only']`, nothing persisted); only if every approved op validates does it run the
  **execute pass**; then writes the audit artifact and runs the outcome-verification read.

No single-op `validate_plan` tool — validation is an internal, fail-closed step of `execute_plan`, so
the agent cannot execute without a fresh validation.

### Single-op ops-plan builder (control.py)

`build_enable_ads_plan` / `build_budget_plan` / the `pause_ads` builder cover bulk + budget. `rename`,
`set_creative`, `set_creative_features`, and the four targeting ops have **no** single-op builder today
(the CLI hand-authors ops into a plan file). Add one:

```
def build_single_op_plan(
    reader, ad_account_id, *, op, level, id, params,
    account_slug=None, date_from=None, date_to=None, run_date=None, policy=None,
) -> dict:   # {schema_version, plan_type:"ops", intent:op, account_slug, ad_account_id,
             #  guardrails:{requires_explicit_approval:True, requires_grounding:True}, ops:[<op>]}
```

It builds one op dict (`op_id`, `op`, `level`, `id`, `params`, `status:"proposed"`), attaches grounding
via `write_grounding.attach_op_grounding` (structural abstain — these are direction/no-metric ops, so
they abstain honestly and pass the apply-time grounding gate, mirroring a safety PAUSE), and returns it
run through `review.review_ops_plan`. Reuse `validate_op` (already rejects unsupported ops, wrong
levels, Meta-AI/Advantage+ params) — do not duplicate its guardrails.

### Proposal store + plan_id (proposals.py — new module, pure library, no socket)

`propose_*` persists the built+reviewed plan and hands the agent a **reference**, never an approvable
body:

- `save_proposal(plan, *, account_slug, run_date) -> str` — writes JSON to
  `reports/<account_slug>/<run_date>/proposals/<plan_id>.json`; returns `plan_id`. `plan_id` is unique
  (`f"{plan_type}-{intent}-{account_slug}-<UTC-timestamp>"`; add a counter/suffix on collision).
- `load_proposal(plan_id) -> dict` — resolves by id under the proposals tree; raises a clear error if
  missing/unreadable.
- The propose tool returns `{plan_id, plan_type, intent, account_slug, ops:[{op_id, op, level, id,
  status, confidence.band, review_verdict, note}...]}` — a review-ready summary the agent relays to the
  human. Ops come back at `status:"proposed"` (or demoted by review).

### Approval seam (default here; un-forgeable source is `mcp-local-approval-gate`)

`execute_plan` consults a small **approval seam** before applying, so ticket 13 can drop in the
un-forgeable local approver without rewriting execute. Mirror the reader-provider seam:

```
class ApprovalGate(Protocol):
    def assert_approved(self, plan_id: str, plan: dict) -> None: ...   # raise ApprovalError if not
```

Default impl **this ticket** ships: `PlanStatusApprovalGate` — a no-op that relies on the existing
`apply_*_plan` invariant (only `status=="approved"` ops are sent). Since **no MCP tool in this ticket
flips an op to `approved`**, a freshly-proposed plan has zero approved ops → `execute_plan` applies
nothing → returns `{refused: True, reason: "no approved ops — approval required (see approval gate)"}`.
That IS the core refusal. Document plainly that this default is **forgeable by a local filesystem-write
agent** (it could hand-edit `status`) and that `mcp-local-approval-gate` replaces it, behind this same
seam, with an un-forgeable source (out-of-band CLI stamp / confirmation token / HMAC over the plan).

### Execute orchestration (proposals.py)

```
def execute_plan(plan_id, *, approval_gate, reader, client=None) -> dict
```

1. `plan = load_proposal(plan_id)` — **never** accept a plan dict from the caller. This is the
   anti-forgery measure this ticket owns: if execute took a body, the agent would pass `status:
   approved`. It takes an id and loads the persisted artifact.
2. Idempotency guard: if the proposal is already marked executed, refuse (avoid double-apply — Meta
   writes are not transactional). Mark executed only after a successful execute pass.
3. `approval_gate.assert_approved(plan_id, plan)`.
4. Build a write client lazily (`meta_api.client_from_env()`) — do **not** reuse the reader's hidden
   client (reader has no public `.client`; writes keep their own explicit client). Pass the existing
   `reader` for grounding re-reads.
5. **validate pass**: dispatch on `plan["plan_type"]` to the apply fn with `validate_only=True`. If any
   approved op returns `validation_failed`/`blocked`/`failed`, abort before the execute pass and return
   the per-op validation results (no writes).
6. **execute pass**: same dispatch with `execute=True`. Collect per-op results.
7. Write the audit artifact (`control.write_ops_results` for ops; the store records `plan_id`,
   `executed`, `generated_at`, per-op status/request/response/reason).
8. **Outcome verification** (carry the pausing lesson): re-read each executed entity's
   `effective_status` via the `reader` and include it per-op; for any `set_status→PAUSED`, emit a
   structured `verify_next_day_spend` follow-up marker in the result (same-day spend cannot be
   confirmed $0 — the write registering is necessary but not sufficient proof delivery stopped).

Dispatch map (extensible; authoring+rotation register their branches in the chained ticket):

```
PLAN_APPLIERS = {
  "ops": lambda plan, client, **kw: control.apply_ops_plan(plan, client, **kw),   # takes reader kw
  # "authoring": ... (next ticket), "audience_rotation"/"advantage_disable": ... (next ticket)
}
```

Note the signature split: `apply_ops_plan` / `apply_rotation_plan` / `apply_advantage_disable_plan`
accept a `reader=` kwarg; `apply_authoring_plan` does **not**. The dispatch wrapper absorbs that.

### Last-active-ad ⇒ pause the ad set too

`propose_set_status(..., status="PAUSED")` on an ad: via the `reader`, check whether the target ad is
the **last ACTIVE ad** in its ad set; if so, add a companion `set_status→PAUSED` op on the parent ad set
to the same plan (each op independently approvable). Put this in `control.py` (a testable helper the
propose tool calls), not in `mcp_server.py`. Enabling (ACTIVE) does not cascade.

### Token scope: clear read-only error

Writes need `ads_management`; reads need only `ads_read`. There is no scope pre-check today. Use the
mandatory validate pass as the natural pre-flight: a read-only token fails validate_only with a Meta
permissions error. In `execute_plan`, catch the `MetaApiError` whose message signals a permission/scope
failure (e.g. contains `ads_management`, `(#200)`, `#10`, or `permission`) and re-raise as a clear
`ToolError`: *"The configured META_ACCESS_TOKEN lacks ads_management (writes need it; it looks
read-only). Reads work; set an ads_management-scoped token to execute."* Keep the mapping in one helper;
a `/debug_token` pre-check is out of scope (no new Graph call needed — the validate pass suffices).

### Media uploads: NOT exposed (documented exception)

`MetaMarketingApiClient.upload_video` / `upload_image` create inert, unreferenced assets and bypass the
gate. **Do not** expose them as MCP tools. The MCP write surface stays 100% gated: the operator uploads
via the CLI (`upload_video` / `upload_image`), obtains asset ids, then the agent proposes
`create_video_ad`/`create_ad` with those ids (authoring ticket). Document this exception explicitly in
`docs/META_ACTION_WORKFLOW.md`.

### Server wiring (mcp_server.py stays a thin entrypoint)

All lifecycle/orchestration lives in `proposals.py` + the existing builders. `mcp_server.py` adds a
`build_write_tools(reader, approval_gate)` (pure, like `build_read_tools`) returning
`{tool_name: callable}`, registered in `build_server` alongside the reads with `_wrap_tool_errors`. The
shared `reader` is the existing `DirectMetaReader`; the write client is built lazily inside execute.
Update `build_server_info` if it should advertise a `write_tools_enabled` capability flag.

### settings.json + boundary doc

- `.claude/settings.json`: JSON has no comments — add a top-level `"_comment"` key recording the
  enforced intent: *"deny-list bars UNGATED MCP writes (the official `mcp__meta-ads__*` connector, which
  stays read-only). Our custom server's gated tools carry `mcp__meta-suite__*` and are sanctioned —
  they route through propose→approve→validate→execute. The monthly MCP-write auditor still flags any NEW
  ungated write tool, on either prefix."* Keep every existing `mcp__meta-ads__*` deny rule.
- `docs/META_ACTION_WORKFLOW.md`: document the MCP guarded-write path (propose → human approve →
  execute → verify), the `mcp__meta-suite__*` vs `mcp__meta-ads__*` distinction, the media-upload
  exception, and the "PAUSED ≠ delivery stopped; verify next-day spend = $0 / pause the last-ad's ad
  set" lesson.

## Edge cases & interactions

- **Propose-then-execute, no human approval** → refused (0 approved ops). The core safety test.
- **execute must load by id, never a body** — a plan dict from the agent would carry forged
  `status:approved`. Test that `execute_plan` has no plan-body parameter and reads the persisted file.
- **CBO** — `propose_set_daily_budget(adset_id=...)` under CBO returns a non-executable ad-set pointer +
  a campaign-level op; executing approves/sends only the campaign op. `apply`-time
  `classify_adset_budget` still refuses an ad-set budget write under CBO. Cover both.
- **Partial failure mid-execute** — one op errors, others succeed: per-op results reported; no op
  silently marked done; later ops not aborted-as-failed. No half-approved surprises.
- **Re-execute an already-executed proposal** → refused by the idempotency guard.
- **Read-only token** → clear ads_management error surfaced at the validate pass (mock the client
  raising a permissions `MetaApiError`).
- **Last active ad paused** → companion ad-set pause op added at propose time.
- **Missing/invalid plan_id** → clear `ToolError`, server keeps serving.
- **Concurrent proposals** — unique `plan_id`s; loading one never clobbers another.

## TODO

### Phase 1 — control-layer builders
- Add `control.build_single_op_plan` (reuses `validate_op` + `write_grounding.attach_op_grounding` +
  `review.review_ops_plan`) covering rename / set_creative / set_creative_features / the four targeting
  ops. Confirm a `pause_ads` builder is exposed for `propose_pause_ads` (there is a `pause_ads` intent
  builder; wire or thin-wrap it).
- Add the last-active-ad ⇒ pause-ad-set companion helper in `control.py`.

### Phase 2 — proposals.py (store + orchestration + seam)
- `save_proposal` / `load_proposal` / proposals-dir path helper + unique `plan_id`.
- `ApprovalGate` Protocol + `ApprovalError` + default `PlanStatusApprovalGate`.
- `execute_plan` orchestration: load → idempotency → approval → lazy write client → validate pass →
  execute pass → audit → outcome verify. `preview_plan` local dry run.
- `PLAN_APPLIERS` dispatch (ops branch wired; leave the map open for authoring/rotation).
- Token-scope error mapping helper.

### Phase 3 — mcp_server.py wiring
- `build_write_tools(reader, approval_gate)` returning the control `propose_*` + `preview_plan` +
  `execute_plan` callables; register in `build_server` with `_wrap_tool_errors`; add
  `write_tools_enabled` to `build_server_info`.

### Phase 4 — settings.json + docs
- Add the `_comment` key to `.claude/settings.json`; keep all deny rules.
- Update `docs/META_ACTION_WORKFLOW.md` (guarded-write path, prefixes, upload exception, pausing lesson).

### Phase 5 — tests (mock-only; NO live Meta call anywhere)
Reuse `_ControlFakeClient` (has `update_ad`/`update_adset`/`get_ad`/`get_adset`/`get_campaign`) as both
client and reader-backing.
- propose→execute with no approval ⇒ refused (0 approved ops).
- an approved op (persist a plan with `status:approved`, simulating the eventual stamp) ⇒ validate pass
  then execute pass ⇒ `executed`; audit artifact written; outcome read-back present.
- `execute_plan` takes only `plan_id` (no plan-body param); loading a forged body is impossible.
- CBO: `propose_set_daily_budget(adset_id=...)` under a CBO fake ⇒ campaign-level op; ad-set budget
  write under CBO refused at apply.
- partial failure: one op fails, per-op statuses correct, others unaffected.
- re-execute ⇒ refused (idempotency).
- read-only token ⇒ clear ads_management error at validate.
- last active ad paused ⇒ companion ad-set pause op present in the proposal.
- run the full suite: `pytest -q 2>&1 | tee /tmp/mcp-write-core.log` (stream output; do not silent-redirect).
