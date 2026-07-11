description: The Seattle Mission lead account reads blank for "Results" and "cost per lead" because our config names the wrong internal lead action type, so the account can't be graded on its cost-per-lead goal. Fix the action type and make lead resolution robust.
prereq:
files: config/meta_ads_accounts.json, src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, knowledge/accounts/seattle_mission/profile.md
difficulty: medium
----

## Problem (root cause confirmed by a live read)

`seattle_mission` (`act_103014553`) is a Meta Instant-Forms lead account (objective
`OUTCOME_LEADS`). Its config sets:

```json
"primary_result_action_type": "leadgen_grouped"
```

A live read on 2026-07-07 (re-confirmed 2026-07-10 via `ads_get_ad_entities`,
`time_range 2026-07-01..2026-07-05`, `time_increment: 1`) shows the account's leads are reported by
Meta under action type **`onsite_conversion.lead_grouped`**, NOT `leadgen_grouped`:

| Date | Spend | `lead` = `onsite_conversion_lead_grouped` | `cost_per_action_type:onsite_conversion.lead_grouped` | `results` | `cost_per_result` |
| --- | --- | --- | --- | --- | --- |
| 7/1 | $436.81 | 12 | $36.40 | Not available | Not available |
| 7/2 | $574.25 | 8 | $71.78 | Not available | Not available |
| 7/3 | $422.09 | 3 | $140.70 | Not available | Not available |
| 7/4 | $369.04 | 12 | $30.75 | Not available | Not available |
| 7/5 | $554.25 | 12 | $46.19 | Not available | Not available |

**Two independent facts to internalize:**

1. **Our own pipeline (the durable grade producer) — fixable in code.** Both
   `sync_api._build_performance_row` (`src/meta_ads_analysis/sync_api.py:292`) and
   `account_discovery.cross_account_performance` (via `_resolve_result_key`,
   `src/meta_ads_analysis/account_discovery.py:505`) resolve `results`/`cost_per_result` from the
   **raw Graph `actions` blob** using the account's `primary_result_action_type`, falling back to
   `_infer_primary_result_action` (`sync_api.py:431`). Because the configured key `leadgen_grouped`
   never matches the real `onsite_conversion.lead_grouped` entry, `_find_metric` returns `None` →
   `results`/`cost_per_result` come back blank → the account can't be graded. **Correcting the config
   key + resolving leads against a key family fixes both call sites at once** (they share the sync_api
   helpers). `cross_account_performance` computes `cost_per_result = spend/results`
   (`compute_derived_metrics`, `account_discovery.py:474`); `sync_api` pulls it from the
   `cost_per_action_type` blob — both only need `results` (the lead count) to resolve.

2. **The external `meta-ads` connector's `ads_get_ad_entities` — NOT fixable in our code.** For this
   account Meta returns `results`/`cost_per_result` as the literal string `"Not available"` (it does
   not map an objective "result" abstraction here), while `lead` / `cost_per_lead` /
   `onsite_conversion_lead_grouped` populate correctly. This is a Meta/connector limitation, not our
   bug — the fix for the interactive daily-overview path is **documentation** (tell the reader to use
   the lead fields), handled in the knowledge update below.

## Design (resolved — no open questions)

**Canonical lead action type = `onsite_conversion.lead_grouped`** (verified live). Correct the config
and make lead resolution robust so a future key drift self-heals within the lead family (and never
mis-attributes across goal types).

### 1. Config — `config/meta_ads_accounts.json`, `seattle_mission`

- Change `measurement_focus.primary_result_action_type` from `"leadgen_grouped"` to
  `"onsite_conversion.lead_grouped"`.
- Update `measurement_focus.analysis_notes` to record the verified action type (drop the
  "VERIFY … leadgen_grouped vs onsite_conversion.lead_grouped" instruction now that it is resolved;
  state it resolved to `onsite_conversion.lead_grouped` as of 2026-07-10).
- Leave `primary_result_label` = `"Leads (form)"`, `roas_role` = `"not_applicable"`, and the
  `action_policy` thresholds ($10 target / $40 pause) unchanged.

### 2. `src/meta_ads_analysis/sync_api.py` — lead-key family + inference + label

- Add a module-level `LEAD_KEYS` constant next to `PURCHASE_KEYS` / `APP_INSTALL_KEYS` (~line 83),
  ordered canonical-first:
  ```python
  LEAD_KEYS = [
      "onsite_conversion.lead_grouped",
      "leadgen_grouped",
      "leadgen.other",
      "onsite_conversion.lead",
      "lead",
  ]
  ```
- `_infer_primary_result_action` (`sync_api.py:431`): add `LEAD_KEYS` as the **final** candidate group
  (after `PURCHASE_KEYS`, `APP_INSTALL_KEYS`). Rationale: config is authoritative for
  seattle_mission, so this only matters as a safety net for an unconfigured lead account; placing it
  last avoids ever inferring incidental leads over a purchase/install account's true goal.
- `_label_for_action` (`sync_api.py:444`): add a lead branch — if `"lead"` in the lowered action type,
  return `"Leads"` (config's `primary_result_label` = `"Leads (form)"` still wins upstream because it
  is applied before this fallback).
- **Lead-family self-heal in `_build_performance_row`:** after computing `primary_result_key`, when
  that key is in `LEAD_KEYS`, resolve `results`/`cost_per_result` against the **whole `LEAD_KEYS`
  family** (order-preserving) rather than the single configured string — i.e. use
  `_find_metric(actions, LEAD_KEYS)` / `_find_metric(cost_per_action_type, LEAD_KEYS)` for lead
  accounts. If the configured lead key itself did not match but a family member did, append a warning
  naming both the configured key and the matched key (so the operator can tidy config). For
  non-lead keys, behavior is byte-for-byte unchanged (still the single configured/inferred key).

### 3. `src/meta_ads_analysis/account_discovery.py` — mirror the lead-family resolution

- `_resolve_result_key` / the `results_value` computation (`account_discovery.py:634-635`): when the
  resolved `result_key` is in `LEAD_KEYS`, resolve `results_value = _find_metric(actions, LEAD_KEYS)`
  (family match), so `cross_account_performance` populates `results`/`cost_per_result`/`result_label`
  for lead accounts exactly as sync does. Import `LEAD_KEYS` from `sync_api` alongside the existing
  `PURCHASE_KEYS` import (`account_discovery.py:51`). There is no warnings channel here — populating
  via the family match is strictly better than leaving it blank; keep it silent.
- Do **not** self-heal across goal types (never fall back to inference when a non-lead configured key
  fails to match — a genuine zero-result window must stay a real zero/absent, not be back-filled from
  an unrelated action type).

### 4. Knowledge — `knowledge/accounts/seattle_mission/profile.md`

- Record the resolved action type (`onsite_conversion.lead_grouped`) and that our
  `sync-api` / `cross_account_performance` path now resolves `results`/`cost_per_result` = cost per
  lead from the raw actions blob.
- Add an explicit note for the **interactive daily-overview path**: the `meta-ads` connector's
  `ads_get_ad_entities` returns `results`/`cost_per_result` = "Not available" for this account (Meta
  limitation, unfixable here) — when grading via that tool, request `lead` / `cost_per_lead` (or
  `onsite_conversion_lead_grouped`) instead and compute cost-per-lead from those. Grade against the
  config thresholds: 🎯 $10 target `cost_per_result`, 🛑 $40 `pause_cost_per_result_above`; never
  apply ROAS (`roas_role: not_applicable`).

## What "done" looks like

- `sync-api` / `cross_account_performance` populate `results` (= lead count) and `cost_per_result`
  (= cost per lead) for `seattle_mission` from the raw actions blob (verified by unit tests using the
  live numbers above, e.g. 12 leads / $436.81 → $36.40 on 7/1).
- Config names the correct, verified action type; the "VERIFY" instruction is gone.
- The knowledge profile tells a future reader how to grade this account on both the durable
  (sync/cross-account) and interactive (connector) paths.
- Full test suite passes; no ROAS is ever attributed to this account.

## Edge cases & interactions

- **Non-lead accounts unaffected.** The lead-family resolution and inference addition must not change
  results/cost_per_result for `divine_designs` (`purchase`) or `pollen_sense`
  (`app_custom_event.fb_mobile_subscribe`). Add/keep a regression assertion.
- **Genuine zero-lead window.** A day with spend but 0 leads (e.g. 6/27–6/28 in the decision log) must
  yield `results = 0`/absent and cost_per_result absent — never a fabricated number. `_find_metric`
  returning `None` across the whole family must not be coerced to 0.
- **Key-family ordering / double counting.** `_find_metric` returns the first matching key's value;
  ensure it does not sum `lead` + `onsite_conversion.lead_grouped` if Meta ever returns both (the
  live read shows them equal — 12 == 12 — so first-match is correct, but assert single-value, not a
  sum).
- **cross_account vs sync parity.** Both paths must report the same lead count for the same actions
  blob; a lead account graded by `cross_account_performance` and by `sync-api` should not disagree.
- **`_infer_primary_result_action` precedence.** Adding `LEAD_KEYS` last must not flip an
  unconfigured account that has both purchases and leads to a lead label — cover with a test that a
  blob containing `purchase` + `onsite_conversion.lead_grouped` and no config still infers
  `purchase`.
- **Self-heal warning path (sync only).** When config still held a stale lead key that no longer
  matches but a family member does, the warning must fire and name both keys; when the configured key
  matches directly, no warning.
- **Label precedence.** `primary_result_label` from config (`"Leads (form)"`) must win over the
  `_label_for_action` fallback (`"Leads"`).

## TODO

### Phase 1 — code + config
- [ ] Correct `primary_result_action_type` + `analysis_notes` for `seattle_mission` in
      `config/meta_ads_accounts.json`.
- [ ] Add `LEAD_KEYS`, wire into `_infer_primary_result_action` (last group) and `_label_for_action`
      in `sync_api.py`.
- [ ] Add lead-family resolution + stale-key warning in `_build_performance_row`.
- [ ] Import `LEAD_KEYS` and add lead-family resolution in
      `account_discovery.py` (`_resolve_result_key` / `cross_account_performance`).

### Phase 2 — tests (`tests/test_meta_ads_analysis.py`)
- [ ] Unit test `_build_performance_row` for a seattle_mission-shaped row: actions blob with
      `onsite_conversion.lead_grouped=12` + `cost_per_action_type` → `Results=12`,
      `Cost per result=$36.40`, `Result type="Leads (form)"`.
- [ ] Add a `cross_account_performance` lead-account test (pattern:
      `test_cross_account_performance_uses_configured_result_key`, line ~10518) asserting
      `results`/`result_label`/`cost_per_result` populate for a lead account.
- [ ] Stale-key self-heal test: config key `leadgen_grouped`, actions blob only has
      `onsite_conversion.lead_grouped` → results still resolve (family match) + warning fires.
- [ ] Inference-precedence test: unconfigured blob with `purchase` + `onsite_conversion.lead_grouped`
      still infers `purchase` (no lead label).
- [ ] Zero-lead-window test: spend but no lead action → results/cost_per_result absent, no ROAS.
- [ ] Regression: `divine_designs` / `pollen_sense` result resolution unchanged.

### Phase 3 — knowledge + validation
- [ ] Update `knowledge/accounts/seattle_mission/profile.md` per §4 (durable path resolved +
      interactive connector guidance + $10/$40 grade rule, no ROAS).
- [ ] Run the test suite (`pytest`, streamed with `tee`) and confirm green before handing to review.
