description: Over our own Meta MCP server, an agent can propose brand-new campaigns, ad sets, and ads, but there is currently no way to actually run those proposals — decide how a human should consciously green-light a from-scratch create so it can execute.
prereq: mcp-local-approval-gate
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/proposals.py, src/meta_ads_analysis/authoring.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## The gap

The `mcp-guarded-write-authoring-rotation` ticket added six authoring `propose_*` tools to the custom
MCP server. Four of them create **net-new** entities:
`propose_create_campaign`, `propose_create_adset`, `propose_create_ad`, `propose_create_video_ad`.

A net-new create has no prior entity to measure, so its builder cites a **zero sample** →
`abstain` (the cold-create boundary, mirroring the cold-ad enable). When such a plan is approved and
sent to `execute_plan`, the apply-time grounding gate (`require_grounding` in
`authoring.apply_authoring_plan`) **blocks** it — correctly — because auto-executing a create on no
evidence should require a conscious operator override.

The problem: **there is no MCP-side path to perform that conscious override.** The only way to drop
`guardrails.requires_grounding` today is to hand-edit the persisted proposal JSON (which the unit
tests simulate). So over MCP these four tools can **propose but never execute** — the create is
hard-blocked at apply with a clear per-op reason, but the operator has no in-band way to say "yes, I
know it's net-new, create it anyway (PAUSED)."

The two authoring tools that *can* execute over MCP today are `propose_duplicate_ad` (grounded on a
proven source ad's own metric) and `propose_lookalike` (a structural abstain the gate allows).

This is a **design decision**, not a code bug — the block is intentional. It is filed as a future
concern because it needs an operator-facing decision, and because it is closely related to the
`mcp-local-approval-gate` seam (both are "a human consciously authorizes something over MCP that the
agent must not be able to forge").

## What "done" looks like — pick one, document why

Decide and implement **one** of:

1. **Fold the override into the approval seam.** When the human confirmation from
   `mcp-local-approval-gate` flips a net-new create to `approved`, that same un-forgeable confirmation
   also authorizes dropping `requires_grounding` for that plan — i.e. a human approving a net-new
   create IS the conscious override. This is likely the cleanest: one human gesture, one seam.
2. **A separate, explicit override signal** (a distinct confirmation token / flag on execute) so
   "approve" and "override the cold-create abstain" stay two deliberate acts.
3. **Accept net-new-create-is-propose-only over MCP** and document it: the operator must use the CLI
   to execute net-new creates, and the MCP tools exist only to draft/persist them. (If chosen, make the
   `execute_plan` refusal message for a blocked net-new create point the operator at the CLI path.)

## Constraints / interactions

- The override must never be something the **agent** can set on its own — same forgery concern as the
  approval gate. This is why the prereq is `mcp-local-approval-gate`: reuse its un-forgeable source.
- Do **not** weaken the default: net-new creates stay `abstain` and stay forced PAUSED. This ticket
  only adds a *conscious human* path to execute an approved one; it does not auto-execute anything.
- `propose_duplicate_ad` / `propose_lookalike` already execute on approval alone — leave them as-is.
- Update `docs/META_ACTION_WORKFLOW.md` and the AGENTS.md write-tool catalog once the path exists (the
  catalog currently notes "no MCP tool drops it yet — see `mcp-local-approval-gate`").
