description: Let our own MCP server make account changes (pause, budget, status, audience rotation, create-paused) — but only by running them through the same propose-review-approve-execute safety flow we already built, never as raw one-shot API calls. This deliberately revisits our "no writes over MCP" rule.
prereq: mcp-server-scaffold
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/write_grounding.py, .claude/settings.json, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## Why

The whole point of owning the server is to move the **guarded write flow** behind it so specialists
can act on accounts through a connector with the guardrails enforced *in the server* (a capability
boundary) rather than as prompt rules an agent can be talked out of. This ticket exposes the writes —
but only as wrappers over the existing gate, never the raw Graph client.

## Hard constraint — consistent with the boundary rule, correctly scoped

The project's write-boundary rule bars **ungated** writes over MCP — specifically the **official Meta
connector**, whose write tools (`ads_update_entity`, `ads_activate_entity`, …) execute *immediately*
with no propose→approve→validate→execute gate, no confidence band, no adversarial review, no audit
entry, no PAUSED-by-default. A real Jun-2026 incident ($300/day stuck `IN_PROCESS` ad) came from
exactly that. `.claude/settings.json` `permissions.deny` hard-blocks `mcp__meta-ads__*` (the official
connector) — and that must **stay** blocked.

This ticket does NOT reopen that rule. A **custom** server whose write tools route through the guarded
flow is precisely what the rule always allowed — the prohibition was on *ungated* writes, not on the
MCP transport. To stay within the rule, the design MUST preserve every guarantee it protects, or it
must not ship:

- **Never expose the raw API.** No write tool may call `MetaMarketingApiClient.create_*/update_*`
  directly. Every write tool routes through the existing guarded pipeline
  (`control.apply_ops_plan`, `authoring`, `rotation`) so Evidence + a **computed** confidence band,
  the adversarial review gate (`review.py`, demote-only), the audit entry, and **PAUSED-by-default**
  all still apply.
- **Split propose from execute** (see `mcp-local-approval-gate`, which depends on this): a `propose_*`
  tool produces a proposal artifact at status `proposed`; a separate `execute` tool sends to Meta and
  **only sends ops already marked `approved`** — exactly today's `apply_ops_plan` behavior
  (`control.py`: ops whose status ≠ `approved` are skipped). The agent must not be able to move an op
  to `approved` itself.
- **Keep the deny-list on the official connector; recognize our gated tools as legitimate.** The
  `mcp__meta-ads__*` deny rules (Meta's official connector) stay in place — that connector remains
  read-only. Our server's tools carry a different prefix, so they aren't caught by those rules; that's
  correct, not a loophole, because ours are gated. Document this in `.claude/settings.json` and the
  boundary rule so the enforced intent reads clearly as "no *ungated* writes over MCP," and the monthly
  MCP-write auditor knows our guarded tools are sanctioned (and still flags any *new ungated* write
  tool, on our server or the official one).
- **Verify outcome.** Carry the "PAUSED is not proof delivery stopped — verify next-day spend = $0"
  lesson: the execute path should support/require an outcome-verification read, and pausing the only
  ad in an ad set should pause the ad set too.

## Scope / what "done" looks like

Expose the reversible + authoring surface already supported by the CLI, each as propose/execute MCP
tools wrapping the existing builders:
- Ops: `set_status` (pause/enable), `set_daily_budget` (CBO-aware, campaign-level under CBO),
  `rename`, `set_creative`, `set_creative_features`, and targeting ops
  (`set_age_range`/`set_genders`/`set_geo_locations`/`set_placements`) — from `control.py`.
- Authoring (PAUSED-by-default): create campaign/ad set/ad, duplicate ad, lookalike — from
  `authoring.py`. **No delete/archive.**
- Audience rotation / advantage-audience disable — from `rotation.py`.
- Mock-only tests proving: a proposed-but-unapproved op is **refused** by execute; an approved op runs
  through validate_only then execute; the audit artifact is written; created entities are PAUSED.
  **No live Meta call anywhere.**

## Edge cases & interactions

- Agent calls `execute` on a plan it just proposed, with no human approval in between → must be
  refused (this is the core safety test; the approval mechanism itself is `mcp-local-approval-gate`).
- CBO budget writes must target campaign-level budget, not ad-set (the known gap called out in the
  hybrid-build notes) — cover it here rather than inheriting the bug.
- Media uploads (`upload_video`/`upload_image`) create inert, unreferenced assets and today bypass the
  gate — decide whether to expose them and document the exception explicitly (as the docs already do).
- Partial failure mid-plan (some ops execute, one errors) → results must report per-op status; no
  half-approved surprises.
- Token scope: writes need `ads_management`; surface a clear error if the configured token is read-only.
