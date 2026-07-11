description: Surface how many accounts in each aggregate block actually contributed results and revenue, so consumers can tell whether a portfolio ROAS is based on 1 of 10 accounts or all 10.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: easy
----

## What shipped

`cross_account_performance` now emits `results_accounts` and `purchase_value_accounts` on every
aggregate block — each `totals_by_currency[currency]` subtotal (via `_finalize_subtotal`) and
`normalized_total` (via `_finalize_normalized_total`). They report how many of the block's
`account_count` accounts contributed `results` / `purchase_value` to the sum. Purely additive; both
keys read pre-existing accumulator fields (`results_contrib` / `pv_contrib`) that were already tracked
per account but discarded before emit.

## Review findings

**Diff reviewed:** commit `7506a47` (implement stage) — 2 production lines in each finalize helper +
5 new tests. Read the full accumulation path (per-account loop at `account_discovery.py:606-731`, the
finalize helpers at `:756-814`), the docstring, `docs/META_API_SETUP.md`, the MCP tool description,
and all downstream consumers.

**Correctness — verified sound.** `results_contrib` / `pv_contrib` are incremented at most once per
account inside the single per-account loop (`:677`, `:680`, `:719`, `:722`), so the new counts can
never exceed `account_count`. The main loop iterates once per resolved account (`for ad_account_id,
payload, error in results`), never per insight row, so no double-counting. FX-excluded accounts
increment their native currency's `results_contrib` but not `norm`'s — the implementer's
`test_coverage_counts_no_fx_excluded_from_normalized_total` pins this and it holds.

**No downstream breakage.** Searched every consumer of `totals_by_currency` / `normalized_total`
(`mcp_server.py`, `account_benchmark`, `pacing_report`, `flag_accounts_needing_attention`). None do
exact-dict-equality on aggregate blocks or enumerate their keys; `account_benchmark` post-processes
per-account rows (unchanged), not the aggregate blocks. Additive keys are safe.

**Test coverage — adequate, one gap closed.** The 5 implementer tests cover partial / full / zero /
multi-currency / FX-excluded. The implementer flagged the empty-fleet edge case (`account_count==0`)
as unverified — that gap was real: the existing `test_cross_account_performance_empty_reach_note` did
not assert the new keys. **Fixed in this pass** (minor): added
`results_accounts==0 and purchase_value_accounts==0` assertions to that test, confirming the
present-but-empty `normalized_total` carries zeroed coverage counts.

**Docs — brought current (minor).** The docstring, `docs/META_API_SETUP.md`, and the MCP tool
description all describe the output prose-style and never enumerated aggregate keys, so none were
strictly stale. Added a paragraph to the `cross_account_performance` docstring documenting the two new
coverage keys and their `0`-is-meaningful semantics, so future consumers discover them without reading
the finalize helpers. `META_API_SETUP.md` and the MCP description remain behaviorally accurate as-is.

**Semantics note (accepted, not a defect).** `results_value == 0.0` (a real reported zero) counts as a
contribution — `results_contrib` increments and `results` is emitted as `0`. This is pre-existing
gating behavior (`:675`), unchanged by this ticket; the new key merely surfaces the existing count.
The multi-currency `normalized_total` counts each FX-eligible contributor independently rather than
summing per-currency `results_accounts` — matches the existing accumulation pattern and is now
documented in the docstring.

**Major findings:** none. No new tickets filed.

## Validation

```
.venv/bin/pytest tests/test_meta_ads_analysis.py -q -k "cross_account_performance or coverage_counts"
21 passed, 571 deselected

.venv/bin/pytest -q
592 passed in 1.69s
```

No linter (ruff/flake8/mypy) is configured in `pyproject.toml`; validation is the pytest suite.
