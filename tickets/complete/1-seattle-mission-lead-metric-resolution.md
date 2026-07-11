description: The Seattle Mission lead account now reports its lead count and cost-per-lead correctly, so it can be graded on its cost-per-lead goal. Reviewed and shipped.
prereq:
files: config/meta_ads_accounts.json, config/meta_ads_accounts.example.json, src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, knowledge/accounts/seattle_mission/profile.md, docs/ACCOUNT_SETUP.md
difficulty: medium
----

## Summary

`seattle_mission` (`act_103014553`) is a Meta Instant-Forms lead account whose config named the wrong
internal lead action type (`leadgen_grouped`), leaving `Results`/`cost_per_result` blank in the durable
grade producers. The implement pass fixed the config key to the live-verified
`onsite_conversion.lead_grouped` and made lead resolution robust against a whole key-family so future
drift self-heals (with an operator warning on the sync path). Review confirmed the implementation is
correct, extended test coverage for the one untested new branch, and fixed a doc that still advertised
the stale key. Shipped.

## Review findings

### What was checked
- **Read the implement diff first** (`git show 8a84dfd`) with fresh eyes before the handoff: config
  example, `sync_api.py` (`LEAD_KEYS`, `_LEAD_KEYS_LOWER`, `_find_metric_key`,
  `_infer_primary_result_action`, `_label_for_action`, `_build_performance_row`),
  `account_discovery.cross_account_performance`, and all 8 new tests.
- **Gitignored runtime state** (the handoff's #1 reviewer-focus item): verified the *live*
  `config/meta_ads_accounts.json` holds `onsite_conversion.lead_grouped` (VERIFY text gone, label +
  `roas_role` intact) and the tracked `meta_ads_accounts.example.json` template also carries the
  canonical key. `knowledge/accounts/seattle_mission/profile.md` is likewise gitignored (durable
  memory) — read it in full; its "Lead metric resolution" section is accurate and complete.
- **Semantics / correctness angles:** first-match-by-blob-order (`_find_metric`), no-sum guarantee,
  never-cross-goal-types, purchase/install regression, inference precedence (LEAD_KEYS last),
  absent-vs-fabricated-0, self-heal warning guard (`results is not None and not configured_matched`),
  `_find_metric_key` can't return None inside the warning branch (identical scan to the value lookup).
- **Docs:** grepped the repo for lingering `leadgen_grouped`; read every file the change touches and
  the ones it should have (`docs/ACCOUNT_SETUP.md`, `knowledge/accounts/seattle_mission/decision-log.md`).
- **Tests + build:** full suite green.

### Findings & disposition
- **MINOR — fixed inline.** `docs/ACCOUNT_SETUP.md` still used `leadgen_grouped` as the example
  `primary_result_action_type` for lead accounts — i.e. it advertised the exact stale key this ticket
  proved wrong, inviting the same bug on the next lead-account setup. Updated the example to
  `onsite_conversion.lead_grouped` and noted the family self-heal behavior.
- **MINOR — fixed inline.** The new `_label_for_action` `"lead" → "Leads"` branch had no direct
  assertion (every lead test supplied a config `primary_result_label`, so the fallback never fired).
  Added `test_build_performance_row_unconfigured_lead_only_labels_leads`: an unconfigured lead-only
  account exercises inference (LEAD_KEYS group) → lead key AND the label fallback end-to-end. Suite is
  now **627 passed**.
- **ACCEPTED (no action) — `_find_metric` is blob-order first-match, not key-order.** If Meta ever
  returned differing values for `lead` vs `onsite_conversion.lead_grouped`, the resolved value would
  depend on blob order rather than canonical-first key order. This is consistent with every other
  metric family (`PURCHASE_KEYS`, `APP_INSTALL_KEYS`, …) — not a regression — and the live data has
  both keys equal (12==12), so it is moot today. Documented in code comments; acceptable.
- **ACCEPTED (no action) — non-lead resolution byte-for-byte unchanged.** Confirmed by reading the
  `else` branch (identical to prior single-key logic) and by the divine_designs / pollen_sense
  regression test, including lead-action noise in the blob not leaking into `Results`.
- **ACCEPTED (no action) — import ordering of `_LEAD_KEYS_LOWER` in `account_discovery.py` is
  non-alphabetical.** Harmless; no linter is configured in the repo.

### Known gaps carried forward (not blocking)
- **No live/mock MCP end-to-end read for this specific account.** All lead-family tests drive
  `_build_performance_row` / `cross_account_performance` with synthetic blobs shaped like the live
  read (which the implementer spot-checked by hand against the 7/1–7/5 table). A live `sync-api` smoke
  for `seattle_mission` needs a token/connector and was not run in-ticket. Worth a manual pass by an
  operator with access, but the synthetic coverage is faithful to the live shape.
- **Interactive connector path is documentation-only.** `ads_get_ad_entities` returning "Not
  available" for `results`/`cost_per_result` is a Meta/connector limitation, not code — `profile.md`
  directs operators to use `lead`/`cost_per_lead` there. Verified that guidance reads clearly.

### Validation run in this pass
- `.venv/bin/python -m pytest tests/ -q` → **627 passed** (was 626; +1 review-added test).
- Live config + example template both confirmed to hold `onsite_conversion.lead_grouped`.
- No linter is configured in the repo (no ruff/flake8/black/mypy in `.venv` or `pyproject.toml`);
  lint step is N/A.
- No pre-existing test failures encountered; `.pre-existing-error.md` not written.
