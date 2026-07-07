description: When someone manages more than one mission's ad account (a supervisor over several missions, not a single-account specialist), automatically figure out which accounts belong to them from an org roster, instead of a person manually building that list one account at a time.
prereq:
files: docs/SPECIALIST_ONBOARDING.md, scripts/onboard_specialist.sh, config/meta_ads_accounts.example.json, tickets/backlog/mcp-role-based-access-tiers.md
difficulty: medium
----
## Why (future — not needed for the current single-account case)

Floated 2026-07-07 while onboarding Matthew (an FSC leader) onto this system. Confirmed he manages
just one account (Seattle Mission), so the single-account registry design already built
(`scripts/onboard_specialist.sh`, `docs/SPECIALIST_ONBOARDING.md`) covers his case as-is — this
ticket is not blocking anything active. But the idea behind it is real: the next person onboarded
this way might genuinely be a supervisor over *several* missions, and `mcp-role-based-access-tiers`
already specifies "Supervisor... their department's accounts" as a data-scope rule — this ticket is
the concrete mechanism for *populating* that scope at onboarding time from an authoritative source,
rather than a human hand-typing a list of account IDs.

## What it should cover (spec, to be designed later)

- A source of truth mapping people to the accounts they manage. Floated approach: have the person
  upload an export of the relevant SharePoint list (a static file dropped into the Cowork/Desktop
  chat) rather than wiring up a live SharePoint/Microsoft 365 connector for every specialist's
  machine just for this one lookup — much less setup for a capability used once at onboarding.
- Cross-reference by name (or employee ID/email if names collide in a large org) against that
  roster to derive which mission/account IDs belong to them.
- Cross-check the derived account list against what their Meta token can actually reach (fetch
  `/me/adaccounts` — see `META_API_SETUP.md`'s token sanity-check) and surface any mismatch loudly
  rather than silently proceeding with an account the token can't actually see, or silently
  granting access to more than the roster says.
- Still requires a human to supply the curated per-account policy (targets, guardrails) — this
  ticket is only about discovering *which* accounts are theirs, not about authoring the numbers for
  each one; see `mcp-guided-policy-drafting` for that half.

## Open questions for design time

- What should happen if the roster says someone manages zero accounts, or an unexpectedly large
  number — how loud should that surprise be, and to whom?
- Roster staleness: SharePoint exports go stale. Does discovery run once at onboarding, or need a
  periodic re-check as people's mission assignments change?
- Name-collision handling at real org scale.
