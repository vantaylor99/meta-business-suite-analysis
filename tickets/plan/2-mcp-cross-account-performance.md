description: A cross-account report that shows efficiency, not just raw totals — cost per click, cost per lead, click-through rate, etc. — and lets you compare accounts that bill in different currencies by converting to one reporting currency.
prereq: mcp-cross-account-batched-fanout
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/normalize.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

`cross_account_spend_summary` returns raw additive totals (spend, impressions, clicks) grouped by
currency. To actually judge accounts against each other you need **efficiency metrics** (CPM, CPC,
CTR, cost-per-result / CPL / CPA, and ROAS where revenue is tracked) computed correctly, and a way
to **compare across currencies** — today multi-currency accounts can't be lined up at all because
nothing is normalized. This is the successor read that the ranking, triage, and benchmark tools
build on.

## What it must deliver

- **Derived per-account metrics** computed from the additive base metrics — never summed directly
  (a ratio like CPC must be recomputed from summed spend ÷ summed clicks, not averaged across
  accounts). Include at least: CPM, CPC, CTR, cost-per-result (lead/conversion) where result/action
  fields are available, and ROAS where purchase/value actions exist.
- **Result/conversion sourcing**: pull result counts from Meta's action/conversion fields (the same
  fields the per-account insights path already understands) so "cost per lead" reflects the account's
  optimization event, not a raw click. Where an account has no trackable result, the result-based
  metrics are absent for that account (not zero-filled into a misleading ratio).
- **Currency normalization to a chosen reporting currency** (default USD): a
  `reporting_currency` parameter. Output keeps the native-currency figure **and** the normalized
  figure side by side; per-currency subtotals still exist, and a normalized grand total becomes
  meaningful only in the reporting currency. **FX source is DECIDED: a static rate table checked
  into `config/`** (no external/network calls — keeps unattended and mock runs deterministic). The
  table maps currency → rate-to-USD (or to the reporting currency) and MUST carry an explicit
  `as_of` date; the tool surfaces that `as_of` in its output and treats the rates as approximate so
  no consumer mistakes them for live rates. A currency absent from the table → normalized fields
  absent for that account + recorded in `errors` (native figures still returned). Never silently sum
  unlike currencies without conversion. (Live/Meta-provided FX was explicitly deferred by the
  product owner — do not build it here.)
- Rides the batched fan-out engine and `resolve_scope` seam from
  `mcp-cross-account-batched-fanout`; same partial-failure and determinism guarantees.

## Behavior / interface (proposal — plan stage to finalize)

- New tool `cross_account_performance(date_from, date_to, account_ids=None,
  reporting_currency="USD", level="account")` returning per-account rows with both native and
  normalized money fields + derived metrics, per-currency subtotals, and a normalized total.
- Derived-metric computation lives in a small pure helper (testable without a reader);
  normalization helper likely in `normalize.py`.

## Edge cases & interactions

- Zero clicks / zero impressions / zero results → guard divide-by-zero; emit metric as absent, not
  `inf`/`NaN`.
- Ratio metrics must be recomputed from summed components, never averaged (classic Simpson's-paradox
  trap when rolling accounts together).
- Account whose currency has no FX rate available → normalized fields absent + recorded in `errors`;
  native figures still returned.
- Missing/!partial action data → result metrics absent for that account only.
- Determinism and per-account partial failure inherited from the fan-out engine.
- Mock path stays live-call-free and deterministic, including FX (use a fixed test rate table).

## Use cases

- Supervisor: "show CPL and CTR for all my specialists' accounts last 30 days, in USD" — even
  though some bill in MXN/BRL/EUR.
- Specialist: same view over their own accounts to see which is drifting on cost-per-result.
- Provides the per-account metric rows consumed by `mcp-rank-accounts`,
  `mcp-flag-accounts-attention`, and `mcp-account-benchmark`.
