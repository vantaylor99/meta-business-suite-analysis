description: Give the server three levels of access — specialists who run their own accounts, supervisors who oversee a department and approve specialists' changes, and a department head who can see across all areas — with each person's identity deciding what they can do. This is where supervisor approval and real login come in.
prereq: mcp-azure-knowledge-store
files: src/meta_ads_analysis/mcp_server.py, .claude/settings.json, docs/META_API_SETUP.md
difficulty: hard
----
## Why (future — not active local-play work)

The local rig approves writes with a single-operator human confirmation. The production shape is
**role-based**: identity (Entra ID / Azure AD group membership → role claim) decides which tools a
caller can invoke and which data they see. This is also where the write-approval checkpoint becomes
genuinely un-forgeable across people: a **specialist** proposes, a **supervisor** approves, the
specialist executes — and no specialist credential can call the approve tool.

Same codebase, one capability surface, authorization by role — not three forks. Deploy as separate
endpoints only if blast-radius separation is wanted.

## Tiers (spec, to be designed later)

| Tier | Can call | Data scope |
|---|---|---|
| Specialist | reads, `propose_*`, `execute_approved`, `record_learning` | only their own accounts |
| Supervisor | `approve_plan`, `query_learnings` | their department's accounts |
| Department head | cross-department analytics (read-only) | all departments |

## What it should cover

- Real auth on the hosted server (OAuth / Entra ID), replacing the local token→role stub; role claim
  drives tool exposure + per-call authorization + data scoping.
- Server-side approval state (in the Azure store) that a specialist token cannot write — the
  production replacement for `mcp-local-approval-gate`'s local confirmation seam.
- Per-specialist Meta-account scoping (a specialist only touches their accounts) enforced server-side.
- Reconcile with the `mcp-read-cli-write-boundary` rule and the `.claude/settings.json` deny-list at
  org scale: the enforced intent is "no *ungated, unauthorized* writes over MCP."

## Open questions for design time

- Group/role model mapping to departments and areas.
- Where the Meta token(s) live (per-specialist vs shared service credential + Key Vault).
- Audit: who approved/executed what, retained where.
