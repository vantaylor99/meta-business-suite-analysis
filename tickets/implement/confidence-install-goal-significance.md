description: For app-install accounts, a recommendation's confidence rating only ever counts purchases — which those accounts rarely have — so even a recommendation backed by lots of installs is stuck at a low rating. Make the rating count the conversion type that actually fits the account's goal.
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----

## Problem (recap)

`evaluate_action_confidence` (actions.py) always sources `Evidence.sample_purchases` from
`ad["total_purchase_count"]` for **every** goal. Install-goal accounts
(`primary_goal == "maximize_in_app_subscriptions"`, e.g. `pollen_sense`) typically report
`total_purchase_count == 0`, so the conversions floor (`CONFIDENCE_CONVERSIONS_FLOOR == 25`
purchases) is structurally never cleared, and the confidence band is capped at `low` no matter how
many in-app subscriptions or app installs accrued. The metric the call rests on already switches by
goal (`_select_action_metric` → cost-per-install for install accounts), but the *sample* that grounds
significance does not — so the `evidence` block is internally inconsistent (cost/install metric,
purchases sample) and the installs that actually back the call are dropped.

See the source ticket / `docs/META_ACTION_WORKFLOW.md` "Evidence and Confidence" for full provenance.

## Design decisions (resolved — do not re-litigate)

**1. Significance is grounded on a goal-aware conversion count.** Add a small private helper in
actions.py — `_select_sample_conversions(ad, goal) -> float | None` — that mirrors the prioritization
the rest of the goal logic already uses:

- `goal == "maximize_in_app_subscriptions"` → **`total_results` when `total_results > 0`, else
  `total_app_installs`**. This is the *exact* rule `_should_pause_ad` already applies
  (`total_results in (None, 0, 0.0)` → fall to the app-install signal), so significance grounding
  and pause detection now agree on what the account's conversion signal is.
- `goal == "roas"` → `total_purchase_count` (unchanged).
- default / unknown goal → `total_purchase_count` (unchanged — preserves today's behavior for every
  non-install account).

Wire this into `evaluate_action_confidence`: replace
`sample_purchases=_number(ad.get("total_purchase_count"))` with
`sample_purchases=_select_sample_conversions(ad, goal)` (the `goal` is already resolved at the top of
the function for `_select_action_metric`). Result: for an install ad with 80 installs / $250 spend,
the sample becomes 80, clears the floor, and the band reads `medium`/`high` on its real signal
instead of `low`.

*Considered and rejected:* "results if `>= floor` else installs" (would let a few subscriptions fall
back to a richer install count). Rejected for consistency — the codebase's established rule is
"results present at all → that's the signal; no results → installs," and reusing it keeps significance
grounding, pause detection, and scale qualification speaking one language. Tradeoff: an install ad
with a *few* subscriptions (e.g. 3) and many installs (80) grounds on the 3 subscriptions and may stay
thin/`low`. This is the intended conservative behavior — subscriptions are the account's real
commercial signal, and 3 of them honestly *is* thin; the installs fallback is specifically for "no
subscription volume yet." Name this in a test so the choice is pinned, not accidental.

**2. The structural field/key rename (`sample_purchases` → `sample_conversions`) is DEFERRED, not
done here.** That symbol spans 10 source modules, ~30 test sites, the serialized JSON key consumed by
the apply-time grounding guard (`write_grounding.py`), `review.py`, `monitor.py`, and stored
`action_plan.json` — too large and risky for this change, and it would churn the
already-reviewed `confidence-core` schema. It is tracked separately in
`tickets/backlog/confidence-sample-conversions-rename.md`. **Do NOT rename the field, the
`data_strength(sample_purchases=...)` parameter, or the JSON key in this ticket.**

**3. Make confidence.py's operator-facing wording honest now (small, deliberate confidence-core
touch).** Because the sample for an install account is now installs/subscriptions, the human-readable
factor and evidence strings must stop saying "purchases." Change the *operator-visible wording* from
"purchases" → "conversions" in:

- `data_strength` factor strings: `"sample: N purchases / $S spend (over floor)"` and
  `"sample: $S spend cleared but only N purchases (< floor) — thin on conversions"` and the abstain
  string `"below significance floor: N purchases < floor ..."`.
- `render_evidence_line`: `"n=N purchases / $S spend"` → `"n=N conversions / $S spend"`.
- Rename the internal helper `_fmt_purchases` → `_fmt_conversions` (purely internal; no callers
  outside confidence.py beyond these strings).

"Conversions" is honest for **every** goal (a purchase is a conversion), so ROAS-account output stays
correct. Do **not** touch `BAND_PRESENTATION` (pinned verbatim to `knowledge/README.md`), the band
names, or any serialized key — only these free-text factor/sample strings.

*Out of scope (deliberately not done):* adding a `conversion_kind` field to `Evidence` to print
"80 app installs" vs "80 subscriptions". The `metric_display` already names the metric
(cost/install), so the generic "conversions" wording is sufficient; a typed kind label is a possible
future refinement, not a requirement.

## Edge cases & interactions

- **Spend floor is pre-cleared for guarded actions.** `pause_ad` uses `spend_floor=MIN_WASTE_SPEND`
  (100) and only fires on `waste_status` high/medium, which itself requires spend ≥ `MIN_WASTE_SPEND`;
  `increase_adset_budget` uses `MIN_SCALING_SPEND` (75) and only fires on scaling candidates
  (spend ≥ `MIN_SCALING_SPEND`). So for a *real* pause/scale candidate the spend axis is always
  cleared, and `data_strength` therefore returns `abstain` only when **both** floors fail — which
  cannot happen on a real candidate. The meaningful transition this ticket fixes is `low → medium/high`
  driven by conversion volume, **not** `abstain`. To exercise the `abstain` branch deterministically,
  a test must drive a thin **spend** too (e.g. call `evaluate_action_confidence` directly with an
  install ad whose spend is below the floor, or use a `consider_scale_budget`/`refresh_creative` ad —
  those are non-guarded and not spend-gated the same way). Do not write an abstain fixture that
  silently can't abstain because spend cleared.
- **`results > 0` boundary.** `total_results` exactly `0` or `0.0` or `None` → fall to installs;
  any positive value → use results. `_number`/the existing `in (None, 0, 0.0)` idiom already handles
  the float/None cases — reuse it, don't invent a new coercion.
- **Both signals absent.** install ad with `total_results` 0/None and `total_app_installs` 0/None →
  sample is 0/None → with spend cleared, band is `low` ("thin on conversions"); honest, unchanged
  shape. Only flips to `abstain`/`insufficient_data` if spend is also below floor.
- **Non-install goals unchanged.** ROAS and default/unknown goals must produce byte-identical
  `sample_purchases` values as before (still `total_purchase_count`). Add/keep a test pinning that a
  Divine Designs (ROAS) action's sample is still the purchase count.
- **`4 * conversions_floor` high threshold.** With the floor at 25, an install ad needs ≥ 100
  installs/subscriptions to reach the `high` base band; 25–99 → `medium`. Recency still rounds down one
  level (and unknown recency rounds down), so verify the *combined* expectation in tests, not just the
  base band.
- **Grounding cap still applies.** `pause_ad`/`increase_adset_budget` are `direct_observation`
  (ceiling `high`); `consider_scale_budget`/`refresh_creative` are `correlational` (ceiling `medium`)
  — so a well-sampled scale *candidate* still cannot read above `medium`. Don't write a test expecting
  `high` on a `consider_scale_budget`.
- **`_abstain_action` rationale wording.** It currently prints "only N purchases / $S spend". Decide
  whether to leave it (it reads `evidence_block["sample_purchases"]`, whose key is unchanged) or
  align its prose to "conversions" for install consistency. Aligning is preferred for honesty; if you
  do, update its assertion in tests. Either way the *key* stays `sample_purchases`.
- **Test-text churn from the wording change.** Existing tests assert exact substrings like
  `"120 purchases"` (brief renderer, ~line 1275) and factor text containing "purchases". Update every
  such assertion to "conversions". Grep `tests/test_meta_ads_analysis.py` for `purchases` before
  declaring done; do not leave a stale assertion that only passed because the value happened to match.
- **Stored-plan back-compat.** `evidence_from_dict`/`confidence_from_dict` and the apply-time guard
  read the `sample_purchases` JSON key — which is **unchanged** — so previously written
  `action_plan.json` files still deserialize. Confirm no serialization key moved.

## TODO

### Phase 1 — goal-aware significance sample (actions.py)
- Add `_select_sample_conversions(ad, goal) -> float | None` implementing decision (1), reusing
  `_number` and the existing `total_results in (None, 0, 0.0)` idiom.
- In `evaluate_action_confidence`, source `Evidence.sample_purchases` from
  `_select_sample_conversions(ad, goal)` instead of `ad["total_purchase_count"]`.
- (Optional, preferred) align `_abstain_action`'s rationale wording to "conversions"; leave the
  `sample_purchases` JSON key untouched.

### Phase 2 — honest operator-facing wording (confidence.py)
- Generalize the three `data_strength` factor strings + the `render_evidence_line` sample string from
  "purchases" → "conversions"; rename `_fmt_purchases` → `_fmt_conversions`. Leave `BAND_PRESENTATION`,
  band names, and all serialized keys/params (`sample_purchases`, `data_strength(sample_purchases=…)`)
  exactly as-is.

### Phase 3 — tests (tests/test_meta_ads_analysis.py)
- **Above-low on real install volume:** an install-goal (`maximize_in_app_subscriptions`) pause/scale
  ad with `total_results == 0`, `total_app_installs >= 100` (or subscriptions ≥ 100), spend cleared,
  recent window → band `medium` or `high` (account for recency round-down and the grounding ceiling).
  Assert the `evidence` `sample_purchases` value equals the install/subscription count, not the
  purchase count.
- **Subscriptions-first selection:** install ad with `total_results > 0` grounds on results, not
  installs (pins decision-1 tradeoff).
- **Install abstain when genuinely thin:** drive both signals thin *and* spend below the floor (direct
  `evaluate_action_confidence` call, or a non-guarded action type) → band `abstain`; for a guarded
  action, assert it flips to `verdict: "insufficient_data"`, `executable: false`.
- **ROAS goal unchanged:** a Divine Designs action's `sample_purchases` still equals
  `total_purchase_count`.
- Update all stale `"... purchases"` substring assertions to `"... conversions"`.

### Phase 4 — docs
- Update `docs/META_ACTION_WORKFLOW.md` "Evidence and Confidence" to state how significance is
  grounded per goal: subscriptions-first then app-installs for install-goal accounts; purchases for
  ROAS/default. Note that the sample wording is "conversions" (goal-neutral) and that the serialized
  key remains `sample_purchases` pending the tracked rename.

### Validation
- `pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/pytest.log` (stream, don't silently
  redirect). Run any repo type-check/lint the project uses (see AGENTS.md). Fix failures you cause;
  flag genuinely pre-existing ones per the runner's `.pre-existing-error.md` protocol.
