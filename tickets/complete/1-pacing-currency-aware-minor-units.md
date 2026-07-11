description: The budget-pacing tool used to always divide money by 100 (cents→dollars), which made zero-decimal currencies like Japanese yen and Korean won read 100× too small. The divisor is now chosen from the account's currency, so yen/won and the rare 3-decimal currencies convert correctly.
files: src/meta_ads_analysis/currency.py, src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, config/fx_rates.json, README.md, docs/META_API_SETUP.md
difficulty: medium
----

## What shipped

`pacing_report`'s minor-unit→major-unit conversion is now ISO-4217 **currency-aware** instead of a
hard-coded `÷100`. A committed static exponent table in `currency.py` maps the non-2-decimal ISO
currencies to their exponent; everything else defaults to 2. The divisor is
`value / 10 ** minor_unit_exponent(currency)`, with the per-account `currency` threaded from the perf
row into `summarize_account_budget` and the three `_minor_to_major` call sites (budget summary +
`spend_cap` + `amount_spent`). Unrecognized codes fall through to 2-decimal and are surfaced in the
report `note` (`assumed 2-decimal minor units for unrecognized currency codes: …`) rather than
silently guessed. See the implement commit `218e4b0` for the full diff.

## Review findings

**Stage: review — adversarial pass over commit `218e4b0`. Verdict: sound; accepted as-is. One
out-of-scope sibling issue filed to backlog; no inline changes needed.**

### Correctness of the fix — checked, PASS
- **Exponent table vs ISO-4217.** Verified the exceptions are complete and correct: 16 zero-decimal
  (BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF), 7 three-decimal
  (BHD IQD JOD KWD LYD OMR TND), 2 four-decimal (CLF UYW). No stray/missing entries. The 4-decimal
  additions (a deviation from the plan's 0/3-only sketch, but the plan told the implementer to
  "confirm against a current ISO-4217 reference and include the full set") are correct and harmless —
  accepted.
- **Divisor arithmetic & types.** `10 ** exponent` is int, `_number()` yields float, so `float / int`
  stays float for every exponent incl. `num / 1` (JPY). Probed directly: `_minor_to_major("30000","JPY")`
  → `30000.0` (float), `("30000","KWD")` → `30.0` (float), `("0","JPY")` → `0.0` (the `spend_cap<=0→None`
  uncapped guard survives), negatives pass through. PASS.
- **Currency source is consistent.** `currency = row.get("currency") or "UNKNOWN"` comes from the perf
  (spend) row — the same source used for FX normalization — not from the budget-config read, so the
  divisor and the normalization agree per account. Good.
- **Case-insensitivity / blank / None / UNKNOWN** → default 2, never raises. Verified via
  `minor_unit_exponent` and `minor_unit_exponent_is_known` probes.

### Regression safety — checked, PASS
- Both new public functions default `currency="USD"`, so every pre-existing bare call
  (`_minor_to_major`, `summarize_account_budget`) and the two unedited direct-call unit tests are
  byte-identical. The pre-existing all-USD end-to-end / tiebreak / no-FX pacing tests still pass
  untouched — they are the regression guard.
- **FX-subset guard.** `test_fx_rates_codes_are_subset_of_known_minor_unit_currencies` asserts every
  `config/fx_rates.json` code (USD EUR GBP BRL MXN CAD AUD) is in `KNOWN_MINOR_UNIT_CURRENCIES`, so an
  FX-normalizable account is never spuriously flagged "assumed". Confirmed the invariant holds today.

### Tests — checked, PASS (`python -m pytest tests/test_meta_ads_analysis.py -q` → **598 passed**, 1.75s)
- Happy path (USD unchanged), the bug case (JPY 0-decimal end-to-end: `active_daily_budget==30000`,
  `period_budget==930000`, `spend_cap==1500000`, `amount_spent==500000`, verdict `on_track`), 3-decimal
  (KWD/BHD ÷1000), edge (blank/None/UNKNOWN→2, `spend_cap "0"` still 0), and the assumption-note path
  (fires deduped+sorted for unrecognized FX-injected codes, does NOT fire for all-USD, USD never listed)
  are all covered. Coverage is adequate; I did not add tests. The 4-decimal path is exercised only via
  `minor_unit_exponent` (not `_minor_to_major`) — acceptable, since the divisor logic is uniform
  `10 ** exp` and CLF/UYW are non-transactional units that will never be a Meta ad-account currency.

### Docs — checked, PASS
- Grepped for the stale "known 100× limitation" caveat across `src/`, `docs/`, `README.md`. All five
  live locations are narrowed to the new currency-aware reality: `_minor_to_major` docstring,
  `pacing_report` `**Units.**` docstring, `README.md`, `docs/META_API_SETUP.md`, and the LLM-facing
  `mcp_server.py DISCOVERY_TOOL_DESCRIPTIONS["pacing_report"]`. No stale caveat remains in any file the
  change touches or should have touched. The implementer's "fifth doc location" (mcp_server) was the
  right call — an un-narrowed tool description would tell the model a fixed bug still exists.

### Accepted honest gaps (not defects)
- **`_KNOWN_TWO_DECIMAL_CURRENCIES` is curated, not exhaustive.** A legitimate but unlisted 2-decimal
  code (e.g. MRU, MGA — ISO base-5 minor-unit oddballs; probed, both resolve to exponent 2 = correct
  divisor) would trigger a *spurious* assumption note. This is cosmetic — the number is still right at
  ÷100. The *correctness-critical* half (the non-2-decimal exceptions) is complete, so the divisor is
  always right. Exposure is low (fleet is USD-dominant per project memory). Accepted.
- **Assumption note only collected on the budget-read-OK path.** A `budget_unread` account with an
  unrecognized currency is not added to `assumed_currencies`. Defensible — no divisor was applied to it.
  Accepted.

### Out-of-scope sibling issue → filed to backlog
- **`src/meta_ads_analysis/cli.py:833`** still does an unconditional `int(a['daily_budget'])/100` for
  the single-account snapshot's `$…/day` display (hardcoded `$`, USD-oriented, a different pipeline from
  `pacing_report`). It is **pre-existing**, not introduced by this change, and out of this ticket's
  scope. Filed `tickets/backlog/cli-snapshot-currency-aware-budget-display.md` (low priority, consistent
  with the currency-precision-low-priority steer) so it is not lost. Not fixed here.

## Acceptance checklist (from the source ticket) — all met
- [x] JPY (0-decimal) + KWD/BHD (3-decimal) convert correctly; JPY end-to-end magnitudes correct.
- [x] Existing 2-decimal behavior byte-identical (prior USD pacing + direct-call tests green, unedited).
- [x] "known 100× limitation" caveat removed/narrowed (5 locations).
- [x] `python -m pytest tests/test_meta_ads_analysis.py -q` passes (598).

## Validation re-run in review
`source .venv/bin/activate && python -m pytest tests/test_meta_ads_analysis.py -q` → **598 passed** in
1.75s (streamed to `/tmp/pacing-review.log`). No ruff/mypy configured in this repo; pytest is the only
gate.
