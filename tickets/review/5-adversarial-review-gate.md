description: Before any pause/scale recommendation reaches the operator, an automatic second-opinion check now judges each call from only the facts cited for it — big enough sample, long enough window, correlation-vs-cause, is the stated confidence actually earned, and does the call agree with its own numbers — and downgrades or drops the ones that fail (showing them, not hiding them) so plausible-but-wrong advice can't slip through.
prereq:
files: src/meta_ads_analysis/review.py, src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: hard
----
## What shipped

A new pure module `src/meta_ads_analysis/review.py` (no Meta API, no network, no clock — mirrors
`confidence.py`'s style) that adversarially reviews each structured recommendation **from its cited
`evidence` + claimed `confidence` band only**, deliberately not trusting the producing `rationale`'s
conclusion. It re-derives the band from the same evidence via `confidence.assess` and compares,
reusing the one confidence vocabulary (`Band`/`assess`/`combine_bands`/`grounding_strength`).

### `review_recommendation(...) -> ReviewResult`

Runs six refutation checks and returns the **most-conservative** verdict
(`insufficient` > `refuted` > `downgrade` > `stands`), accumulating a reason + the failing rubric-input
key from **every** check that fired (never a vague "looks wrong"):

1. `sample_floor` — cited sample clears neither the conversions floor (25) nor the per-action spend
   floor → `insufficient` (abstain). *("9-purchase winner" case.)*
2. `window_length` — window span (`end - start` from the `YYYY-MM-DD..YYYY-MM-DD` string) shorter than
   `REVIEW_MIN_WINDOW_DAYS` (7) → `downgrade` one band. *("ROAS 1.1 over 3 days" case.)*
3. `causal` — `causal_flag` true, tier ≠ `ab_experiment`, and claimed band exceeds the causal-capped
   band `combine(data_band, grounding_strength(tier, causal=True))` → `downgrade` to the capped band.
4. `band_earned` — recompute the band from evidence + tier via `assess` (ignoring rationale); if the
   claimed band exceeds the recompute → `downgrade` to the recomputed band. The structural defense
   against a drifted/hand-edited band.
5. `direction` — ROAS-goal account whose cited ROAS contradicts the action: a scale/budget-increase
   below `target_roas`, or a `pause_ad` comfortably (≥1.5×) above target → `refuted`.
6. `external` — tier `external` with a band above `low` → `downgrade` to `low`.

A `downgrade` whose revised band lands on `abstain` becomes `insufficient`.

### `review_action_plan(plan, ...) -> dict`

Returns a **new** plan (input never mutated). Each recommendation-bearing action (one carrying a
`confidence` block: `pause_ad`, `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`)
gains a `review` block and has `confidence`/`status`/`executable` adjusted per verdict — `stands`
unchanged; `downgrade` lowers the band (caps both axes) and appends the reasons to `confidence.factors`;
`insufficient` → `executable=False`, `status=proposed`, `verdict="insufficient_data"`, band→abstain,
keep-running rationale; `refuted` → `executable=False`, `verdict="refuted"`, demoted out of `approved`.
Actions with no `confidence` block pass through untouched. **Idempotent** via a skip-guard: an action
that already carries a `review` block is left as-is.

### Wiring

- `briefs.build_operator_brief` gained `review_enabled: bool = True`; when on it runs
  `review.review_action_plan(plan)` first and builds the brief from the reviewed plan. `_brief_action`
  carries the `review` block through (additive). `render_operator_brief` adds a
  **"Refuted / Downgraded By Review"** section (refuted + insufficient verdicts, with failing input +
  reason, excluded from all other sections) and appends a `↓ Review:` line under the confidence block
  for in-section downgrades. New `summary.reviewed_out_count`.
- `cli.operator_brief_main` gained `--no-review` (passes `review_enabled=False`); default is review ON.
- `config.REVIEW_MIN_WINDOW_DAYS = 7` (the only new number — floor/recency re-checks reuse
  `MIN_WASTE_SPEND`/`MIN_SCALING_SPEND`/`CONFIDENCE_CONVERSIONS_FLOOR`/`CONFIDENCE_RECENCY_STALE_DAYS`).

## Validation

`python -m pytest tests/ -q` → **137 passed** (124 pre-existing + 13 new). New tests:

- `test_review_below_floor_returns_insufficient` — 9 purchases / 5-day window → `insufficient`,
  `sample_floor` named.
- `test_review_short_window_downgrades` — 3-day window → `downgrade`, revised band lower,
  `window_length` named.
- `test_review_causal_correlational_downgrades` — inflated `high` on correlational+causal → `downgrade`,
  `causal` named, "A/B" in reason.
- `test_review_band_earned_downgrades` — band drifted above `assess` recompute → `downgrade` to the
  recomputed band.
- `test_review_external_caps_at_low` — external tier above `low` → `downgrade` to `low`.
- `test_review_direction_contradiction_refutes` — pause of a ROAS-6 ad vs 3.0 target → `refuted`.
- `test_review_clean_call_stands` — large recent direct-observation pause → `stands`, empty
  `failed_inputs`.
- `test_review_action_plan_below_floor_flips_to_keep_running` — plan-level abstention shape + input
  not mutated.
- `test_review_action_plan_skips_non_recommendation_actions` — `measurement_review` untouched.
- `test_review_gate_only_ever_demotes` — asserts executable never raised, status never promoted to
  approved, band never raised.
- `test_review_action_plan_is_idempotent` — `review(review(plan)) == review(plan)`.
- `test_operator_brief_review_refuted_direction_surfaced_not_approved` — refuted call lands in the new
  section, not "Approved To Execute".
- `test_operator_brief_no_review_reproduces_pre_gate_behavior` — `--no-review` escape hatch.

Manual smoke test rendered a 3-action brief (stands → Approved; short-window → Ready-for-Review at the
lower band with a `↓ Review:` line; ROAS-6 pause → "Refuted / Downgraded By Review" with the `direction`
reason). `--no-review --help` confirmed.

## Use cases the reviewer should re-exercise

- The "9-purchase winner" never reaches the brief as a confident call (sample_floor → insufficient).
- A 3-day-window pause is downgraded, not trusted at face value.
- A correlational call that asserts cause but escaped the producer's causal cap is downgraded.
- A pause that contradicts its own ROAS vs the account goal is refuted and **surfaced**, never deleted.
- The gate can only demote — it sits upstream of the guarded-write approval and cannot promote a
  proposed action into an executable/approved one (so it cannot weaken the write gate).

## Known gaps / where to push hardest (treat tests as a floor, not a finish line)

- **Install-goal direction is not checked.** The `direction` check (check 5) fires **only** for ROAS
  goals (`primary_goal == "roas"`, cited `blended_roas`, numeric `target_roas`). Accounts on
  `maximize_in_app_subscriptions` (cost-per-install metric) get no direction refutation. This is a
  deliberate conservative skip; decide whether install-goal contradictions need coverage (likely a
  follow-up fix/plan ticket if so).
- **`recency_stale_days` is accepted but effectively inert.** The `band_earned` recompute defers to
  `confidence.assess`, which hardcodes `CONFIDENCE_RECENCY_STALE_DAYS`. `review_action_plan` passes the
  same constant so behavior is consistent, but the parameter does not independently change anything —
  confirm that's acceptable or wire it through a direct `data_strength(stale_days=…)` recompute.
- **Per-action spend-floor map is duplicated.** `review._ACTION_SPEND_FLOOR` mirrors
  `actions._ACTION_SPEND_FLOOR` by hand (importing `actions` would pull in `meta_api` and break the
  module's purity). If the producer's floors change, this copy must be kept in sync — verify they match
  today and consider whether the shared values belong in `config.py`.
- **Idempotency is enforced by a skip-guard, not per-check fixed points.** `window_length` downgrades
  relative to the *current* band, so a second independent review (after clearing the `review` block)
  would downgrade again. This is correct for the single-pass-before-brief flow but is worth confirming
  no other code path re-runs the gate on an already-reviewed plan without the guard.
- **`band_earned` recompute faithfulness.** It uses `recency_days` derived from window-end vs the
  plan's `run_date` (matching the producer). When `run_date`/window is missing, recency is `None` and
  `assess` rounds down exactly as the producer did → no false positives, but a band that *should* have
  been recency-downgraded but wasn't (because the producer also couldn't determine recency) is not
  separately caught. Confirm the `_recompute_band` / `_causal_capped_band` logic against
  `confidence.assess` for tier-coercion edge cases (unknown tier → `None` → no verdict, by design).
- **Semantic refutations are out of scope here.** KB-narrative contradiction, cherry-picked-window vs
  known relearning periods, and free-text-prose grounding are the **companion doc-procedure**
  `adversarial-review-protocol` (the doc mirror of this code gate, analogous to how
  `grounding-rules-and-external-evidence` mirrors `confidence.py`). Confirm that companion ticket is
  tracked; this code gate intentionally does **not** re-pull metrics or read the KB.
- **`_render_review_note` / section interaction.** Eyeball the rendered Markdown for an
  `insufficient` action (band → abstain renders "⚪ Insufficient data — keep running" *and* a
  "Review — Insufficient data […]" line) — confirm the slight redundancy reads acceptably and that no
  `None`/false-precision leaks in.
