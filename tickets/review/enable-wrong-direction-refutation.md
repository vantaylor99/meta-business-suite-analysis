description: A proposal to turn an ad back on now warns the operator when the ad's own numbers say it loses money against the account goal, so a known-loser re-enable no longer looks as trustworthy as a genuine performer.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What shipped

Enable ops are now direction-judged the same way budget scale-ups already were. Enabling an ad is
directionally a scale-up (0 → live), so a re-enable whose own cited ROAS sits below the account's
`target_roas` is the same self-contradiction as scaling up a below-target budget — and is now
**refuted** by the review gate instead of reaching the operator as a `medium`/`high`-trust proposal.

### Code changes

- **`control.build_enable_ads_plan`** (`control.py:770`) — the enable op dict gains
  `"action_type": "enable_ad"` (mirrors how `_budget_op` tags budget ops). This is the key that lets
  `review._direction_contradiction` fire on the op. Builder docstring updated to describe the
  wrong-direction refutation; `build_pause_plan` was **not** touched (pause is the safe direction, out
  of scope).
- **`review.py`** — added `_ENABLE_ACTIONS = {"enable_ad"}` next to `_SCALE_ACTIONS`, and a dedicated
  branch in `_direction_contradiction` (positioned right after the `_SCALE_ACTIONS` branch) with an
  **enable-specific** reason string (`"...enabling an ad whose ROAS X is below the Y target"`) and the
  same strict `<` the scale-up branch uses (an exactly-at-target enable is **not** refuted). Updated
  the `review_ops_plan` docstring and the `_review_plan_ops` inline comment to note enable ops now
  carry an `action_type` too.
- **`docs/META_ACTION_WORKFLOW.md`** — the enable-grounding section previously asserted "ops carry no
  `action_type`, so the direction check does not fire on an enable." That paragraph (lines ~195–199)
  was rewritten to describe the new refutation, the band-vs-direction complementarity, the
  warning-not-block semantics, and the ROAS-only scope.

No new verdict and no new applier path: `refuted` flows through the existing `_apply_op_verdict`
`VERDICT_REFUTED` handling — it appends the contradiction reason to `confidence["factors"]`, sets
`op["review_verdict"] = "refuted"`, and demotes `status` approved→proposed (a no-op for a
freshly-built enable, which starts `proposed`). It does **not** delete the op and does **not** add an
apply-time block.

## Why `refuted` is a warning, not a hard block (verify this is intended)

`apply_ops_plan`'s gate keys on **grounding** (`op_grounding_gap` over `confidence`/`evidence`), not on
`review_verdict`. So a refuted enable with a grounded (non-`abstain`) band is *not* blocked at apply —
an operator who genuinely wants the retest (a deliberate seasonal/creative-rotation re-enable) can set
the op to `approved` and `--execute` it. The refutation's job is to refuse to *present* a known loser
as a performer, not to trap operator intent. This is the settled plan decision (decision #1 in the
source ticket); the test `test_enable_ads_refuted_can_still_be_operator_approved` pins it. A reviewer
who feels a below-target enable should be hard-blocked should treat that as a **new** design question,
not an inline fix — it contradicts the locked decision.

## Use cases / validation

All in `tests/test_meta_ads_analysis.py`, in the "Enable / set_status grounding" block. Run:

```
.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q
```

→ **290 passed** (was 283 before; 7 new). No ruff/mypy/pyright is configured (pyproject declares only
pytest). No `.pre-existing-error.md` was written — the suite is fully green at HEAD with these changes.

New tests (each uses the `_enable_client` fixture; `ad2` is the PAUSED ad; ROAS = revenue/spend):

- `test_enable_ads_below_target_roas_strong_sample_is_refuted` — the core case: ROAS 1.0, target 2.0,
  30-purchase (statistically strong) sample → `action_type == "enable_ad"`, verdict `refuted`,
  `"direction" in failed_inputs`, reason names "enabling"/"1.00"/"2 target", `review_verdict ==
  "refuted"`, and the band is **left at `medium`** (refuted is a warning, not a band-cap).
- `test_enable_ads_above_target_roas_stands` — ROAS 5.0 ≥ target 2.0 → `stands`, band `medium` (no
  direction fire).
- `test_enable_ads_roas_goal_without_target_does_not_refute` — ROAS goal but **no** `target_roas` →
  `stands`. This is the guard that keeps the pre-existing computed-band tests green; pinned so a
  refactor can't silently start refuting when no target is set.
- `test_enable_ads_install_goal_not_direction_refuted` — install-goal enable carries
  `action_type == "enable_ad"` but metric is `cost_per_app_install` (≠ `blended_roas`) and goal ≠
  `roas`, so even with a `target_roas` in the policy it is **not** refuted; stays `low`/`stands`.
- `test_enable_ads_cold_ad_with_target_stays_insufficient_not_refuted` — cold ad cites a zero sample →
  `metric_value` is `None` → direction can't fire → stays `insufficient`, and the apply gate still
  blocks the approved turn-on.
- `test_enable_ads_below_target_and_below_floor_is_insufficient_not_refuted` — below-target ROAS on a
  below-floor sample → `insufficient` (rank 3) wins over `refuted` (rank 2); apply gate still blocks.
  Pins the most-conservative-wins ordering for enables.
- `test_enable_ads_refuted_can_still_be_operator_approved` — a refuted (grounded, `medium`) enable set
  to `approved` returns `dry_run`, not `blocked` — proves the warning doesn't trap operator intent.

## Known gaps / things to probe (tests are a floor, not a finish line)

- **ROAS-only by design.** Install-goal direction polarity (a below-implied-cost re-enable) is **not**
  judged here. Plan defers it to a backlog follow-up (`enable-wrong-direction-install-goal`, folding
  into `review-gate-install-goal-direction` once that lands). Install-goal enables already cap at `low`
  (the conversion sample is purchases, not installs), so a below-target install enable can't present as
  `high`/`medium`-trust anyway. Confirm the reviewer agrees the deferral is acceptable, not a hole.
- **Band assertions depend on `confidence.assess` thresholds + the default window.** The `medium`-band
  assertions in the strong-sample tests rely on `25 ≤ purchases < 100 → medium` and a default 30-day
  trailing window being recent enough that recency-staleness doesn't kick in (same assumption the
  pre-existing `test_enable_ads_paused_ad_with_strong_sample_carries_computed_band` already makes, with
  no explicit `run_date`). If the default-window logic changes, the *band* assertions could shift; the
  *verdict* assertions (`refuted`/`stands`/`insufficient`) would not. Worth a glance.
- **Strict `<`, no margin** — the enable branch uses strict `<` against the bare target (the scale-up
  convention), **not** the `target * _PAUSE_WINNER_MARGIN` (1.5×) the scale-DOWN/pause-winner branches
  use. That is intentional (enabling is a scale-up, so it mirrors the scale-up polarity), but it means
  the refutation threshold for an enable is tighter than for a pause-a-winner. Confirm that asymmetry is
  the intended polarity, not an oversight.
- **`action_type` leakage check (done, re-verify if paranoid).** Grepped: nothing in `validate_op`,
  `_build_request`, the apply path, or `briefs` branches on an *op's* `action_type` (`briefs` counts
  `action_type` only over `plan["actions"]`, never `plan["ops"]`; budget ops already carried one
  harmlessly). Idempotency, input-not-mutated, and the cold-ad structural-abstain invariants all stay
  green.
