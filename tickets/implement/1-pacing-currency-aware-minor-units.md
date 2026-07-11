description: The budget-pacing tool always divides money figures by 100 to turn cents into dollars, but currencies like Japanese yen and Korean won have no cents — so those accounts' budget and spend numbers come out 100 times too small. Make the divisor depend on the account's currency.
prereq:
files: src/meta_ads_analysis/currency.py, src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, config/fx_rates.json, README.md, docs/META_API_SETUP.md
difficulty: medium
----

## Problem

`_minor_to_major(value)` (`src/meta_ads_analysis/account_discovery.py:1707`) converts Meta's
budget / spend-cap / amount-spent fields — returned in the account currency's **minor unit** — into
major units by always dividing by `100`. That is correct for 2-decimal currencies (USD, EUR, GBP,
MXN, BRL, CAD, AUD — the whole current FX table), but:

- **zero-decimal** currencies (JPY, KRW, CLP, VND, …): the minor unit *is* the major unit, so
  dividing by 100 reports figures **100× too small**;
- **three-decimal** currencies (BHD, KWD, TND, …): the divisor should be `1000`, so figures come out
  **10× too large**.

Call sites that must become currency-aware (all inside `pacing_report`'s per-account loop, where the
account `currency` is already in hand as `row.get("currency")`):

- `summarize_account_budget(campaigns, adsets)` — `account_discovery.py:1720`, four `_minor_to_major`
  calls on campaign/adset `daily_budget` / `lifetime_budget`.
- `pacing_report` — `spend_cap` (`:1984`) and `amount_spent` (`:1987`).

Insights `spend` is already in **major** units and is untouched (it flows through
`cross_account_performance` / `_number`, not `_minor_to_major`).

## Design decision (resolved — build this, do not re-open)

**A committed static exponent table of ISO-4217 exceptions, as a Python constant in `currency.py`.**

Chosen over reading Meta's `/currencies` edge because minor-unit digits are a stable, public,
finite ISO fact (they do not drift like FX rates), and a Python constant needs no network, no file
I/O, and no cache — keeping mock/unattended runs deterministic. It is the exponent analogue of the
existing `ACCOUNT_STATUS_LABELS` dict. (The FX *rates* live in `config/fx_rates.json` because they
change over time; exponents do not, so they do not need a JSON file.)

Store only the **exceptions** (non-2-decimal currencies); everything else defaults to exponent `2`
(the ISO default and the correct answer for the ~150 two-decimal currencies).

```python
# currency.py
DEFAULT_MINOR_UNIT_EXPONENT = 2

# ISO-4217 minor-unit exceptions: currency code -> exponent. Everything NOT listed is 2-decimal.
CURRENCY_MINOR_UNIT_EXPONENTS: dict[str, int] = {
    # zero-decimal (minor unit == major unit; divisor 1)
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # three-decimal (divisor 1000)
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}

def minor_unit_exponent(currency: str) -> int:
    """ISO-4217 minor-unit exponent for a currency code; DEFAULT_MINOR_UNIT_EXPONENT (2) for
    unmapped/blank codes."""
```

> The above exception list is a strong starting set (the standard ISO-4217 non-2 currencies). The
> implementer should confirm it against a current ISO-4217 reference and include the full set — the
> table's value is being complete, so a stray missing entry silently reintroduces the bug for that
> currency. If unsure of an entry, prefer the ISO reference over guessing.

Then in `account_discovery.py`:

```python
from .currency import ..., minor_unit_exponent   # add to the existing currency import

def _minor_to_major(value: Any, currency: str = "USD") -> float | None:
    num = _number(value)
    if num is None:
        return None
    return num / (10 ** minor_unit_exponent(currency))
```

**Backward-compatible defaults.** `currency` defaults to `"USD"` (exponent 2) on both
`_minor_to_major` and `summarize_account_budget(campaigns, adsets, currency="USD")`. This keeps every
existing 2-decimal call and the direct-call unit tests
(`test_minor_to_major_cents_and_blank`, `test_summarize_account_budget_cbo_dedup_precedence`)
**byte-identical** without edits, while `pacing_report` threads the real per-account currency through.

Thread `currency` at the three pacing call sites:
- `summarize_account_budget(campaigns, adsets, currency)` and inside it pass `currency` to each of the
  four `_minor_to_major` calls;
- `_minor_to_major(account.get("spend_cap"), currency)` and
  `_minor_to_major(account.get("amount_spent"), currency)` in `pacing_report`.

`currency` in `pacing_report` is already `row.get("currency") or "UNKNOWN"` at `:1942` — an
`"UNKNOWN"` code falls through `minor_unit_exponent` to exponent 2, i.e. today's behavior.

## Surface the assumption (do not silently guess)

Because only the *exceptions* are stored, an unrecognized code (including `"UNKNOWN"` when Meta omits
currency) silently defaults to exponent 2. To avoid an unsurfaced wrong guess, add a small
explicit-membership check and note it in the report rather than inventing a heavy channel:

- Add `KNOWN_MINOR_UNIT_CURRENCIES: frozenset[str]` in `currency.py` = the exception-table keys **plus**
  an explicit tuple of the common 2-decimal codes we recognize. It MUST be a superset of every code in
  `config/fx_rates.json` (USD/EUR/GBP/BRL/MXN/CAD/AUD) so that no FX-supported account is ever treated
  as an assumption. Add a `minor_unit_exponent_is_known(currency) -> bool` predicate.
- In `pacing_report`, collect the distinct account currencies whose exponent was *assumed*
  (`not minor_unit_exponent_is_known(currency)`) and, when non-empty, append a sentence to the existing
  `notes` list (e.g. `"assumed 2-decimal minor units for unrecognized currency codes: XYZ, UNKNOWN"`).
  This reuses the existing `notes`/`result["note"]` join at `:2050-2059` — no new top-level key.

Note: a no-FX account (currency absent from the FX table) is *already* surfaced in `errors` by the
inherited `cross_account_performance` path, so the assumption note is the only new surfacing needed.

## Docs to narrow (remove the "known 100× limitation" caveat, do not just delete blindly)

Once the fix lands, update these three so they describe the new currency-aware behavior instead of a
known bug:

- `account_discovery.py:1707` `_minor_to_major` docstring (the "KNOWN 100x inaccuracy" paragraph).
- `pacing_report` docstring `**Units.**` paragraph (`:1872-1876`).
- `README.md:92` and `docs/META_API_SETUP.md:352-353` (the "zero-decimal currencies … known 100×
  limitation" sentences) — narrow to "minor-unit divisor is ISO-4217 currency-aware (2/0/3 decimal);
  an unrecognized currency code assumes 2 decimals and is surfaced in the report note."

## Edge cases & interactions

- **2-decimal unchanged (regression guard).** A USD and an EUR account must produce byte-identical
  `pacing_report` output vs. before this change (assert an existing fixture's numbers are unchanged).
  The default-`"USD"` params guarantee the two direct-call unit tests need no edits.
- **0-decimal (JPY) conversion.** `_minor_to_major("30000", "JPY") == 30000.0` (not `300.0`);
  `summarize_account_budget` with a JPY daily budget of `"1000"` yields `active_daily == 1000.0`.
- **3-decimal (BHD/KWD) conversion.** `_minor_to_major("30000", "KWD") == 30.0` (÷1000).
- **Case-insensitivity.** `minor_unit_exponent("jpy")` == `minor_unit_exponent("JPY")` == 0 (upper-case
  on lookup, mirroring `FxTable`).
- **Blank / None / "UNKNOWN" currency.** Falls through to exponent 2 (today's behavior); no raise.
- **spend_cap zero/absent.** The existing `spend_cap <= 0 -> None` uncapped rule (`:1985`) must still
  hold after the divisor change (a 0-decimal `spend_cap` of "0" is still 0).
- **`variance_pct` FX-invariance preserved.** `variance_pct` is `(projected - period_budget)/period_budget`
  — both native, same currency — so a correct per-account exponent leaves the *ratio* unchanged for a
  2-decimal account and merely corrects the *magnitude* for a 0/3-decimal account. Add a JPY end-to-end
  pacing test asserting the corrected `period_budget`/`spend_cap`/`amount_spent` magnitudes AND a sane
  `variance_pct` verdict.
- **fx_rates.json ⊆ known set (guard test).** Assert every currency code in `config/fx_rates.json` is in
  `KNOWN_MINOR_UNIT_CURRENCIES` (so an FX-supported account is never flagged as an assumption). A JPY
  account can be exercised in pacing only by first adding a `JPY` rate to the FX table fixture used by
  the test (inject via the `fx_table` seam or a temp table) — the end-to-end pacing test must supply a
  JPY-bearing `fx_table` so the JPY account is normalizable, not no-FX.
- **Assumption note fires only when warranted.** An all-USD/EUR scope must NOT emit the assumption note;
  a scope with an unrecognized code (e.g. a currency in the FX table but deliberately absent from the
  known set — or an `"UNKNOWN"`) must list exactly that code once (deduped, deterministic order).

## Acceptance

- JPY (0-decimal) and a 3-decimal (BHD/KWD) fixture convert correctly through `_minor_to_major` and
  `summarize_account_budget`; a JPY end-to-end `pacing_report` reports correct budget/cap/amount_spent
  magnitudes.
- Existing 2-decimal behavior byte-identical (USD/EUR fixtures + the two unchanged direct-call tests).
- The "known 100× limitation" caveat is removed/narrowed in the four doc locations above.
- `python -m pytest tests/test_meta_ads_analysis.py -q` passes (stream with `2>&1 | tee`).

## TODO

### Phase 1 — exponent table + helpers (currency.py)
- Add `DEFAULT_MINOR_UNIT_EXPONENT`, `CURRENCY_MINOR_UNIT_EXPONENTS` (complete ISO-4217 non-2 set),
  `KNOWN_MINOR_UNIT_CURRENCIES` (exceptions ∪ common 2-decimal, superset of fx_rates.json), and
  `minor_unit_exponent()` / `minor_unit_exponent_is_known()`.
- Unit tests: 0/2/3-decimal lookups, case-insensitivity, blank/unknown → 2, known-predicate, and the
  fx_rates.json ⊆ known-set guard.

### Phase 2 — thread currency through the divisor (account_discovery.py)
- Add `currency` param (default `"USD"`) to `_minor_to_major` and `summarize_account_budget`; import
  `minor_unit_exponent`.
- Thread `currency` at the three `pacing_report` call sites (budget summary, spend_cap, amount_spent).
- Add the assumed-currency note to the existing `notes` list.

### Phase 3 — tests + docs
- Add JPY (0-decimal) + BHD/KWD (3-decimal) coverage: direct `_minor_to_major`, `summarize_account_budget`,
  and a JPY end-to-end `pacing_report` (with a JPY-bearing `fx_table`); assert a USD/EUR fixture is
  unchanged and the assumption-note fires only when warranted.
- Narrow the caveat in `_minor_to_major` docstring, `pacing_report` `**Units.**` docstring,
  `README.md:92`, and `docs/META_API_SETUP.md:352-353`.
- Run `python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/pacing.log`.
