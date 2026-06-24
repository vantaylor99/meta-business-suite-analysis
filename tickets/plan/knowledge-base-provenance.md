description: Make the knowledge base (the committed knowledge/ vault that the agent reads first) wear its sourcing on its sleeve — every stored fact says where it came from, links to how to reproduce it, carries a "last verified" date, and gets flagged when it goes stale or when fresh data contradicts it. The point is that a wrong guess can't quietly harden into "fact" that 25 future specialists trust.
prereq: grounded-recommendations
files: knowledge/README.md, knowledge/learnings.md, knowledge/accounts/divine_designs/profile.md, knowledge/accounts/divine_designs/decision-log.md, AGENTS.md
----
## Why

The `knowledge/` vault is the trust anchor. The agent reads it first when analyzing or acting on an
account, and the ~25 MTC specialists will inherit it as a template. That makes it a double-edged
tool: it grounds recommendations in dated evidence (good), but it can also **launder a bad inference
into apparent fact** — once a wrong conclusion is written with 🟡 Medium confidence, a later session
treats it as established ground truth and builds on it. The system trusts its own prior outputs.

This ticket hardens the vault so a stored fact always carries enough provenance to be re-checked,
ages out instead of silently persisting, and gets challenged when reality disagrees. It is the
knowledge-base counterpart to `grounded-recommendations` (which grounds *live* recommendations); the
two must share ONE confidence/provenance vocabulary, not invent competing ones.

## What this is

### 1. Strict provenance on every evidence line

Every evidence/learning entry must declare its source class — **observed** (direct API/data
observation), **inferred** (agent reasoning over data), or **external** (web / practitioner / Meta
docs). `learnings.md` already gestures at this in prose (e.g. "_(direct API observation;
divine_designs)_" vs "_(practitioner consensus…)_"). Make it a required, consistent, ideally
machine-checkable tag rather than freeform. This is the same observed/inferred/external axis used by
the grounding tier in `grounded-recommendations` — reuse it verbatim.

### 2. Evidence links to a reproducible artifact

A data-backed fact should name the command/query + window that regenerates it (e.g. the
`account_metrics --level … --date-from … --date-to …` that produced the number), so anyone — a
future agent or a skeptical specialist — can re-run and confirm rather than take it on faith. Same
auditability principle as cite-the-basis, applied to stored knowledge.

### 3. Staleness / re-verification

Facts about a *live* ad account rot (the account changes, the platform changes). Each such fact
carries a **last-verified date**; facts older than a threshold are flagged for re-verification
rather than trusted blind. Distinguish fast-rotting account/platform facts from slow-rotting
evergreen principles — only the former should age out aggressively.

### 4. Loud contradiction surfacing

When fresh data contradicts a stored fact, the system must flag it prominently and **lower the
fact's confidence** (per the 🟢/🟡/🔴 rubric with its evidence log) — never silently override or,
worse, silently keep the stale belief. Consider a periodic "audit the vault against current data"
pass that diffs stored claims against freshly-pulled metrics and reports drift. Contradiction
lowers confidence and logs the conflict; it does NOT auto-delete the entry (a human decides).

## Use cases / expected behavior

- A learning that says "Engaged Audience holds ~3.7 ROAS" carries `observed`, the regenerating
  query, and a last-verified date; six weeks later it's flagged "re-verify" rather than cited as
  current truth.
- A new specialist reading the vault can tell at a glance which facts are hard observations, which
  are agent inferences, and which are unconfirmed web claims.
- A vault audit pulls fresh metrics, finds the Engaged Audience now runs 2.1 ROAS, surfaces the
  contradiction loudly, and lowers that learning's confidence with a dated ➖ evidence line.

## Edge cases & interactions

- Must not break the existing `learnings.md` format or the README confidence rubric — extend them,
  don't replace them. One confidence vocabulary across the whole repo.
- Provenance tags should be both human-readable and machine-checkable (a lint could later verify
  every entry has one) — but don't over-engineer in the spec; the plan stage settles the format.
- Staleness applies to account/platform facts, not to evergreen principles — don't flag "lead with
  a hook" as stale because it's 90 days old.
- Contradiction surfacing lowers confidence and logs; it never auto-deletes a learning.
- External-sourced facts inherit the caps from `grounded-recommendations` §5 (capped, recency- not
  upvote-weighted, link+date+quote) — keep the two tickets consistent.
- Read-only with respect to Meta — this is about how knowledge is recorded and audited.
