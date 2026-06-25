description: The action plan's pause/scale recommendations now each carry the facts behind them and a computed trust band, and a too-thin ad is returned as "not enough data yet — keep running" instead of a confident pause or scale.
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/analyze.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

Wired the `confidence.py` engine (from `confidence-core`) into the action plan and the narrative
recommendations.

### `actions.py`
- New public helper `evaluate_action_confidence(ad, *, action_type, policy, account_slug, run_date,
  rationale)` → `(evidence_dict, confidence_dict)`. It picks the metric the call rests on (ROAS for
  ROAS-goal accounts, cost-per-install for install-goal accounts — mirroring
  `_should_pause_ad`/`_qualifies_for_budget_increase`), builds an `Evidence` from the ad summary
  (`total_purchase_count`, `total_spend`, `first_seen..last_seen` window, `entity_id`/`name`,
  `build_regenerating_query(... level=ad ...)`), computes recency as `run_date - last_seen`, and
  calls `confidence.assess`. The action's `rationale` is passed as `causal_text` so an accidentally
  causal rationale downgrades grounding.
- New `_attach_confidence(...)` attaches `evidence` + `confidence` blocks to each
  recommendation-bearing action (`pause_ad`, `increase_adset_budget`, `consider_scale_budget`,
  `refresh_creative`) and, for the executable `pause_ad`/`increase_adset_budget` paths, applies the
  **abstention guard**: when the combined band is `abstain` (sample below the floor) it flips the
  action to non-executable via `_abstain_action` — `executable=False`, `approval_required=False`,
  `verdict="insufficient_data"`, `status` stays `proposed`, and the rationale is rewritten to
  "promising test … keep running," never winner/loser.
- Grounding tiers: `pause_ad` / `increase_adset_budget` → `direct_observation`;
  `consider_scale_budget` / `refresh_creative` → `correlational`. Spend floors reuse existing gates
  (`MIN_WASTE_SPEND` for pause/refresh, `MIN_SCALING_SPEND` for scale); conversions floor =
  `CONFIDENCE_CONVERSIONS_FLOOR` (25). The old ad-hoc `evidence` score dict on these actions is
  **replaced** by the structured block.

### `analyze.py`
- `_build_recommendations` prose now appends inline facts via `_recommendation_facts` (metric /
  window / sample / spend, e.g. `"… Waste Ad (ROAS 1.20, over 6d, 18 purchases, $360 spend) first
  because …"`) and `_trajectory_facts` (metric + % change) for the degrading/improving lines. No
  band is computed in the prose — that lives on the structured action, per the ticket.

### `config.py`
- Added `CONFIDENCE_CONVERSIONS_FLOOR = 25` (the comment block already promised this value from
  `experiment.py`'s `min_conversions` default).

### `confidence.py` (beyond the ticket's listed files — see "Decisions" below)
- Added pure serializers `evidence_to_dict` / `confidence_to_dict` (needed now to write
  `action_plan.json`) and their inverses `evidence_from_dict` / `confidence_from_dict` (for the
  brief renderer in ticket `confidence-operator-brief`). Bands serialize as their lowercase **name**
  (`"high"`…`"abstain"`), never a number. `confidence_from_dict` is a deserializer for an
  already-computed verdict, NOT a scoring path — `assess` remains the only way to *compute* a band.

## Validation / use cases (tests added, all green: 106 passed)

`.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/conf_actions.log` → **106 passed** (98 prior + 8 new).

- `test_action_plan_pause_carries_high_confidence_and_direct_observation` — well-sampled (120
  purchases / $2400), recent, directly-observed pause → `band=="high"`,
  `grounding_tier=="direct_observation"`, `causal_flag` False, still executable, no `verdict`;
  Evidence carries the four facts + a real (non-null) `regenerating_query`.
- `test_action_plan_pause_below_floor_abstains_as_keep_running` — 3 purchases / $40 / 4d →
  `band=="abstain"`, `verdict=="insufficient_data"`, `executable=False`, `approval_required=False`,
  rationale contains "keep running" and NOT "winner"/"loser".
- `test_action_plan_zero_sample_ad_abstains_never_fabricates_pause` — $0 / 0 purchases boundary →
  abstain → insufficient-data, never a fabricated pause.
- `test_evaluate_action_confidence_flags_causal_correlational_and_caps_band` — large-sample
  `consider_scale_budget` with a causal rationale → `correlational`, `causal_flag` True, `band=="low"`
  (grounding caps the high data band; sample size does not average it away).
- `test_action_plan_pause_keeps_rationale_and_params_backward_compatible` — executable pause path
  unchanged (`rationale`/`params`/`executable`/`approval_required`); confidence+evidence additive.
- `test_recommendations_prose_carries_metric_window_sample_facts` — the prose line carries
  metric/window/sample/spend.
- `test_evidence_and_confidence_dicts_round_trip` — serializer round-trip + band-as-name.
- Existing `test_action_plan_*` and the report→plan integration test still pass unchanged (they now
  exercise `_attach_confidence` on real serialized summaries).

## Decisions / honest gaps for the reviewer

- **⚠️ The ticket's headline "43 purchases → 🟢 High ~85%" is NOT what the shipped rubric computes.**
  `confidence-core` (reviewed + accepted) only reaches `high` at ≥ 4× the conversions floor (≥ 100
  purchases); 43 clears the floor but lands at **medium**. I implemented faithfully against the real
  `assess` and pinned the actual band in
  `test_action_plan_pause_with_43_purchases_reads_medium_under_calibrated_knee` (asserts `medium`),
  and used a ≥100-purchase sample for the genuine "High" test. I did **not** alter `confidence-core`
  (out of scope, and its own review flagged the 4× knee as un-calibrated). If product wants 43 → High,
  that is a calibration change to `confidence.data_strength`, not this ticket. **Please confirm this
  is the intended reading.**
- **Touched `confidence.py` and `config.py` beyond the ticket's `files:` list.** `config.py` was
  expected (constant). `confidence.py` got 4 pure (de)serializers — the natural home for the
  Evidence/Confidence JSON contract and what ticket `confidence-operator-brief` will consume. The
  anti-fabrication invariant (no band/score knob on `assess`) is untouched; `*_from_dict` only
  rehydrates an already-computed verdict for rendering. Flagging for visibility.
- **Tier for `refresh_creative` = `correlational`** (judgement call — fatigue is a prior-vs-recent
  trajectory comparison, which the ticket groups with the correlational calls). Defensible either
  way; `direct_observation` would also be arguable since it's the ad's own metrics. Non-load-bearing
  (refresh is non-executable, no abstention consequence). Reviewer may flip if preferred.
- **`review_waste_without_ad_id`** (a waste finding with no `ad_id`) is intentionally NOT given a
  structured evidence/confidence block — there is no entity to ground it to; it keeps its prior
  shape. Same for `measurement_review` / `disable_meta_ai_controls` (not in scope).
- **Backward-compat / downstream dependency:** the `evidence` value on pause/scale/refresh actions
  **changed shape** (structured block, not the old `waste_score`/`scaling_score` dict). Confirmed no
  current consumer reads the old sub-keys (grep of `src/` + `tests/`). The operator brief
  (`confidence-operator-brief`, depends on this ticket) must read the new structured `evidence` +
  `confidence`; `confidence.*_from_dict` are provided for it.
- **Guarded-write gate untouched & verified safe:** an abstaining action is `executable=False`, so
  `apply_action_plan` skips it and `build_api_operation`/Meta-AI param block are never reached. The
  PAUSED-by-default + `proposed→approved→validate_only→execute` flow is unchanged. Read-only w.r.t.
  Meta — only `action_plan.json` representation changed.

## Suggested reviewer focus
- Re-derive a `band` from the source for one pause and one abstain case rather than trusting the
  tests. Probe the abstain→write path (can a human re-approve an abstained action into an execute?).
- Sanity-check the metric-selection mirrors `_should_pause_ad`/`_qualifies_for_budget_increase` for
  both account goals, and that recency uses `run_date` (not wall clock).
