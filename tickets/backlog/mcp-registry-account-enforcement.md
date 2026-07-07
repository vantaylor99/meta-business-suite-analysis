description: Right now, if a specialist's Meta token can reach more ad accounts than the one they're supposed to manage, nothing in our code stops it from being used on those other accounts — this makes that an actual hard limit instead of just something we hope the token permissions get right.
prereq:
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/account_registry.py, docs/META_API_SETUP.md, docs/SPECIALIST_ONBOARDING.md
difficulty: medium
----
## Why

Found 2026-07-07 while onboarding Matthew (single-account case, Seattle Mission): confirmed via
code inspection that `config/meta_ads_accounts.json` being scoped to one account is **not** a real
enforcement boundary today.
- The read tools in `mcp_server.py` (`fetch_insights`, `list_campaigns`, `fetch_ads`, etc.) take a
  raw `ad_account_id` straight from the caller with zero check against the registry.
- The write-proposing tools' `_resolve_account` helper checks the registry first, but **falls back
  to accepting any raw `act_<id>`** if it's not a known slug — so that's not a hard wall either.

What actually enforces "only this account" today is the Meta token's own permission scope (a
System User's asset assignment, per `META_API_SETUP.md`) — our code adds no second layer on top.
We proved this isn't hypothetical: the operator's own personal token (tested live during this same
onboarding work) turned out to have access to a long list of ad accounts well beyond the three it
was assumed to be scoped to.

## What it should cover (spec, to be designed later)

Two distinct pieces — **do both, not just one**; a warning alone doesn't actually protect anything,
it just gives an early heads-up an overwhelmed non-technical user could easily miss or not
understand the significance of:

1. **Enforcement (the actual protection):** every MCP tool that takes an `ad_account_id`/`account`
   argument should reject any value not present in that machine's local
   `config/meta_ads_accounts.json`, full stop — no raw-`act_<id>` fallback bypassing the registry.
   This is what makes the single-account scoping real regardless of how broad the underlying token
   turns out to be.
2. **Onboarding-time cross-check + warning (early detection, not a substitute for #1):** right
   after a token is entered (in `scripts/onboard_specialist.sh` or an MCP-side equivalent), fetch
   `/me/adaccounts` with it and (a) confirm the expected account is actually reachable — fail loudly
   if not, rather than a confusing failure later inside Cowork — and (b) if the token can reach
   *other* accounts too, surface that clearly so the operator can go tighten the token's scope in
   Business Manager before it's relied on.

## Open questions for design time

- Exact error shape for a rejected `ad_account_id` — needs to read sensibly to Cowork so it can
  explain the refusal to a non-technical user in plain language, not just surface a raw exception.
- Whether the registry check belongs in each tool function individually or as one shared wrapper
  (`build_read_tools`/`build_write_tools` already centralize tool assembly in `mcp_server.py` —
  likely the right seam).
- How this interacts with `mcp-role-based-access-tiers`'s eventual per-specialist data scoping —
  this ticket is the local/single-operator-era version of the same guarantee; decide whether it
  gets superseded outright by that work or stays as a defense-in-depth layer underneath it.
