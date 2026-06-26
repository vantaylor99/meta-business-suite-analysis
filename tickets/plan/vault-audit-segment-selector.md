description: When the vault re-checks a stored number that was sliced by two or more dimensions at once (for example "Instagram, in the feed"), it can't tell which live row to compare against and gives up. Let a fact say exactly which slice it came from so the re-check can find it.
prereq:
files: src/meta_ads_analysis/cli.py, src/meta_ads_analysis/knowledge_provenance.py, knowledge/README.md, knowledge/learnings.md
difficulty: medium
----
## Problem

`audit-vault` resolves a stored `metric:` claim to a single fresh live row by **token-matching** the
metric name against each row's breakdown segment value(s) / entity name (`resolve_fresh_metric` in
`cli.py`, helped by `_metric_identifier_tokens` / `_row_identifier_text`). When a metric name's
discriminating tokens match **exactly one** fresh row it resolves; **zero or several matches abstain**
(`could_not_audit`) — by design it never guesses.

That heuristic is correct-but-shallow, and one **real, already-stored** claim falls through it today:

```
knowledge/learnings.md:193
  metric: ig_roas=3.63
  verify: … --breakdown publisher_platform,platform_position
```

A two-dimension breakdown returns many `instagram × {feed, stories, reels, …}` rows. The token
`{instagram}` matches **all** of them → ambiguous → `could_not_audit`. So this claim can **never** be
auto-re-verified; a `lint-vault ⏳ re-verify` flag on it will never clear via `audit-vault` (it needs
manual re-verification). The single-dimension `ig_roas=2.79` (publisher_platform only) resolves fine;
it's specifically the multi-dimension and abbreviation-not-in-the-alias-map cases that abstain.

This is a **coverage limitation, not a correctness bug** — abstaining is the safe direction (it never
fabricates a confirm or a false refutation). But it means a class of legitimately-stored claims is
silently un-auditable.

## What to build

Capture an **explicit segment / entity selector in the provenance tag** so resolution is *exact*
instead of name-matched. Sketch (final syntax is the implementer's call):

- An optional structured selector on the `metric:` line, e.g.
  `metric: ig_roas=3.63 @ publisher_platform=instagram,platform_position=feed` (or a dedicated
  `select:` field), so a claim states precisely which row it summarizes.
- `resolve_fresh_metric` matches a fresh row when **all** selector key/values are present in its
  `segment` dict (exact, not token overlap), falling back to today's heuristic only when no explicit
  selector is given (backward-compatible).
- `lint-vault` could optionally validate that a multi-breakdown `metric:` line carries a selector
  (warn, not error, to avoid breaking existing entries).

## Use cases / expectations

- A claim about "Instagram **in the feed**" (two dimensions) re-verifies automatically.
- Abbreviations not in the small alias map (`ig/fb/an`) become unnecessary — the selector names the
  segment value verbatim.
- Existing single-dimension claims keep working with no migration.
- The audit still **abstains rather than guesses** when a selector matches zero or several rows
  (e.g. a renamed ad set) — the safe-direction invariant is preserved.

## Out of scope

Anything beyond resolution: the verdict math, band decrement, `➖` logging, and read-only/report-only
guarantees are unchanged.
