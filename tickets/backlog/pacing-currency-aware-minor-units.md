description: The tool that checks whether ad accounts are on track to spend their budgets divides money figures by 100 to turn "cents" into dollars, but a few currencies (like Japanese yen and Korean won) don't use cents at all — so for accounts billed in those currencies the budget and spend numbers come out 100 times too small.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Context

`pacing_report` (and its helper `_minor_to_major`) converts Meta's budget / spend-cap / amount-spent
fields — which Meta returns in the account currency's **minor unit** — into major units by dividing by
`100`. That is correct for the ~2-decimal currencies that dominate the fleet (USD, EUR, GBP, MXN, …),
but it is **wrong** for:

- **zero-decimal currencies** (JPY, KRW, CLP, VND, …) where the minor unit *is* the major unit — no
  divisor should apply, so the current code reports figures **100× too small**; and
- **three-decimal currencies** (BHD, KWD, TND, …) where the divisor should be `1000`, so figures come
  out **10× too large**.

Today this is a documented known limitation (see the `_minor_to_major` docstring and the README /
`docs/META_API_SETUP.md` pacing notes). It does **not** corrupt other accounts — the per-account math
is currency-isolated — but a JPY account's `period_budget` / `spend_cap` / `amount_spent` and its
`variance_pct` verdict are unreliable.

## What to build

A **currency-aware minor-unit exponent** so `_minor_to_major(value, currency)` divides by the correct
power of ten (`100` for 2-decimal, `1` for 0-decimal, `1000` for 3-decimal). The canonical exponents
are the ISO-4217 "minor unit" digits; Meta also documents its per-currency `offset` / currency
settings via the `/currencies` edge. Options to weigh at plan stage:

- a small committed static table of exponents keyed by currency code (mirrors the committed
  `config/fx_rates.json` posture — deterministic, no network), OR
- reading Meta's currency metadata once and caching it.

Note: insights `spend` is already in **major** units (untouched here). Only the budget/cap/amount-spent
minor-unit fields need the divisor. When the currency's exponent is unknown, fall back to `100` and
surface the assumption rather than silently guessing.

## Acceptance

- A JPY (0-decimal) and a 3-decimal fixture convert correctly; existing 2-decimal behavior is
  unchanged (byte-identical output for USD/EUR/… accounts).
- The pacing docstring / README / `docs/META_API_SETUP.md` "known limitation" caveat is removed or
  narrowed once fixed.
