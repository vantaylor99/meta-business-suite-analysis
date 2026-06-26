description: Review of config/registry safety-knob additions for write tools — constants, per-account policy field, tests, and AGENTS.md guardrail.
prereq:
files: src/meta_ads_analysis/config.py, src/meta_ads_analysis/account_registry.py, config/meta_ads_accounts.json, tests/test_meta_ads_analysis.py, AGENTS.md
difficulty: easy
----
## What was built

### `config.py`
`MAX_BUDGET_DECREASE_PERCENT = 50.0` and `MIN_DAILY_BUDGET_CENTS = 100` were already present (landed via `cbo-aware-budget-write`). The `META_READER_BACKEND` default `"direct"` already lives canonically in `reader_provider.py` (`READER_BACKEND_ENV`/`reader_from_env`), so no duplicate constant was added to config.py — the env var is the primary switch per the ticket's coordination note.

### `account_registry.py`
- Added `max_budget_decrease_percent: float | None = None` field to `MetaAdsAccount` (dataclass slots field, defaults `None`).
- `load_account_registry` now reads `policy.get("max_budget_decrease_percent")` and coerces to `float` when present; `None` when absent (backward-compatible, no schema break).

### `config/meta_ads_accounts.json`
No changes — both `pollen_sense` and `divine_designs` have no non-default decrease percent, so the global `MAX_BUDGET_DECREASE_PERCENT` applies. Both accounts still load cleanly.

### Tests (3 new)
- `test_account_registry_max_budget_decrease_percent_override` — JSON with `max_budget_decrease_percent: 25` in `action_policy` → field is `25.0`.
- `test_account_registry_max_budget_decrease_percent_defaults_absent` — JSON with only `max_budget_increase_percent` → field is `None`.
- `test_account_registry_existing_config_loads` — the real `config/meta_ads_accounts.json` loads both accounts; both have `max_budget_decrease_percent is None`.

### AGENTS.md Guardrails
Added one bullet documenting `MAX_BUDGET_DECREASE_PERCENT`, `MIN_DAILY_BUDGET_CENTS`, the per-account override field, and the `validate_only` final check.

## Test results
`271 passed in 0.58s` — all green, no pre-existing failures.

## Use cases for validation

- **Default applies when field absent**: load any account JSON without `max_budget_decrease_percent` → `account.max_budget_decrease_percent is None`; the budget ticket uses `MAX_BUDGET_DECREASE_PERCENT` from config.
- **Per-account override readable**: add `"max_budget_decrease_percent": 25` to an account's `action_policy` → `account.max_budget_decrease_percent == 25.0`.
- **Existing config loads unchanged**: `load_account_registry(DEFAULT_ACCOUNTS_CONFIG_PATH)` returns both accounts without error.
- **Floor and cap are separate**: `MIN_DAILY_BUDGET_CENTS` is the absolute floor (minor units); `MAX_BUDGET_DECREASE_PERCENT` / `max_budget_decrease_percent` is the relative cap. Both are intended to apply together (enforcement owned by the budget ticket).

## Known gaps / reviewer notes

- **Wiring not done here**: `account.max_budget_decrease_percent` is parsed but not yet read by any budget-op code — the `cbo-aware-budget-write` ticket owns wiring (it reads `params["max_decrease_percent"]`; a follow-on step should fold the per-account field into that op-param, ideally in a separate fix ticket if not already in scope).
- **Currency disclaimer**: `MIN_DAILY_BUDGET_CENTS` is a conservative local floor in account minor units; the real per-currency minimum is enforced by `validate_only` against Meta (enforced by the budget ticket, not here).
- **Increase cap parity**: the percent increase cap remains op-param-driven (`params["max_increase_percent"]`, default 20) — this ticket deliberately does not touch that path (per the ticket note about the existing field not being wired).

## Review findings
