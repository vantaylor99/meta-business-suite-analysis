description: Ask "which of my accounts are the best and worst on a given metric" and get a ranked shortlist, so someone overseeing many accounts doesn't have to eyeball every one.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: easy
----
## Problem

At 200 accounts you cannot read a flat table and find the outliers. You need "top/bottom N by
metric" directly. This is a thin selection/sort layer over the per-account metric rows produced by
`cross_account_performance` — small, but the single most-used entry point for anyone with more than
a handful of accounts.

## What it must deliver

- Rank the accounts in scope by a chosen metric (spend, CPC, CPM, CTR, CPL/CPA, ROAS, impressions,
  clicks, results), ascending or descending, returning the top/bottom N with the metric value and
  enough identity (account id, name) to act.
- Metric must be comparable across currencies where it's a money metric — reuse the normalized
  figures from `cross_account_performance` (rank money metrics in the reporting currency).
- Sensible handling of accounts that lack the ranked metric (e.g. no results → no CPL): grouped into
  an explicit "not ranked / metric unavailable" bucket rather than sorted as zero or infinity.

## Behavior / interface (proposal — plan stage to finalize)

- `rank_accounts(date_from, date_to, metric, account_ids=None, order="desc", limit=10,
  reporting_currency="USD")` → ordered list of `{account_id, name, currency, metric, value,
  value_normalized?}` plus an `unranked` list with reasons.
- Pure ranking helper over the metric rows (fully unit-testable without a reader).

## Edge cases & interactions

- Ties → stable, documented tiebreak (e.g. by account id) so output is deterministic.
- `limit` larger than scope → return all; `limit` <= 0 → validation error.
- Unknown/misspelled metric name → validation error listing valid metrics.
- Money metric requested without a resolvable reporting currency for some accounts → those land in
  `unranked` with a reason, ranking proceeds for the rest.
- Inherits partial-failure/error channel from the underlying performance read.

## Use cases

- WWFT: "bottom 10 accounts by ROAS this month" → the review shortlist.
- Supervisor: "top 5 accounts by spend among my department" → where the money is going.
- Specialist: "rank my 15 accounts by CPL" → which of mine to work on next.
