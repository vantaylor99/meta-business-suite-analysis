description: The budget-pacing tool used to always divide money by 100 (cents→dollars), which made zero-decimal currencies like Japanese yen and Korean won read 100× too small. The divisor is now chosen from the account's currency, so yen/won and the rare 3-decimal currencies convert correctly.
prereq:
files: src/meta_ads_analysis/currency.py, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, config/fx_rates.json, README.md, docs/META_API_SETUP.md
difficulty: medium
----

## What was built

Made `pacing_report`'s minor-unit→major-unit conversion **ISO-4217 currency-aware** instead of a
hard-coded `÷100`. A committed static exponent table in `currency.py` maps the ~30 non-2-decimal
ISO currencies to their exponent; everything else defaults to 2 (the ISO default). The divisor is now
`value / 10 ** minor_unit_exponent(currency)`.

### Phase 1 — `currency.py` (new public surface)
- `DEFAULT_MINOR_UNIT_EXPONENT = 2`.
- `CURRENCY_MINOR_UNIT_EXPONENTS: dict[str, int]` — the **exceptions only**:
  - 16 zero-decimal: BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF
  - 7 three-decimal: BHD IQD JOD KWD LYD OMR TND
  - **2 four-decimal: CLF, UYW** — see "Deviations" below (ticket design block only listed 0/3).
- `_KNOWN_TWO_DECIMAL_CURRENCIES` (module-private tuple) + `KNOWN_MINOR_UNIT_CURRENCIES: frozenset` =
  exceptions ∪ common 2-decimal codes. Superset of `config/fx_rates.json` (guarded by a test).
- `minor_unit_exponent(currency) -> int` (case-insensitive; blank/None/UNKNOWN → 2; never raises).
- `minor_unit_exponent_is_known(currency) -> bool`.

### Phase 2 — `account_discovery.py`
- `_minor_to_major(value, currency="USD")` and `summarize_account_budget(campaigns, adsets,
  currency="USD")` — both **default to USD** so bare calls (and the two unchanged direct-call unit
  tests) are byte-identical.
- `currency` threaded at the three `pacing_report` call sites (budget summary + spend_cap + amount_spent).
- **Assumption note**: distinct unrecognized currency codes collected into `assumed_currencies` and,
  when non-empty, appended to the existing `notes`/`result["note"]` join as
  `"assumed 2-decimal minor units for unrecognized currency codes: XYZ, ZZZ."` (deduped, sorted).

### Phase 3 — tests + docs
- New tests (all in `tests/test_meta_ads_analysis.py`):
  `test_minor_unit_exponent_lookup_and_known_predicate`,
  `test_fx_rates_codes_are_subset_of_known_minor_unit_currencies`,
  `test_minor_to_major_currency_aware_divisor`,
  `test_summarize_account_budget_currency_aware`,
  `test_pacing_report_jpy_zero_decimal_end_to_end`,
  `test_pacing_report_assumption_note_only_for_unrecognized_currency`.
- Caveat narrowed in the **five** doc locations (four from the ticket + one extra — see Deviations):
  `_minor_to_major` docstring, `pacing_report` `**Units.**` docstring, `README.md:92`,
  `docs/META_API_SETUP.md:352-354`, and `mcp_server.py` `DISCOVERY_TOOL_DESCRIPTIONS["pacing_report"]`.

## Validation done

`python -m pytest tests/test_meta_ads_analysis.py -q` → **598 passed** (was 592 before; +6 new tests).
Command streamed to `/tmp/pacing.log`. No mypy/ruff configured in this repo (pytest is the only gate).

Key assertions the reviewer can lean on:
- **JPY (0-decimal) end-to-end**: `active_daily_budget==30000`, `period_budget==930000`,
  `spend_cap==1500000`, `amount_spent==500000` (all the old buggy `÷100` values × 100), verdict
  `on_track`, `variance_pct≈0`. Requires a JPY-bearing `fx_table` (`_fx(JPY=0.0067)`) so the account is
  normalizable, not routed to no-FX errors.
- **3-decimal**: `_minor_to_major("30000","KWD")==30.0`; `summarize_account_budget(..., "KWD")` ÷1000.
- **2-decimal unchanged**: the pre-existing all-USD end-to-end + tiebreak + no-FX tests still pass
  untouched — the default-`"USD"` params are the regression guard.
- **Assumption note**: fires (deduped, sorted `"XTS, ZZZ"`) for FX-injected unrecognized codes; does
  NOT fire for an all-USD scope; USD never appears in the note.

## Where to be adversarial (honest gaps)

1. **`_KNOWN_TWO_DECIMAL_CURRENCIES` is curated, not exhaustive.** The *correctness*-critical part
   (the exceptions table) is complete per ISO-4217, so the **divisor is always right**. But a
   legitimate 2-decimal currency NOT in the known set (some obscure code) would trigger a *spurious*
   assumption note. That is cosmetic (the number is still correct at ÷100), not a wrong figure — but
   worth a skeptical look at whether the known-set is broad enough for the real fleet. Reachable-fleet
   currency distribution is USD-dominant per project memory, so exposure is low.
2. **Assumption note only fires on the budget-read-OK path.** A `budget_unread` account with an
   unrecognized currency is NOT added to `assumed_currencies` (no divisor was applied to it). Defensible
   — nothing was converted — but a reviewer might argue the currency is still "in scope, assumed". Easy
   to change if wanted.
3. **`10 ** exponent` is int; `num` is float** → division stays float for every exponent (0/2/3/4),
   incl. `num / 1` for JPY. Verified via tests, but a reviewer may want to eyeball
   `_minor_to_major("0","JPY")==0.0` (the uncapped `spend_cap<=0→None` guard survives).
4. **Tests are mock-only** (consistent with the rest of the suite) — no live Meta call exercises a real
   JPY account. If a real yen account is reachable, a one-off manual `pacing_report` spot-check against
   Meta's UI budget figures would be the strongest confirmation; out of scope for this ticket.

## Deviations from the ticket (call out for reviewer sign-off)

- **Added 4-decimal currencies (CLF, UYW).** The ticket's design block showed only exponent 0 and 3,
  but told the implementer to "confirm against a current ISO-4217 reference and include the full set."
  CLF/UYW are the two real ISO-4217 4-decimal codes. They are non-transactional units of account
  unlikely to ever be a Meta ad-account currency, but including them costs nothing and completes the
  table. Remove if the reviewer prefers strict scope.
- **Fifth doc location updated: `mcp_server.py:479`.** The ticket named four doc spots; a grep for the
  stale "known limitation" caveat surfaced a fifth — the LLM-facing `pacing_report` tool description in
  `DISCOVERY_TOOL_DESCRIPTIONS`. Left un-narrowed it would tell the model the (now-fixed) bug still
  exists, so it was updated too. No test asserts that description string.

## Acceptance checklist (from the source ticket)

- [x] JPY (0-decimal) + KWD/BHD (3-decimal) convert correctly through `_minor_to_major` and
      `summarize_account_budget`; JPY end-to-end `pacing_report` magnitudes correct.
- [x] Existing 2-decimal behavior byte-identical (all prior USD/EUR/AUD pacing tests + the two
      direct-call tests unedited and green).
- [x] "known 100× limitation" caveat removed/narrowed (5 locations).
- [x] `python -m pytest tests/test_meta_ads_analysis.py -q` passes (598).
