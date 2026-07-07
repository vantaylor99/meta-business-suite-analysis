description: Move our accumulated learnings and per-account knowledge out of markdown files and into a shared Azure database, accessed only through the MCP server — so many specialists can contribute to and draw from one growing knowledge base instead of separate repos.
prereq:
files: knowledge/ARCHITECTURE.md, src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/confidence.py, knowledge/learnings.md, knowledge/accounts/
difficulty: hard
----
## Why (future — not active local-play work; now with a concrete incident behind it)

**2026-07-07 incident:** Divine Designs' real per-account knowledge (`knowledge/accounts/divine_designs/`
— revenue/ROAS figures, real domain, decision log) and the real `config/meta_ads_accounts.json`
(live ad account IDs for every account) were committed directly to this **public** GitHub repo via
plain `git add`/commit, with no gate at all. Fixed reactively: repo flipped to private, those paths
untracked/gitignored, `config/meta_ads_accounts.example.json` added as the committed template
(see PR #14). That fix only stops *this* repo from re-leaking the same way — it doesn't yet stop a
future specialist's local knowledge files from ending up committed somewhere else the same way,
because there is currently no enforced channel knowledge has to flow through. This ticket is that
channel: once knowledge lives behind the MCP server (not files a person can `git add`), the failure
mode that caused the 2026-07-07 incident structurally can't recur.


Today knowledge is a git-tracked markdown vault: cross-account `learnings.md` + per-account
`knowledge/accounts/<slug>/`, with machine-checkable provenance and a confidence/drift audit engine
(`knowledge_provenance.py`). Git can version knowledge but can't role-scope it or aggregate it across
many people. To roll out to a team of specialists (and later, cross-department roll-ups), the shared
knowledge needs to live in a **database behind the MCP server**, on Azure.

This is explicitly the extension of the promotion loop already documented in
`knowledge/ARCHITECTURE.md`: observation (per-account) → corroborated across accounts → promoted to
general — now spanning specialists and departments instead of one operator's folders.

## What it should cover (spec, to be designed later)

- An Azure-hosted store (candidate: Azure Database for PostgreSQL) whose schema mirrors the existing
  provenance model — `LearningEntry` / `EvidenceLine`, the confidence `Band`, the evidence **tier**
  vocabulary — so the "a wrong guess can't harden into a fact" invariant survives the move. Keep the
  confidence vocabulary single-sourced from `confidence.py`; do not fork a second scale in SQL.
- MCP tools on the server: `record_learning` (validates via the existing lint/provenance rules
  *before* insert) and `query_learnings` (scoped by account/department). Knowledge writes flow through
  the server, not direct DB access from clients.
- A repository seam so local testing can run against a local Postgres (Docker) and production against
  Azure by connection-string swap only.
- Decide the fate of the git vault: operational log kept in git vs. fully migrated; how the drift/audit
  engine runs against a DB-backed store.

## Open questions for design time

- Per-account vs cross-account tables and how promotion (account → general) is represented.
- Secret management for DB + Meta creds (Azure Key Vault).
- Migration/backfill of the existing markdown vault into the schema.
