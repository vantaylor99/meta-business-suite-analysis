# Knowledge Base — read this first

This is the durable, human- and agent-facing memory for this project. Its purpose is
so that a new agent (or person) can get fully oriented **without re-reading every file
or re-deriving what we already know**. If you are an agent starting a session on this
repo, read this directory before touching data or taking action.

## Read order

1. `knowledge/README.md` (this file) — how the knowledge base works.
2. `knowledge/learnings.md` — durable, cross-account lessons (Meta API behavior, advertising
   principles, gotchas we've already hit). Don't relearn these the hard way.
3. `knowledge/accounts/<account>/profile.md` — the account's goal, structure, audiences,
   and current performance baseline.
4. `knowledge/accounts/<account>/decision-log.md` — dated history of what we changed and why.
5. `knowledge/accounts/<account>/experiments.md` — what we're currently testing, what we're
   waiting on, and what past tests taught us.
6. Only then: the latest `reports/<account>/<date>/` output and live data.

## How this relates to the other docs

- `AGENTS.md` — *how to analyze* the data and write the report (the analysis contract).
- `config/meta_ads_accounts.json` — *machine-readable* account settings and action policy.
- `knowledge/` (here) — *narrative history + lessons + experiments*: the "why" and the "so far".

## Conventions (keep this base trustworthy)

- **decision-log.md is append-only and dated** (`YYYY-MM-DD`). Newest entries at the top.
  Record every change made to a live account, plus the reason and the result.
- **experiments.md** tracks hypotheses with: hypothesis, change made, status, what we're
  waiting on, success signal, and (when done) the conclusion. Move concluded learnings into
  `learnings.md` so they become permanent.
- **learnings.md** holds generalized, durable facts — date-stamp each so staleness is visible.
- Convert relative dates to absolute. Cite ad set / ad names and IDs so entries are actionable cold.
- Keep entries concise and skimmable. This base should stay readable in a few minutes.
- **Update this base at the end of any session** that changed an account or taught us something.
- This directory is committed to git, so it travels to every machine and every agent.
