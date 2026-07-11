description: Roll up account performance along the org chart — each specialist as a line a supervisor can expand, each area a line the department head can expand — instead of one flat list of all accounts. Deferred until the account-grouping model exists.
prereq: mcp-role-based-access-tiers
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, docs/META_API_SETUP.md
difficulty: medium
----
## Why this is backlog, not active

Every other multi-account tool in this project works against a **single implicit group = all
reachable accounts**, because we deliberately deferred the grouping decision (dynamic groups vs.
purpose-scoped tokens; who-owns-which-account mapping). A true hierarchy rollup —
specialist → supervisor → area → WWFT — is only meaningful once that grouping model exists. It is
therefore parked until `mcp-role-based-access-tiers` (and its own prereq
`mcp-azure-knowledge-store`) establish where the specialist/supervisor/area assignments live.

## What it will deliver (spec, to design later)

- Given a grouping source, aggregate the per-account metric rows (from
  `cross_account_performance`) at each level of the hierarchy: totals + derived metrics per
  specialist, per supervisor, per area, and org-wide.
- Drill-down shape: a supervisor sees one line per specialist and can expand to that specialist's
  accounts; a department head sees one line per area.
- Derived metrics recomputed from summed components at each level (never averaged up the tree);
  currency normalized to a reporting currency for cross-level comparison.

## Open questions (resolve with the grouping decision)

- Where do specialist/supervisor/area assignments come from — derived from account-name/business
  conventions, an explicit config mapping, or Entra ID groups? (Same open question as
  `mcp-role-based-access-tiers`.)
- Does data scope here follow the caller's role (a supervisor only rolls up their own department),
  tying this tool to the auth work?

## Use cases

- Supervisor: department rollup with each specialist as an expandable line.
- WWFT / department head: area-by-area rollup across the whole org.
