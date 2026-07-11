description: Verify the fix that makes the Seattle Mission lead account report its lead count and cost-per-lead correctly, so it can be graded on its cost-per-lead goal.
prereq:
files: config/meta_ads_accounts.json, config/meta_ads_accounts.example.json, src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, knowledge/accounts/seattle_mission/profile.md
difficulty: medium
----

## What shipped

`seattle_mission` (`act_103014553`) is a Meta Instant-Forms lead account. Its config named the wrong
internal lead action type (`leadgen_grouped`), so `Results`/`cost_per_result` came back blank in our
durable grade producers and the account couldn't be graded on cost-per-lead. Fixed the config key to
the live-verified `onsite_conversion.lead_grouped` and made lead resolution robust against a
key-family so future drift self-heals.

### Changes

- **`config/meta_ads_accounts.json` (gitignored — will NOT appear in the diff, but is what runs)** and
  **`config/meta_ads_accounts.example.json` (tracked template, `example_lead_gen` entry)**:
  `primary_result_action_type` `leadgen_grouped` → `onsite_conversion.lead_grouped`; `analysis_notes`
  rewritten (VERIFY instruction removed, resolution recorded). `primary_result_label` (`"Leads (form)"`),
  `roas_role` (`not_applicable`), and the $10/$40 `action_policy` thresholds are unchanged.
- **`src/meta_ads_analysis/sync_api.py`**:
  - New module constant `LEAD_KEYS` (canonical-first) + `_LEAD_KEYS_LOWER` frozenset.
  - New helper `_find_metric_key` (returns the matched key name; mirrors `_find_metric`'s
    first-match-by-blob-order semantics).
  - `_infer_primary_result_action`: `LEAD_KEYS` added as the **final** candidate group (after purchase,
    install) — safety net only for an unconfigured lead account; never flips a purchase/install account.
  - `_label_for_action`: added a `"lead"` → `"Leads"` branch.
  - `_build_performance_row`: when the resolved primary key is a lead key, resolve
    `results`/`cost_per_result` against the whole `LEAD_KEYS` family; if the configured key itself
    didn't match but a family member did, append a warning naming both keys. Non-lead keys: behavior
    byte-for-byte unchanged (single configured/inferred key).
- **`src/meta_ads_analysis/account_discovery.py`**: imported `LEAD_KEYS`/`_LEAD_KEYS_LOWER`; in
  `cross_account_performance`, when the resolved `result_key` is a lead key, resolve `results_value`
  against the family (silent — no warnings channel). Never falls back across goal types.
- **`knowledge/accounts/seattle_mission/profile.md`**: added a "Lead metric resolution" section —
  canonical action type, durable path resolves correctly, interactive connector `ads_get_ad_entities`
  returns `Results`="Not available" (Meta limitation, use `lead`/`cost_per_lead` there), and the
  $10 target / $40 pause grade rule with "never ROAS".

## How it was validated (this is a floor, not a ceiling)

- Full suite: **626 passed** (`.venv/bin/python -m pytest tests/ -q`). 8 new tests added.
- Live-number spot check reproduced the ticket's 7/1–7/5 table exactly through `_build_performance_row`
  (e.g. 7/1: 12 leads / $436.81 → CPR `36.4`, label `Leads (form)`, ROAS blank) with no warnings.
- Registry load confirms `seattle_mission` now resolves `onsite_conversion.lead_grouped`, VERIFY text
  gone, label/roas_role intact; example template also loads with the new key.

### New tests (all in `tests/test_meta_ads_analysis.py`, after `test_cross_account_performance_uses_configured_result_key`)
- `test_build_performance_row_resolves_lead_family_for_seattle_mission` — 12 leads / $436.81 → `36.4`,
  label wins from config, no ROAS; asserts single value (not summed to 24) when Meta returns both
  `onsite_conversion.lead_grouped` and `lead` at equal value.
- `test_build_performance_row_lead_family_self_heals_stale_config_key` — stale `leadgen_grouped` config
  still resolves via family + warning names both keys.
- `test_build_performance_row_zero_lead_window_leaves_results_absent` — spend, no leads → Results/CPR
  absent (not 0), no warning.
- `test_build_performance_row_unconfigured_purchase_and_lead_infers_purchase` — inference precedence.
- `test_build_performance_row_purchase_and_subscribe_accounts_unchanged` — divine_designs / pollen_sense
  regression, incl. lead-action noise in the divine blob not leaking into Results.
- `test_infer_primary_result_action_lead_precedence_and_fallback` — purchase+lead → purchase; lead-only
  → lead key.
- `test_cross_account_performance_resolves_lead_family` — cross-account parity: 12 leads, CPR = spend/12,
  label `Leads (form)`, no ROAS.
- `test_cross_account_performance_lead_family_self_heals_stale_config_key` — cross-account silent
  self-heal on a stale key.

## Reviewer focus / known gaps to probe

- **The live config change is gitignored and invisible in the diff.** Confirm the tracked
  `meta_ads_accounts.example.json` edit is present and that the runtime `config/meta_ads_accounts.json`
  (local only) holds `onsite_conversion.lead_grouped` — a fresh clone gets the example, not the live file.
- **No end-to-end test against a real/mock Meta read for this specific account.** All lead-family tests
  drive `_build_performance_row` / `cross_account_performance` with synthetic blobs shaped like the
  live read. A live/mock MCP smoke of `sync-api` for `seattle_mission` was not run in-ticket (needs a
  token/connector) — worth a manual pass if a reviewer has access.
- **Interactive connector path is documentation-only.** `ads_get_ad_entities` returning "Not available"
  for `results`/`cost_per_result` is a Meta/connector limitation, not fixed in code — verify the
  profile.md guidance reads clearly for an operator grading via that tool.
- **Warning-string format** is asserted only by substring (`leadgen_grouped` + `onsite_conversion.lead_grouped`
  both present). If a stricter operator-facing message is desired, tighten it.
- **`_find_metric` is blob-order first-match, not key-order.** For the live data both lead keys are
  equal (12==12) so order is moot, but if Meta ever returns differing values for `lead` vs
  `onsite_conversion.lead_grouped`, the resolved value depends on blob order. Called out in code
  comments; confirm this is acceptable (canonical-first key ordering is documented intent but not
  enforced by `_find_metric`).
- Import ordering in `account_discovery.py` places `_LEAD_KEYS_LOWER` non-alphabetically; harmless, no
  linter configured in the repo.
