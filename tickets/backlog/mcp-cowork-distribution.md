description: Package the MCP server as a connector that every specialist's Claude Cowork account gets automatically, so the whole team works through the same tools and guardrails without anyone checking out code.
prereq: mcp-role-based-access-tiers
files: docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Why (future — not active local-play work)

Claude Code (a checked-out repo + CLI) doesn't scale to a department of non-technical specialists.
The delivery vehicle is **Claude Cowork** with our hosted MCP server wired in as a **required
connector** on every specialist account — so everyone gets identical reads, guarded writes, and the
role-appropriate knowledge tools, and nobody can run an ungated path.

## What it should cover (spec, to be designed later)

- Package/register the hosted server as a Cowork connector; the mechanism to **force-include** it for
  every new account in the org (org-level connector policy).
- Per-user connection uses the tier auth from `mcp-role-based-access-tiers` (the connector carries the
  user's identity; role decides tools/scope).
- Onboarding: what a specialist sees on day one, how a supervisor is provisioned, how the department
  head's cross-area view is enabled.
- Rollout/versioning: how server tool changes propagate to all connected Cowork accounts safely.

## Open questions for design time

- Cowork org-admin capabilities for mandatory connectors (confirm what's actually enforceable).
- Fallback/UX if the server is unreachable.
- Change management: piloting with a few specialists before org-wide force-include.
