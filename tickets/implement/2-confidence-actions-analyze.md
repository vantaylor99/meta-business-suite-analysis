description: Make the tool's proposed pause/scale actions each carry the facts behind them and a computed confidence band — and have it say "not enough data to recommend yet" instead of guessing when an ad has barely any spend or sales.
prereq: confidence-core
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/analyze.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why

`build_action_plan` (in `actions.py`) is where the report's findings become operator-facing
"do X" recommendations (pause this ad, scale this ad set). Today each action carries a free-text
`rationale` and a loose `evidence` dict of scores. This ticket makes every action carry the
**structured Evidence + computed Confidence** from `confidence.py`, and makes below-floor
scale/pause calls **abstain** ("insufficient data — keep running") instead of being emitted as a
confident pause/scale.

## What to build

### 1. Attach Evidence + Confidence to each action

In `actions.py`, for the executable/recommendation-bearing actions (`pause_ad`,
`increase_adset_budget`, and the manual `consider_scale_budget`/`refresh_creative` variants),
replace/augment the ad-hoc `evidence` dict with a structured block:

```jsonc
"evidence": {
  "metric_name": "blended_roas",
  "metric_value": 1.2,
  "metric_display": "ROAS 1.20",
  "window": "2026-06-10..2026-06-24",
  "sample_purchases": 43,
  "sample_spend": 880.0,
  "entity_level": "ad",
  "entity_id": "<ad_id>",
  "entity_name": "<ad_name>",
  "regenerating_query": "account_metrics --account <slug> --level ad --date-from 2026-06-10 --date-to 2026-06-24"
},
"confidence": {
  "band": "high", "data_band": "high", "grounding_band": "high",
  "grounding_tier": "direct_observation",
  "factors": ["43 purchases over 14d (well above 25 floor)", "window ends today (recent)", "direct API observation"],
  "would_raise": "...", "would_lower": "...", "causal_flag": false
}
```

- The metric chosen per action should be the one the call actually rests on: ROAS for the
  ROAS-goal pause/scale paths, cost-per-app-install for the install-goal paths (mirror the logic
  already in `_should_pause_ad` / `_qualifies_for_budget_increase`).
- `sample_purchases` / `sample_spend` / window come from the ad summary the report already carries
  (`total_purchase_count`, `total_spend`, `days_active`/`first_seen`/`last_seen`). The window string
  is `first_seen..last_seen`; `recency_days` is `run_date - last_seen` (compute in the caller and
  pass to `confidence.assess`).
- Grounding tier for these paths is `direct_observation` (the action plan reads live/exported API
  metrics, not an experiment). The `consider_scale_budget`/trajectory-driven calls that lean on the
  cross-sectional benchmark comparison are `correlational`.
- Pass the action's `rationale` text as `causal_text` so an accidentally causal rationale is flagged.

### 2. Abstention below the data floor

The action plan already filters waste by `waste_status != "insufficient_data"` upstream, but the
scale path and the medium-waste pause path can still fire on thin data. Add an abstention guard:

- Before emitting a confident `pause_ad` or `increase_adset_budget`, run `confidence.assess`. If the
  combined band is `abstain` (sample below the floor), emit the action as a **non-executable
  "insufficient data — keep running" recommendation** instead of a confident pause/scale: set
  `status` to `proposed`, `executable` to `False`, and a `verdict: "insufficient_data"` field, with
  a rationale that says "promising test / keep running," NOT "winner/loser." This generalizes the
  `monitor.py` significance-floor discipline into the action plan.
- A brand-new entity with zero spend/purchases must resolve to this abstention, never a fabricated
  call (see edge cases).

### 3. analyze.py free-text recommendations

`_build_recommendations` in `analyze.py` emits the `next_7_day_actions` prose strings. Append the
core facts inline to each so even the narrative line is grounded — e.g.
`"Reduce or pause budget on <ad> (ROAS 1.20 over 14d, 43 purchases, $880 spend) …"`. Pull these
from the ad summary already in scope. Don't compute a confidence band in the prose here (the band
lives on the structured action); just ensure the metric/window/sample/entity facts are present so
the prose obeys the section-4 rule mechanically.

## TODO

- [ ] In `actions.py`, add a helper that builds an `Evidence` + `Confidence` for an ad-derived
      action using `confidence.assess` + `build_regenerating_query`, and attach both to `pause_ad`,
      `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`.
- [ ] Add the abstention guard that flips below-floor confident actions to a non-executable
      `verdict: "insufficient_data"` recommendation.
- [ ] Thread `account_slug` + `run_date` into the builders so the regenerating query + recency can
      be computed (both already available on the payload).
- [ ] Append metric/window/sample facts to `analyze._build_recommendations` prose.
- [ ] Tests (see below); run `python -m pytest tests/ -q 2>&1 | tee /tmp/conf_actions.log`.

## Key tests (TDD)

- A pause on an ad with ROAS 1.2 / 14d / 43 purchases → action carries `confidence.band == "high"`,
  `grounding_tier == "direct_observation"`, and Evidence with the four facts + a non-null
  `regenerating_query`. (Mirrors the parent ticket's headline use case: 🟢 High ~85%.)
- The same ad with 3 purchases / $40 spend / 4 days → action is non-executable with
  `verdict == "insufficient_data"`, rationale phrased as "keep running," NOT a confident pause.
- A `consider_scale_budget`/trajectory action whose rationale asserts cause ("scale because the new
  audience converts") → `grounding_tier == "correlational"`, `causal_flag is True`, band capped at
  medium-or-lower even though sample is large.
- Existing action-plan tests (`test_action_plan_*`) still pass — the executable pause/budget paths
  and the guarded-write gate are unchanged in behavior; only the evidence/confidence shape is added.

## Edge cases & interactions

- **Zero-sample entity → abstain.** Brand-new ad, $0 spend, no purchases must produce the
  insufficient-data verdict, never a fabricated pause/scale. Test this boundary explicitly.
- **Must not weaken the guarded-write gate.** This sits UPSTREAM of `build_api_operation` /
  approval. PAUSED-by-default, `proposed → approved → validate_only → execute`, and the Meta-AI
  param block must be untouched. An action that abstains becomes non-executable, so it can never be
  approved into a write.
- **Read-only w.r.t. Meta.** No new account writes; this only changes how recommendations are
  represented in `action_plan.json`.
- **Backward compatibility.** `action_plan.json` consumers (the brief in the next ticket, existing
  tests) must keep working. Keep `rationale`; ADD `confidence` + the structured `evidence` rather
  than removing fields other code reads. If `evidence` changes shape, update the brief renderer (its
  own ticket) — note the dependency.
- **Grounding cap over sample.** The correlational scale path must never read High purely because
  the ad set has large spend; the cap comes from `confidence.combine_bands`.
- **Recency.** `recency_days` is derived from `run_date` vs the ad's `last_seen`; a stale-window
  recommendation rounds the data band down.
