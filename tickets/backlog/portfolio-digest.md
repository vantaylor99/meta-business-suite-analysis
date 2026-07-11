description: A single "give me my portfolio overview" tool that returns one ranked digest — totals, each account's goal verdict, what changed and needs attention, and budget pacing — in one call, instead of making four separate calls and stitching them together by hand.
prereq: grade-accounts-against-goals
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why parked in backlog (promote after grading lands)

This tool **composes** `grade_accounts_against_goals`, so it can only be built once that tool exists.
It is parked here and should be promoted to `plan/` right after `grade-accounts-against-goals`
completes — building it against a not-yet-existing grade tool would be guesswork.

## What it delivers

A one-call daily-driver (working name `portfolio_digest`) over a scope + window that returns a single
ranked digest by **composing the existing tools** (do not reimplement their logic):

- **Totals** — from `cross_account_performance` (spend / results / efficiency, normalized).
- **Goal summary** — from `grade_accounts_against_goals`: counts of on_goal / watch / pause_candidate
  and the pause-candidate shortlist.
- **What changed** — from `flag_accounts_needing_attention` (behavior-change flags).
- **Pacing** — from `pacing_report`: over / under / on-track counts + worst pacers.
- **"Needs you"** — a short synthesized shortlist merging the pause-candidates + high-severity flags,
  worst-first.

## Efficiency requirement (the point of a composite)

Calling four tools naively re-fetches the same insights repeatedly. The digest is exactly where the
optimization the `flag_accounts_needing_attention` docstring deferred ("threading a shared perf into
`pacing_report` is a future optimization out of scope") should pay off: fetch
`cross_account_performance` **once** and thread that shared result into grade / flag so the digest
isn't 3-4× the reads of one tool. Keep the read-heaviest opt-ins (ad-health, per-account pacing over
huge scopes) **off by default**; expose flags to turn them on.

## Behavior / interface (proposal — plan stage finalizes)
- `portfolio_digest(date_from, date_to, account_ids=None, reporting_currency="USD", include_pacing=?, include_flags=?)`.
- Output: `{totals, top, bottom, goal_summary, attention, pacing, needs_you}` — each section clearly
  labeled and independently readable.

## Edge cases & interactions
- Large scope → bound it / document a ceiling; inherit the timeout lessons (don't fan the heaviest
  sub-signals over hundreds of accounts by default).
- Accounts with `no_goal_configured` (from grading) → surfaced, not errored.
- Partial failure in any sub-tool isolates into `errors`; the digest still returns the sections that
  succeeded.
- Currency normalized to `reporting_currency` (static FX, per [[currency-precision-low-priority]]).

## Use cases
- Specialist morning check-in; supervisor department overview; WWFT fleet snapshot — the same digest
  at any scope.
