description: Show how one ad account stacks up against its peers — e.g. "is this account's cost-per-lead good or bad compared to the others?" — by ranking it as a percentile within a comparison group.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

A raw number (CPL = $18) means nothing without context. A specialist's real question is "is that
good *for an account like mine*?" This tool answers by placing one account's metrics as
**percentiles against a cohort** (by default, all accounts in scope), so a small number is
interpretable. It's the specialist-facing counterpart to the manager-facing ranking/triage tools —
same underlying metric rows, inverted point of view (one account vs. the field, rather than the
field ranked).

## What it must deliver

- For a target account and a comparison cohort (default = the resolved scope; may be an explicit
  list), report each key metric's value **and** its percentile / rank within the cohort, plus the
  cohort median and quartiles for context.
- **Directionality is explicit**: for cost metrics lower is better; for CTR/ROAS higher is better.
  The output must state whether the account is in a good or bad percentile, not just a raw
  percentile number.
- Money metrics compared in a common reporting currency (reuse `cross_account_performance`
  normalization) so a USD account can be benchmarked against a peer set that includes other
  currencies.
- Cohort composition is transparent: the response states how many accounts formed the cohort and how
  many were excluded (missing metric, no FX) so a percentile isn't silently computed over 3 accounts.

## Behavior / interface (proposal — plan stage to finalize)

- `account_benchmark(account_id, date_from, date_to, cohort_ids=None, reporting_currency="USD")`
  → `{account:{...metrics}, cohort:{count, excluded, per_metric:{median, p25, p75}},
  percentiles:{metric: {value, percentile, direction, verdict}}}`.
- Pure percentile/quartile helper over metric rows (unit-testable).

## Edge cases & interactions

- Tiny cohort (< a documented minimum, e.g. 3–5) → percentiles are unreliable; return the raw
  comparison but flag "cohort too small for a meaningful percentile."
- Target account missing a metric → that metric's percentile is absent with a reason.
- Target account not in the cohort list → include it anyway (benchmark still valid) or error;
  plan stage to decide and document.
- Cost vs. quality metric direction must be correct — a test must assert a low CPL yields a
  "better than peers" verdict, not worse.
- Currency exclusions surfaced, not silent.

## Use cases

- Specialist: "how does my Reno mission's CPL compare to the rest?" → "72nd percentile (better than
  most)" instead of a bare $/lead.
- Supervisor: benchmark a struggling account against its peers to decide whether it's the account or
  the market.
