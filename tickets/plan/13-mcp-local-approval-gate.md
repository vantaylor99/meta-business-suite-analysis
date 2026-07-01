description: Make sure a change can only be executed after a real person has confirmed it — the agent must never be able to approve its own proposed action. This is the local, single-operator version of the check; full role-based (supervisor) approval comes with the Azure/tiering work later.
prereq: mcp-guarded-write-tools
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/control.py
difficulty: hard
----
## Why

A guarded write is only safe if the **approval** between propose and execute comes from a human, not
from the agent. In the CLI world that checkpoint is implicit: a human runs `apply --execute`. Over
MCP the *agent* triggers execute, so approval must be something the agent structurally cannot forge —
otherwise it's checkpoint theater (and re-opens the incident the boundary rule was written for).

This ticket delivers the **local, single-operator** version: enough to play with the flow safely on
one machine. The full multi-user, supervisor-approves-specialist, role-based version is deferred to
the backlog `mcp-role-based-access-tiers` ticket (Entra ID + server-side approval state in Azure).

## Scope / what "done" looks like

- A proposed plan cannot be executed until a **human confirmation** flips it to `approved`, and the
  agent has **no tool** that performs that flip. Options to resolve in design (pick one, document why):
  - a small human-run CLI/command that stamps the plan `approved` (out-of-band from the agent), or
  - a one-time confirmation token the human hands back that the `execute` tool must verify, or
  - a signature/HMAC over the approved plan using a secret the agent process never holds, verified by
    `execute` before any Meta call.
- The `execute` tool reads approval; it never writes it. Reuse the existing `apply_ops_plan` invariant
  (only `approved` ops are sent) — this ticket adds the *un-forgeable source* of that `approved`
  state, it does not re-implement the gate.
- Mock-only tests: agent-initiated approval attempt is impossible/rejected; a human-confirmed plan
  executes; a tampered/edited "approved" plan without a valid confirmation is refused.

## Design notes

- Keep the mechanism swappable: the local human-confirmation source should sit behind a seam so the
  later Azure/role-based approval (supervisor token → server-side `approved` state) drops in without
  rewriting the `execute` tool. Mirror the reader-seam pattern (`reader_provider.py`) — one interface,
  local vs hosted implementations.
- Document plainly for the operator: propose (agent) → review + confirm (you) → execute (agent). This
  is the loop the user wants to "play with" locally.

## Edge cases & interactions

- Agent has filesystem write access locally → a plain `status: approved` field in a file it can edit
  is NOT sufficient on its own; the confirmation source must be outside the agent's reach (that's the
  whole point). Call this out in the chosen design.
- Confirmation replay: a used token/signature shouldn't approve a *different* or *mutated* plan —
  bind the confirmation to the specific plan's contents.
- Expiry: a stale approval (plan proposed days ago, account state moved on) should not silently
  execute; consider requiring a fresh validate_only + outcome check at execute time.
