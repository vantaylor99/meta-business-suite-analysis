description: Added the tunable safety numbers and per-account policy knob the new budget-write tools rely on, so the limits live in config instead of being scattered as magic numbers.
prereq:
files: src/meta_ads_analysis/config.py, src/meta_ads_analysis/account_registry.py, src/meta_ads_analysis/control.py, config/meta_ads_accounts.json, tests/test_meta_ads_analysis.py, AGENTS.md
difficulty: easy
----
## What shipped

- `config.py` — `MAX_BUDGET_DECREASE_PERCENT = 50.0` and `MIN_DAILY_BUDGET_CENTS = 100` (both landed via `cbo-aware-budget-write`, present and correct). No duplicate `META_READER_BACKEND` constant: the default `"direct"` lives canonically in `reader_provider.py` (`READER_BACKEND_ENV` / `reader_from_env`, verified at lines 588–619), env var is the primary switch — consistent with the ticket's coordination note.
- `account_registry.py` — `MetaAdsAccount` gained `max_budget_decrease_percent: float | None = None`; `load_account_registry` parses `policy.get("max_budget_decrease_percent")`, coercing to `float` when present, `None` when absent (backward-compatible, uses `is not None` so a legitimate `0` is preserved).
- `config/meta_ads_accounts.json` — unchanged; neither account sets a non-default decrease percent, so the global default applies. Both still load.
- Tests — 3 added by implement (override → `25.0`; absent → `None`; real config loads with both fields `None`), plus one assertion added in review (see below).
- `AGENTS.md` — one Guardrails bullet documenting both knobs, the per-account override, and the `validate_only` final check.

## Review findings

**Checked:** implement diff read first with fresh eyes; correctness of the parse; backward-compat (`is not None` vs truthiness, so `0` survives); whether the per-account override is actually honored at apply time; consistency of the parse path against the wiring in `control.py`; documentation accuracy in `AGENTS.md`; test coverage across present/absent/zero/no-policy cases; full suite + lint.

- **Override IS live (handoff understated it).** The implement handoff flagged "wiring not done here." In fact `cbo-aware-budget-write` already wired it: `build_budget_plan` (`control.py:1516-1517`) folds `policy.get("max_budget_decrease_percent")` into the `max_decrease_percent` op-param, and `_resolve_decrease_cap` / `_capped_budget_request` enforce it alongside `MIN_DAILY_BUDGET_CENTS`. So the per-account decrease override is functional end-to-end through the `action_policy` dict. No action needed — flagging that the gap note is stale, not wrong.
- **The new dataclass field is currently unread (minor, accepted).** `account.max_budget_decrease_percent` is parsed and stored but no production code reads it — the live path reads the same JSON key off the `action_policy` dict (`policy.get(...)`), not the typed accessor. The field is a harmless forward-looking convenience accessor; it cannot drift from the wired value because both read the identical JSON key. Not worth a refactor and not in this ticket's wiring scope. Left as-is, documented here.
- **Two parse paths for one key (minor, accepted).** Registry uses strict `float(raw)` (raises on a non-numeric string); `control._num` is lenient (returns `None` on garbage). Both read the same key from the same dict, so they cannot disagree on valid input; on invalid input the registry fails fast at load (acceptable, surfaces a bad config early). No change.
- **Coverage gap fixed inline (minor).** All three implement tests had `action_policy` present, leaving the "no `action_policy` block at all" branch (`policy = {}` interacting with the new parse) unasserted. Added `assert account.max_budget_decrease_percent is None` to `test_account_registry_resolves_valid_slug`, which constructs an account with no policy. Float-valued override and `0`-value paths are covered by the existing int-override test (proves `float()` coercion) and the `is not None` guard respectively — judged not worth additional cases.
- **Docs verified, not assumed.** `validate_only` exists and is threaded through `control.py:270-276`; reader-backend default confirmed in `reader_provider.py`. The AGENTS.md bullet matches reality.
- **Security / resource cleanup / error handling:** nothing applicable — pure config parsing, no I/O beyond the existing `read_text`, no new external surface.

## Test results

`271 passed in 0.39s` — full suite green. No lint tooling (`ruff`/`mypy`) is installed in `.venv`; pytest is the project's quality gate. No pre-existing failures.

## End
