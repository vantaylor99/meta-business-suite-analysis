description: When comparing many ad accounts, the portfolio-wide ROAS and cost-per-result numbers can be quietly based on only a few accounts that actually track revenue, and nothing in the output warns you of that — so the headline efficiency figure can look better or worse than reality without any signal that coverage is partial.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md
difficulty: easy
----

## Context

`cross_account_performance` (added in ticket `mcp-cross-account-performance`) emits aggregate
`roas` and `cost_per_result` on both `normalized_total` and each `totals_by_currency` group. These
aggregates sum **spend/impressions/clicks over every FX-eligible account** but sum
**results/purchase_value over only the accounts that reported them** (contributor-tracked internally
via `results_contrib` / `pv_contrib` in the accumulators).

That is a deliberate and reasonable portfolio-aggregation semantic — but the *output* carries no
signal of partial coverage. A consumer reading `normalized_total.roas` cannot tell whether it
reflects 1 of 10 accounts' revenue or all 10. This matters because the project is emphatic about not
overstating ROAS trustworthiness (see `AGENTS.md` → Interpretation Rules: "If results exist but
purchase value is missing, explicitly say the account is showing outcomes without reliable
revenue…"). A downstream agent relaying a headline portfolio ROAS could materially mislead.

The internal contributor counts already exist in the accumulators (`_finalize_subtotal` /
`_finalize_normalized_total` in `account_discovery.py`); they are simply dropped before emit.

## Desired behavior

Surface enough coverage information on the aggregate blocks (`normalized_total` and each
`totals_by_currency` entry) that a consumer can caveat a portfolio ROAS / cost-per-result — e.g. the
count (or fraction) of accounts in the group that actually contributed `results` and
`purchase_value`, versus the group's `account_count`. The exact shape (contributor counts vs a
coverage ratio vs a boolean "partial" flag) is open; keep it additive so existing keys/tests are
untouched.

Not in scope: changing how the aggregate ratios are computed — the sum-based semantic is correct and
Simpson's-paradox-safe. This is purely about making partial revenue/result coverage visible.
