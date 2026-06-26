description: Add the small set of tunable safety numbers and per-account policy knobs the new write tools rely on (budget-decrease floor and cap, reader-backend choice), so they live in one config place instead of being hard-coded.
prereq:
files: src/meta_ads_analysis/config.py, src/meta_ads_analysis/account_registry.py, config/meta_ads_accounts.json, tests/test_meta_ads_analysis.py, AGENTS.md
difficulty: easy
----
## Why

The write tickets reference a handful of new tunables. Centralize them in `config.py` /
`account_registry.py` / `config/meta_ads_accounts.json` so they are not scattered as magic numbers,
and so per-account policy (which already drives pause/scale gates via `action_policy`) can govern the
new budget-decrease and reader-backend behavior. This ticket is independent of the read/write
feature tickets (it only adds constants/fields they will read) and can land early; the feature
tickets treat these as landing.

## IMPORTANT — verified fact about the existing increase cap (do not repeat the plan's error)

The ops budget-increase cap is currently read from the **per-op param** `params["max_increase_percent"]`
in `control._build_request` (default 20) — it is **NOT** read from `config.py` and **NOT** from the
account registry. The `max_budget_increase_percent` field present in `config/meta_ads_accounts.json`
is NOT wired into the ops path today. So the new decrease cap is NOT a drop-in mirror of an existing
wired cap — `cbo-aware-budget-write` is responsible for actually WIRING the decrease cap (and may also
choose to wire the registry increase field). This ticket only PROVIDES the constants/fields; it makes
no claim that they are read until the budget ticket wires them.

## What to build

### `config.py` constants (next to the existing floors — do NOT introduce competing numbers)

- `MIN_DAILY_BUDGET_CENTS` — conservative floor a budget decrease may not cross (wired by
  `cbo-aware-budget-write`).
- `MAX_BUDGET_DECREASE_PERCENT` — default symmetric cap for decreases (wired by the budget ticket;
  no existing config-driven increase cap to mirror — see the note above).
- A read-backend default name (`META_READER_BACKEND` default `"direct"`) if a config home is
  preferable to env-only; coordinate with `community-mcp-read-server` (env var is the primary
  switch — config is just the documented default).

Reuse existing floors (`MIN_WASTE_SPEND` 100.0, `MIN_SCALING_SPEND` 75.0,
`CONFIDENCE_CONVERSIONS_FLOOR` 25, `REVIEW_MIN_WINDOW_DAYS` 7) for grounding — do not add parallel
spend/sample floors.

### `account_registry.py` / `config/meta_ads_accounts.json`

- Allow `action_policy` to carry an optional `max_budget_decrease_percent` (per-account override of
  the global cap), parsed by `MetaAdsAccount` like the existing `max_budget_increase_percent` field is
  parsed. Add it to the two existing accounts' policies only if a non-default is desired; otherwise
  leave the global default to apply.
- No schema break: new fields are optional with sane defaults; existing configs must still load.

## TODO

- Add `MIN_DAILY_BUDGET_CENTS`, `MAX_BUDGET_DECREASE_PERCENT` (and the reader-backend default) to
  `config.py`.
- Parse optional `max_budget_decrease_percent` in `MetaAdsAccount` / `load_account_registry`.
- Confirm `config/meta_ads_accounts.json` still loads (both `pollen_sense` and `divine_designs`);
  add the new optional field only where a non-default is intended.
- Tests (mock-only): registry loads with and without the new field; defaults apply when absent;
  per-account override is read.
- Add a one-line mention of the new safety knobs in AGENTS.md Guardrails (full catalog in the docs
  ticket).
- `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Backward-compatible parse** — existing account JSON has no `max_budget_decrease_percent`;
  `load_account_registry` must default it, not raise. Test the existing file loads unchanged.
- **Floor vs cap interaction** — a decrease must satisfy BOTH the percent cap (not too big a swing)
  AND the absolute `MIN_DAILY_BUDGET_CENTS` floor (not too low). The budget ticket enforces both;
  this ticket just provides the numbers — document both are intended to apply together.
- **No false parity claim** — do not document or assume the new decrease cap mirrors a wired increase
  cap; the increase cap is op-param-driven today (see the note above). The budget ticket owns wiring.
- **Currency** — `MIN_DAILY_BUDGET_CENTS` is in account minor units and accounts may differ in
  currency; keep the constant conservative and note that `validate_only` against Meta is the final
  per-currency floor check (the budget ticket owns enforcement).