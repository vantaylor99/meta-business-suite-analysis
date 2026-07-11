description: Surface how many accounts in each aggregate block actually contributed results and revenue, so consumers can tell whether a portfolio ROAS is based on 1 of 10 accounts or all 10.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: easy
----

## What was done

Added `results_accounts` and `purchase_value_accounts` to every aggregate block emitted by `cross_account_performance`:
- `totals_by_currency[currency]` — via `_finalize_subtotal` (account_discovery.py ~line 779)
- `normalized_total` — via `_finalize_normalized_total` (~line 808)

Both keys read from pre-existing accumulator fields (`results_contrib` / `pv_contrib`) that were already being tracked but discarded before emit. No logic changes — purely additive.

## Changes

**`_finalize_subtotal`** — after `out["account_count"] = acc["account_count"]`:
```python
out["results_accounts"] = acc["results_contrib"]
out["purchase_value_accounts"] = acc["pv_contrib"]
```

**`_finalize_normalized_total`** — after `out["excluded_no_fx"] = norm["excluded_no_fx"]`:
```python
out["results_accounts"] = norm["results_contrib"]
out["purchase_value_accounts"] = norm["pv_contrib"]
```

## Tests added (5 new, all passing)

- `test_coverage_counts_partial_results` — 3 USD accounts, 1 contributes results+revenue → `results_accounts==1`, `purchase_value_accounts==1`; also tests results-only case → `purchase_value_accounts==0`
- `test_coverage_counts_all_accounts_contribute` — 2 USD both contributing → keys equal `account_count==2`
- `test_coverage_counts_zero_contributions` — 2 USD neither contributing → both keys `==0`, `results`/`purchase_value` absent from output (existing behavior confirmed)
- `test_coverage_counts_multi_currency_independent` — 2 USD (1 with results) + 1 EUR (with results) → USD subtotal `results_accounts==1`, EUR `results_accounts==1`, `normalized_total["results_accounts"]==2`
- `test_coverage_counts_no_fx_excluded_from_normalized_total` — 1 USD (results) + 1 JPY (results, no FX rate) → `totals_by_currency["JPY"]["results_accounts"]==1`, `normalized_total["results_accounts"]==1` (JPY excluded from norm)

## Test run

```
.venv/bin/pytest tests/test_meta_ads_analysis.py -x -q -k "coverage_counts or partial_contributor"
6 passed in 1.52s

.venv/bin/pytest tests/test_meta_ads_analysis.py -x -q -k "cross_account_performance"
16 passed in 0.49s
```

## Known gaps / reviewer notes

- Keys are always emitted including `0`; zero is meaningful (no contributor, so derived metrics absent). Reviewer should confirm this is the desired behavior for empty-fleet edge cases (`account_count==0`).
- No changes to per-account row shape — coverage is aggregate-level only.
- The multi-currency normalized total counts each FX-eligible contributor independently (not a sum of per-currency `results_accounts`), which matches the existing accumulation pattern. Reviewer should verify this semantics is clear from key names or consider a note in the output.
