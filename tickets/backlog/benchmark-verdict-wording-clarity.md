description: When comparing one ad account to its peers, the tool can describe an expensive account as "below the cohort median," which reads like it's cheap/good — but it actually means the account ranks worse than most peers. Make the plain-language verdict say clearly whether the account is better or worse than its peers.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: easy
----
## Problem

`account_benchmark`'s per-metric `verdict` string describes the account's **percentile position**
(where the tool's convention is "a HIGH percentile is always good"), not its dollar value — so for a
cost metric the wording can be read backwards.

Observed live 2026-07-11 (Washington Seattle Mission vs a 6-account cohort):

```
cost_per_result: value 66.42, direction lower_is_better,
                 percentile 25, rank 5 of 6, median 43.45,
                 verdict "below the cohort median"
```

$66.42 is **above** the dollar median ($43.45) — i.e. more expensive, worse — but "below the cohort
median" reads to a non-technical operator as "cheaper than median = good." The `percentile` (25) and
`direction` fields are correct and unambiguous; only the human-facing `verdict` string is
misreadable.

## What "done" looks like

- The `verdict` text is **direction-aware and unambiguous about better-vs-worse**, e.g. for a cost
  metric at the 25th percentile: "more expensive than most peers (5th of 6)" rather than "below the
  cohort median." A quality metric (CTR/ROAS) high percentile reads as "better than most peers."
- Keep the structured fields (`value`, `percentile`, `rank`, `median`, `p25`, `p75`, `direction`,
  `unreliable`) exactly as they are — this is a wording change to `verdict` only.
- Cost vs quality direction must be correct: a low cost-per-lead must never read as "worse."

## Edge cases & interactions
- Ties (shared rank), tiny/`unreliable` cohort (below `min_for_percentile`) — verdict should still
  read sensibly (or defer to the existing "too small" signal).
- A metric the account is missing (`roas` null) — no verdict, unchanged.
- Whatever wording is chosen must be covered by a test asserting a cheap account reads as "better"
  and an expensive one as "worse."
