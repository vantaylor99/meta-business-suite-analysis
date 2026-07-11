description: The single-account snapshot printout always divides an ad set's daily budget by 100 and prints it with a dollar sign, so for accounts billed in a non-cent currency (Japanese yen, Korean won) or any non-USD currency the displayed daily budget is wrong or mislabeled.
files: src/meta_ads_analysis/cli.py, src/meta_ads_analysis/currency.py
difficulty: easy
----

## Context

The multi-account `pacing_report` path was made ISO-4217 currency-aware (see the completed
`pacing-currency-aware-minor-units` ticket): its minor-unit→major-unit divisor is now chosen from the
account currency via `minor_unit_exponent(currency)` in `src/meta_ads_analysis/currency.py`.

That fix did **not** touch the separate single-account snapshot display in the CLI. At
`src/meta_ads_analysis/cli.py:833` the ad-set daily budget is still rendered as:

```python
budget = f" ${int(a['daily_budget'])/100:.0f}/day" if a.get("daily_budget") else ""
```

Two currency-correctness problems here, both pre-existing:

- the `/100` is unconditional, so a zero-decimal currency (JPY, KRW) reads 100× too small and a
  three-decimal currency (BHD, KWD) reads 10× too large — the same bug class the pacing ticket fixed
  for the other pipeline; and
- the `$` sign is hard-coded, so any non-USD account is mislabeled regardless of the divisor.

## Priority / scope

**Low priority.** This is a human-facing display line in the single-account snapshot command, not a
figure the analysis tools compute against, and the reachable fleet is USD-dominant (per project
memory, currency-exactness is a deprioritized concern). Filed so the divergence between the two
pipelines is not lost, not because it is urgent.

## What a fix would look like

Thread the account currency into this display and reuse `minor_unit_exponent` for the divisor (and,
ideally, the currency code instead of a hard-coded `$`). Confirm where `a['daily_budget']` originates
in `build_account_snapshot` — verify it is still raw minor units at this point (not already converted)
before changing the divisor, so the fix does not double-convert. Add/adjust a CLI snapshot test if one
exists for this output.
