description: Let Claude look at an account's real past performance and suggest a reasonable goal (like "aim for $10 per lead") instead of a person having to work that out by hand — but always ask in plain language and get a clear yes before saving anything, never let Claude change the file on its own.
prereq:
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/account_registry.py, config/meta_ads_accounts.example.json, docs/SPECIALIST_ONBOARDING.md
difficulty: medium
----
## Why (future — not needed for the current single-account case)

Floated 2026-07-07 while onboarding Matthew (an FSC leader, single-account case, already covered
by the existing `scripts/onboard_specialist.sh` flow). The idea: instead of the operator manually
deriving a target cost-per-result (or ROAS floor, etc.) from historical data and hand-writing it
into `config/meta_ads_accounts.json`, Claude could pull the account's real performance and propose
sensible values conversationally — cutting out the manual analysis step, especially valuable once
someone is onboarding many accounts (pairs naturally with `mcp-sharepoint-account-discovery`).

## The non-negotiable part: this has to work for non-programmers

Everyone this reaches (Matthew, Hunter, and whoever comes after them) is explicitly **not a
programmer** and can get confused or overwhelmed easily. Whatever "confirm before it's saved" step
this ticket builds must not look or feel like a code review:

- No raw JSON diffs, no "here's the patch, approve it" CLI-style prompt.
- Ask in plain language, one thing at a time: *"Based on the last 90 days, this account's actual
  cost per lead has been about $14. Does aiming for $10 per lead still sound right, or should we
  set the goal somewhere else?"* — not *"set target_cost_per_result to 10.0? y/n"*.
- After saving, restate what happened in plain language so they can verify without reading JSON:
  *"Got it — I've set the goal to $10 per lead, and I'll flag ads above $40 per lead for review."*
- If several values need confirming for one account, confirm them one at a time, not as a single
  dense batch — a long list invites a rushed blanket "yes" to something not actually read.

## What it should cover (spec, to be designed later)

- A capability (new MCP tool, or an extension of an existing one) that computes a suggested value
  from real historical data — e.g. pull the account's trailing-N-day insights, compute the actual
  average cost-per-result, and phrase it as the plain-language suggestion above.
- The write itself still needs an explicit human confirmation before touching
  `config/meta_ads_accounts.json`, in the same *propose → human confirms* spirit as Meta account
  writes — but the **presentation** must be conversational, not the `approve_plan`/plan-id/JSON-diff
  mechanism built for Meta writes (that assumes comfort with a plan_id and a JSON body; not
  appropriate to reuse here as-is for this audience).
- Decide whether this needs the same approval-secret gate as Meta writes, or something lighter —
  this file doesn't spend money directly, but it does drive later automated pause/scale decisions
  (`apply-actions`, `audit-vault`), so a wrong value has real, if delayed, consequences.

## Open questions for design time

- Should this only ever *suggest*, with the human always the one who states the final number, or
  can they just say "yes, that number, go ahead" conversationally and have Claude write it?
- How to phrase data-thinness honestly ("only 12 days of data, so this is a rough guess") in a way
  a non-technical reader trusts appropriately — not falsely confident, not so hedged it's useless.
- If paired with `mcp-sharepoint-account-discovery` for a multi-account supervisor, this would run
  once per account — needs to not feel repetitive or tedious across a dozen accounts.
