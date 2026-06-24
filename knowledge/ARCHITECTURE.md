# Knowledge architecture & roadmap (per-account → cross-account → template)

How knowledge is organized now, and where it's headed as this grows from one personal account to a
team of specialists each running multiple accounts.

## Two layers

- **General / cross-account** — `knowledge/learnings.md`: (a) **universal platform/API facts**
  (account-agnostic — e.g. validate_only, dev-mode-app blocker, Dynamic-Creative = own ad set), and
  (b) **strategy patterns corroborated across multiple accounts** (e.g. "enhance_CTA tends to lift
  ROAS", "audience type X performs well").
- **Per-account** — `knowledge/accounts/<slug>/`: profile, decision-log, experiments, winning_copy,
  followups. Everything specific to one account lives here.

## The promotion loop (how general knowledge forms and evolves)

1. An observation is recorded in the **account folder** with confidence + dated evidence.
2. When the **same pattern is seen independently in ≥2–3 accounts**, consistent in direction and not
   explained by a shared confound, **promote it to `learnings.md`** as a cross-account learning that
   **cites the supporting accounts** as evidence.
3. Confidence **rises with each independent corroborating account** and **drops when an account
   contradicts** it (the standard confidence+evidence rubric, applied across accounts).
4. **Bidirectional:** general knowledge seeds a *new* account's starting **hypotheses** (priors to
   test, not rules to blindly apply); each account's results feed back to refine general.

## Guardrails on generalizing

- **Label scope.** These accounts are likely a similar vertical (faith/Church-oriented), so a
  cross-account pattern may be vertical-specific, not universal — say "holds across our faith
  accounts," don't claim a universal law.
- **Keep provenance** so any general claim can be audited and down-weighted if an account bucks it.
- **Write claims consistently** (a normalized one-line statement) so they're comparable across accounts.

## Future capability (build at ~3–5 accounts, not 1)

A periodic **cross-account synthesis** task/agent: scan every account's knowledge, detect recurring
patterns, and update the general layer (with provenance + confidence). Premature today with one
account — nothing to cross-correlate yet.

## Roadmap

- **Now:** experiment in this personal repo (divine_designs); learn what's valuable.
- **Cleanup at account #2:** move account-specific strategy currently sitting in `learnings.md`
  (IG>FB, placement policy, enhancements stance) down into the account folder; reserve `learnings.md`
  for universal facts + corroborated cross-account patterns.
- **Template for MTC specialists:** extract a generic knowledge skeleton + guardrail conventions +
  a new-specialist onboarding doc. **Auth via the official Meta MCP (per-person Business OAuth — no
  distributed Graph tokens);** keep our guardrails, workflows, and the knowledge model on top.
