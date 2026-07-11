description: Surface how many accounts in each aggregate block actually contributed results and revenue, so consumers can tell whether a portfolio ROAS is based on 1 of 10 accounts or all 10.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: easy
----

## What to build

Add two keys — `results_accounts` and `purchase_value_accounts` — to every aggregate block emitted by `cross_account_performance`: each `totals_by_currency[currency]` dict and `normalized_total`. These are the counts of accounts within that group that actually contributed `results` / `purchase_value` to the sum, versus `account_count` which is the total accounts in the group. This is purely additive; no existing keys are touched or renamed.

### Output shape (before → after)

```python
# totals_by_currency["USD"] — BEFORE
{"spend": 600.0, "impressions": 6000, "clicks": 300,
 "results": 10, "purchase_value": 500.0,
 "account_count": 3, "cost_per_result": 60.0, "roas": 0.833}

# totals_by_currency["USD"] — AFTER (two new keys added)
{"spend": 600.0, "impressions": 6000, "clicks": 300,
 "results": 10, "purchase_value": 500.0,
 "account_count": 3, "cost_per_result": 60.0, "roas": 0.833,
 "results_accounts": 1,          # ← new: 1 of 3 contributed results
 "purchase_value_accounts": 1}   # ← new: 1 of 3 contributed purchase_value

# normalized_total — AFTER (same two keys)
{"reporting_currency": "USD", "spend": 600.0, ...,
 "account_count": 3, "excluded_no_fx": 0,
 "results_accounts": 1,
 "purchase_value_accounts": 1,
 "cost_per_result": 60.0, "roas": 0.833}
```

When `results_accounts == 0`, `results` is absent from the dict (existing behavior) and the new key tells you why ROAS / cost_per_result are absent — no one contributed. When coverage is full (`results_accounts == account_count`), the aggregate ratios are trustworthy. Partial coverage (the main case) is now legible.

`results_accounts` and `purchase_value_accounts` are always emitted (including `0`) — zero is meaningful.

## Where the data already lives

The counts are tracked today but discarded before emit:

- **`_finalize_subtotal`** (`account_discovery.py` ~line 756): uses `acc["results_contrib"]` and `acc["pv_contrib"]` to gate whether `results` / `purchase_value` appear in `out`, then drops those counts.
- **`_finalize_normalized_total`** (~line 784): same pattern with `norm["results_contrib"]` / `norm["pv_contrib"]`.

## Implementation

Two micro-changes to the finalize helpers. No logic changes; the existing accumulation is correct.

### `_finalize_subtotal`

After `out["account_count"] = acc["account_count"]` (before the `out.update(compute_derived_metrics(base))` call), add:

```python
out["results_accounts"] = acc["results_contrib"]
out["purchase_value_accounts"] = acc["pv_contrib"]
```

### `_finalize_normalized_total`

After `out["excluded_no_fx"] = norm["excluded_no_fx"]` (before the `out.update(compute_derived_metrics(base))` call), add:

```python
out["results_accounts"] = norm["results_contrib"]
out["purchase_value_accounts"] = norm["pv_contrib"]
```

That's the entire production change.

## Tests to add

Add to `tests/test_meta_ads_analysis.py`, near the existing `test_cross_account_performance_partial_contributor_normalized_total` test (line 10171):

- **`test_coverage_counts_partial_results`**: 3 USD accounts, only 1 has purchase action + revenue. Assert `totals_by_currency["USD"]["results_accounts"] == 1`, `["purchase_value_accounts"] == 1`, `["account_count"] == 3`. Same on `normalized_total`. Verify `purchase_value_accounts == 0` when only results contributed (no `action_values`).

- **`test_coverage_counts_all_accounts_contribute`**: 2 USD accounts, both have results + purchase_value. Assert `results_accounts == account_count == 2` and `purchase_value_accounts == 2` in both `totals_by_currency["USD"]` and `normalized_total`.

- **`test_coverage_counts_zero_contributions`**: 2 USD accounts, neither has actions. Assert `results_accounts == 0` and `purchase_value_accounts == 0` in both blocks, and `results` / `purchase_value` keys absent (existing behavior confirmed).

- **`test_coverage_counts_multi_currency_independent`**: 2 USD accounts (1 has results, 1 does not) + 1 EUR account (has results). Assert USD block has `results_accounts == 1` and `account_count == 2`; EUR block has `results_accounts == 1` and `account_count == 1`. Assert `normalized_total["results_accounts"] == 2` (both currency contributors counted independently).

- **`test_coverage_counts_no_fx_excluded_from_normalized_total`**: 1 USD account (has results) + 1 JPY account (has results, but JPY absent from test FX table). Assert `normalized_total["results_accounts"] == 1` (JPY account excluded from `norm` accumulators, so it never contributed). Assert `totals_by_currency["JPY"]["results_accounts"] == 1` (JPY's native subtotal correctly counts it).

## Edge cases & interactions

- **Zero coverage**: `results_accounts == 0` even when `results` key is absent from the output. The absent key signals "no results computed"; the new count confirms "because no account contributed." Both must hold simultaneously.
- **FX-excluded accounts**: An account with no FX rate but WITH results still increments its currency group's `results_contrib` (in `_finalize_subtotal`) but NOT `norm["results_contrib"]` (in `_finalize_normalized_total`), because the `table.has(currency)` gate in `cross_account_performance` blocks it from the normalized accumulator. The new test `test_coverage_counts_no_fx_excluded_from_normalized_total` pins this.
- **Full coverage**: `results_accounts == account_count` — confirm both keys equal the total, not just one.
- **Multi-currency**: each currency group's coverage counts are independent of other currencies. `normalized_total` counts across the union of all FX-eligible contributors (not a sum of per-currency `results_accounts`).
- **Per-account row shape**: unchanged — no new keys on individual account rows, only on the aggregate blocks.
- **Empty reach**: `normalized_total` already emits `account_count == 0` and `excluded_no_fx == 0` for empty reach; the new keys should read `results_accounts == 0` and `purchase_value_accounts == 0`, consistent.

## TODO

- Edit `_finalize_subtotal` to emit `results_accounts` and `purchase_value_accounts`
- Edit `_finalize_normalized_total` to emit `results_accounts` and `purchase_value_accounts`
- Add the five new tests described above
- Run `pytest tests/test_meta_ads_analysis.py -x -q` and confirm all pass (no existing test uses exact-dict equality on `totals_by_currency` or `normalized_total` in the performance function)
