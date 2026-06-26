description: When an operator is shown a proposal to turn an ad back on, the system never warns that the ad's own numbers say it loses money against the account's goal. Add that warning so a known-loser re-enable no longer looks as trustworthy as a genuine performer.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Background

`control.build_enable_ads_plan` proposes `set_status=ACTIVE` ops for not-active ads, each with an
`evidence` block + a **computed** `confidence` band, then runs the plan through
`review.review_ops_plan`. The band machinery protects against an *over-confident* enable (the band can
never exceed what the cited sample supports), but **not** against a **wrong-direction** enable:
re-enabling an ad whose cited ROAS sits clearly below the account's `target_roas`, on a
statistically-strong / high-spend sample, produces a `medium`/`high` band and a `stands` verdict — it
reaches the operator looking exactly as trustworthy as re-enabling a genuine performer.

The review gate already refutes this contradiction for **budget** ops: `_budget_op` tags each op with
an `action_type` (`increase_*`/`decrease_*_budget`), and `review._direction_contradiction` refutes a
scale-up whose cited ROAS is below `target_roas` (`review._SCALE_ACTIONS`). Enable ops carry **no**
`action_type`, so the direction check no-ops on them today. Enabling an ad is directionally a scale-up
(0 → live), so a below-target enable is the same self-contradiction as a below-target budget scale-up.

## Decisions settled in plan (do not re-litigate)

1. **Strength = `refuted`** (the same verdict budget scale-ups get), *not* a band-cap. The
   plan-ticket's stated worry — that a hard refutation blocks legitimate operator intent (a deliberate
   retest, a seasonal ad, a creative rotated back in) — **does not apply to ops.** For an op, `refuted`
   via `_apply_op_verdict` only: appends the contradiction reason to `confidence["factors"]`, sets
   `op["review_verdict"] = "refuted"`, and demotes `status` approved→proposed (a no-op for an enable,
   which starts `proposed`). It does **not** delete the op and does **not** add an apply-time block —
   `apply_ops_plan`'s gate keys on grounding (`op_grounding_gap`), not on `review_verdict`. So an
   operator who genuinely wants the retest can still set the op to `approved` and `--execute` it; the
   refutation is a loud, evidence-named warning that refuses to *present* a known loser as a performer,
   not a hard block. That is exactly the behavior the source ticket asks for ("should not reach the
   operator looking as trustworthy as a re-enable of a genuine performer").

2. **Keying = a synthetic `action_type = "enable_ad"`** set on the enable op in
   `build_enable_ads_plan`, mirroring how `_budget_op` sets its `action_type`. Add a dedicated
   `_ENABLE_ACTIONS = {"enable_ad"}` set and a dedicated branch in `_direction_contradiction` with an
   *enable-specific* reason string (do **not** fold `enable_ad` into `_SCALE_ACTIONS` — keep the
   taxonomy and the operator message readable and distinct from a budget scale-up).

3. **ROAS-only, for now.** The enable direction rule fires only under the same guard the existing check
   uses: `policy["primary_goal"] == "roas"` + numeric `target_roas` + cited
   `evidence["metric_name"] == "blended_roas"` + a present `metric_value`. Install-goal enables are
   intentionally **not** direction-judged here — they already cap at the `low` band (the conversion
   sample is purchases, not installs; see the `enable-and-set-status-write` review note), so a
   below-target install enable cannot present as `high`/`medium`-trustworthy anyway. The cost-per-install
   polarity extension for enables is deferred to a follow-up
   (`enable-wrong-direction-install-goal`, backlog) that folds into `review-gate-install-goal-direction`
   once that lands. Do not couple to install-goal cost semantics in this ticket.

## Why existing tests still pass (verify, do not assume)

The current enable/overclaim tests do **not** set `target_roas`:
- `test_enable_ads_paused_ad_with_strong_sample_carries_computed_band` passes
  `policy={"primary_goal": "roas"}` (no target) → `_num(policy.get("target_roas"))` is `None` →
  direction no-ops → still `stands`.
- `test_review_ops_plan_demotes_overclaimed_enable` hand-builds an op with no `action_type` and a plan
  with no `account_action_policy` → direction no-ops → still `downgrade`.

So adding the rule is non-breaking for them. Run the suite to confirm — if either flips, the cause is a
bug in the new branch's guards, not an expected change.

## Interfaces / shape

`control.build_enable_ads_plan` — the enable op dict gains one key:

```python
op = {
    "op_id": f"enable_ad_{ad.get('id')}",
    "op": "set_status",
    "level": "ad",
    "id": ad.get("id"),
    "name": ad.get("name"),
    "action_type": "enable_ad",          # NEW — lets review._direction_contradiction fire
    "params": {"status": "ACTIVE"},
    "status": PROPOSED_STATUS,
    "note": ...,
}
```

`review.py`:

```python
# Enabling an ad is directionally a scale-up (0 → live). For a ROAS-goal account, turning ON an ad
# whose own cited ROAS sits below target contradicts the goal the same way a budget scale-up does.
_ENABLE_ACTIONS = {"enable_ad"}
```

In `_direction_contradiction`, after the existing `_SCALE_ACTIONS` / `_SCALE_DOWN_BUDGET_ACTIONS` /
`pause_ad` branches (all already guarded by the ROAS-goal + numeric-target + `blended_roas` +
present-`roas` preconditions at the top), add:

```python
if action_type in _ENABLE_ACTIONS and roas < target:
    return (
        f"recommendation contradicts its cited metric vs the account goal: enabling an ad "
        f"whose ROAS {roas:.2f} is below the {target:g} target"
    )
```

Use the same strict `<` the scale-up branch uses, so an exactly-at-target enable is **not** refuted
(consistency with the scale-up polarity).

No new verdict, no new applier path: `_apply_op_verdict`'s existing `VERDICT_REFUTED` handling does the
right thing for an op (factors + `review_verdict` + approved→proposed demotion).

## TODO

### Phase 1 — code
- In `control.build_enable_ads_plan`, add `"action_type": "enable_ad"` to the enable op dict (only the
  enable op — do not touch `build_pause_plan`; pause direction is out of scope and the safe direction).
- In `review.py`, add `_ENABLE_ACTIONS = {"enable_ad"}` near `_SCALE_ACTIONS` with the scale-up-analogy
  comment, and the enable branch in `_direction_contradiction` (enable-specific reason string, strict
  `<`).
- Update the `review_ops_plan` docstring (and the `_review_plan_ops` comment that says "most ops carry
  no action_type … the exception is budget ops") to note that enable ops now also set an `action_type`
  so the direction check fires on a below-target re-enable. Update the `build_enable_ads_plan` docstring
  to mention the wrong-direction refutation alongside the existing cold-ad/below-floor behavior.

### Phase 2 — tests (`tests/test_meta_ads_analysis.py`)
- **Builder, below-target strong sample → refuted:** `build_enable_ads_plan` with
  `policy={"primary_goal": "roas", "target_roas": 2.0}` and insights giving ROAS ≈ 1.0 on a strong
  purchase sample (e.g. spend 500, purchase value 500, 30 purchases — the same fixture shape as
  `test_enable_ads_paused_ad_with_strong_sample_carries_computed_band`). Assert
  `op["action_type"] == "enable_ad"`, `op["review"]["verdict"] == "refuted"`,
  `"direction" in op["review"]["failed_inputs"]`, the reason names "enabling" + the ROAS + target, and
  `op["review_verdict"] == "refuted"`.
- **Builder, above-target → stands:** same policy, insights giving ROAS comfortably ≥ target → still
  `stands` (band computed from the sample, no direction fire).
- **Builder, ROAS goal but no `target_roas` → stands:** the guard that keeps the existing tests green;
  pin it so a future refactor can't silently start refuting when no target is configured.
- **Builder, install-goal below-implied-cost → NOT direction-refuted:** extend / mirror
  `test_enable_ads_install_goal_grounds_on_cost_per_install`; assert the enable op carries
  `action_type == "enable_ad"` but `review["verdict"] != "refuted"` (it stays `stands`/`low`) — the
  direction rule is ROAS-only.
- **Cold-ad abstain unaffected:** confirm `test_enable_ads_cold_ad_abstains_and_gate_blocks_turn_on`
  still yields `insufficient` (metric_value None → direction no-op) and the apply gate still blocks the
  approved cold enable. (Add an assertion if you add the `target_roas` to its policy; otherwise just
  confirm it stays green.)
- **Precedence — below-target AND below-floor → insufficient wins:** a below-target ROAS on a
  below-floor sample yields `insufficient` (rank 3) over `refuted` (rank 2), and the apply gate still
  blocks it. Pin this so the most-conservative-wins ordering is explicit for enables.
- **Operator override survives refutation:** a refuted enable that the operator manually sets to
  `approved` with a *grounded* (non-abstain) band is **not** blocked by `apply_ops_plan` (the gate keys
  on grounding, not `review_verdict`) — proves `refuted` is a warning, not a hard block for ops.

### Phase 3 — validate
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q 2>&1 | tee /tmp/enable-dir.log` — all
  green. (No ruff/mypy/pyright is configured; pyproject declares only pytest.)
- If any pre-existing, clearly-unrelated failure surfaces, follow the `.pre-existing-error.md` protocol;
  do not chase it inside this ticket.

## Edge cases & interactions

- **Cold ad / no-metric enable:** `evidence["metric_value"]` is `None` → the top-of-function `roas is
  None` guard returns `None` → no direction fire. Must remain `insufficient` (cited zero sample below
  floor), never flip to `refuted`.
- **Install-goal enable:** `metric_name == "cost_per_app_install"` ≠ `"blended_roas"` → no direction
  fire even though `action_type == "enable_ad"`. Stays `low`/`stands`.
- **ROAS goal, no `target_roas`:** `target is None` → no fire. (Guards the existing green tests.)
- **Exactly at target** (`roas == target`): strict `<` → not refuted (mirrors the scale-up branch).
- **Verdict precedence:** `refuted` (rank 2) beats a `window_length`/`band_earned` `downgrade` (rank 1)
  but loses to `insufficient` (rank 3); `_resolve` already does most-conservative-wins — confirm the
  enable path obeys it.
- **Idempotency:** an enable op that already carries a `review` block is left untouched by
  `review_ops_plan` (existing invariant) — a second review pass must not double-append the direction
  reason.
- **Input not mutated:** `review_ops_plan` deep-copies; the caller's plan must be untouched (existing
  invariant — keep the assertion in the new refuted test).
- **`action_type` leakage:** adding `action_type` to the op must not perturb `control.validate_op`,
  `_build_request`, the apply path, or `briefs` rendering. Budget ops already carry `action_type`
  harmlessly, and `briefs` counts `action_type` only over `plan["actions"]`, never `plan["ops"]` —
  verify (grep) that nothing else branches on an op's `action_type` before relying on this.
- **Pause ops untouched:** do not add `action_type` to `build_pause_plan`; the pause-winner direction
  is out of scope and is the safe direction.
