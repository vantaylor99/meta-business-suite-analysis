description: Make budget changes work correctly when the budget lives at the campaign level (Meta's campaign-budget-optimization) instead of the ad set, support lowering budgets as well as raising them, and require facts + a confidence band + the second-opinion check on every budget move.
prereq: guarded-write-evidence-scaffold
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## NOTE — this is the largest/highest-risk ticket in the set

It bundles three coupled deliverables that the LOCKED scope ("CBO-aware budget +/-" + "fix the CBO
gap") requires together. Work the TODO **in order** and commit the CBO-fix + campaign-redirect first
(it is the locked gap-fix), then the decrease path, then grounding, then the actions.py parity fix
last. If time pressure forces a cut, the CBO-detection/redirect must land; the decrease path is the
next-most-important. Do not start the decrease path until the CBO redirect passes its tests.

## Why

Two locked requirements converge here: (1) **fix the CBO budget-level gap** and (2) deliver
reversible **CBO-aware budget +/-** as a grounded write. Today:
- `control.set_daily_budget` (`control.py` ~line 320, `_build_request`) re-reads the entity's
  `daily_budget`, and if it is None/0 it **refuses** with "entity has none — likely lifetime/CBO
  budget; not changing it." It only caps increases, and has no decrease path. The increase cap is
  read from the **per-op param** `params["max_increase_percent"]` (default 20) — NOT from `config.py`
  and NOT from the account registry. (The `max_budget_increase_percent` field in
  `config/meta_ads_accounts.json` exists but is NOT currently read by the ops path.)
- `actions.increase_adset_budget` + `actions._populate_budget_params_from_live_state` read only the
  **ad set's** `daily_budget` and silently block when CBO is active (budget lives on the campaign),
  because they never inspect the campaign budget.

Under CBO the campaign holds the daily/lifetime budget and ad sets inherit it. The current code
can't tell "ad set has no budget because it's broken" from "ad set has no budget because CBO is on."

## What to build

### CBO detection (the fix — land + commit FIRST)

When an ad-set budget op finds the ad set has no `daily_budget`, fetch the parent campaign's
`daily_budget` + `lifetime_budget` (via the reader: `get_campaign(campaign_id, fields=['daily_budget',
'lifetime_budget'])`). Classify:
- **CBO active** (campaign has daily or lifetime budget, ad set has none) → do NOT silently block.
  Surface it: mark the ad-set budget op non-executable with a clear note ("CBO active: budget is at
  the campaign — increase/decrease the campaign budget instead") and emit/route a **campaign-level**
  budget op as the actionable alternative.
- **Truly broken** (neither campaign nor ad set has a budget) → block with the existing clear error.
- **Ad-set-level budget present** → proceed as today.

Record the CBO classification on the op/action (e.g. `cbo_detected: true`, `live_campaign_state`) so
the operator/audit log sees why the ad-set op was redirected.

### Campaign-level budget op

`control.OP_LEVELS` already lists `set_daily_budget: {adset, campaign}` (verified) and
`_update_entity` already dispatches the campaign path via `client.update_campaign`. Ensure
`_build_request` handles the campaign path with the same cap logic, and the CBO redirect produces a
valid campaign op. Use `control` ops, not a new actions type, to avoid duplication; only add a new
action-plan variant if genuinely needed (follow the `howToAddOp` template) — prefer reusing
`set_daily_budget@campaign`.

### Budget DECREASE (reversible control)

Locked scope includes "CBO-aware budget +/-". Today only increases are capped/allowed. Add a
decrease path:
- Allow lowering `daily_budget` (ad set or campaign) with a **floor guard** so it can't be set to a
  destructive/near-zero value — add `MIN_DAILY_BUDGET_CENTS` and `MAX_BUDGET_DECREASE_PERCENT` to
  `config.py` (these land in `write-config-registry-controls`; this ticket WIRES them — do not assume
  any decrease cap is read today, because the increase cap is op-param-driven, not config-driven).
  Validate against the live current budget.
- Select the cap by sign of (new - current): increase uses the existing increase cap (op-param
  `max_increase_percent`); decrease uses `MAX_BUDGET_DECREASE_PERCENT` (op-param override allowed,
  falling back to the config default, and to the per-account `max_budget_decrease_percent` from the
  registry if present). Decreases also must satisfy the absolute `MIN_DAILY_BUDGET_CENTS` floor.
- Decreases are reversible (no spend risk beyond reduced delivery) — same evidence/confidence/review,
  but a separate symmetric guard, not the increase cap.

### Grounding (every budget move)

Attach `Evidence` (the metric justifying the budget move — ROAS/cost-per-result over the window,
sample, regenerating query) + computed `Confidence` via the scaffold's `attach_op_grounding`, and run
through `review`. A below-floor sample abstains → non-executable (no confident budget swing on thin
data — the "9 purchases over 5 days" guard). For the `direction` review check to fire (refute a
scale-up whose cited ROAS is below target / a scale-down of a clear winner), the op must supply the
`action_type`-equivalent the gate reads (see scaffold ticket: ops lack `action_type`, so set it for
budget ops or add an explicit direction guard here).

## TODO (work in this order)

1. Add CBO detection: on a missing ad-set `daily_budget`, read parent campaign budget via the
   reader; classify CBO-active vs broken vs adset-level; record `cbo_detected`/`live_campaign_state`.
2. Redirect a CBO-active ad-set budget op to a campaign-level `set_daily_budget` op (non-executable
   ad-set op + actionable campaign op), with operator-facing note. Implement campaign-level
   `_build_request` cap logic (reuse the adset cap path; `_update_entity` campaign dispatch already
   exists). COMMIT after this passes its tests.
3. Fix `actions._populate_budget_params_from_live_state` to detect CBO (Option A from research:
   executable=False + clear note) so the action-plan path no longer silently blocks. Add a parity
   test that `increase_adset_budget` (actions) and `set_daily_budget` (ops) classify the same fixture
   identically.
4. Add the decrease path: read `MIN_DAILY_BUDGET_CENTS` + `MAX_BUDGET_DECREASE_PERCENT` from config
   (+ optional per-account/op-param override), select cap by sign, validate against live budget.
5. Wire evidence/confidence via `attach_op_grounding`; run through `review`; supply the direction
   `action_type`-equivalent for budget ops.
6. CLI: budget proposer(s) accept increase OR decrease and surface CBO redirect.
7. Tests (mock-only): CBO-active adset → redirected to campaign op + ad-set op non-executable;
   truly-broken → blocked; adset-level present → normal increase; decrease within floor → ok;
   decrease below floor → blocked; over-cap increase → blocked; over-cap decrease → blocked; thin
   sample → abstain/non-executable; review refutes scale-below-target; actions/ops CBO parity. Use
   FakeMetaReader/FakeClient with a campaign carrying budget and an ad set without.
8. Update `docs/META_ACTION_WORKFLOW.md` budget section (CBO behavior + decrease + floors).
9. `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Lifetime-budget CBO** — campaign has `lifetime_budget`, not `daily_budget`. Detection must treat
  EITHER as "CBO active." A daily-budget cap can't be applied to a lifetime budget; the redirected
  campaign op must operate on whichever budget type is live, or block with a clear "lifetime budget —
  not adjustable via daily-budget op" message. Decide and test both.
- **Decrease to zero / below Meta minimum** — Meta rejects budgets below a per-currency minimum;
  `MIN_DAILY_BUDGET_CENTS` should be conservative, and validate_only will surface Meta's own floor.
  Don't let a decrease silently pause delivery.
- **Currency/units** — budgets are in account-currency minor units; the existing code treats
  `daily_budget` as integer cents. Document the assumption and don't break non-USD accounts (the
  registry has per-account currency via `get_account`).
- **Cap direction confusion** — applying the increase cap to a decrease (or vice versa) would wrongly
  block a valid move. Keep the two caps separate and select by sign of (new - current). Test both
  directions.
- **Increase cap source** — the existing increase cap is the op-param `max_increase_percent`, NOT
  config. When you add the decrease cap, do not accidentally change the increase cap's source; keep
  increase behavior byte-identical except where CBO redirect changes the target level.
- **CBO redirect provenance** — the redirected campaign op must carry its OWN evidence/confidence
  (campaign-level metric), not copy the ad set's; the ad-set op stays non-executable with the note.
  Audit log must show both.
- **Re-read drift** — budget may change between propose and execute; `_build_request` re-reads live
  budget to cap against. Confirm the CBO classification also re-reads at execute, not just at propose,
  so a campaign that flipped CBO state isn't mis-handled.
- **Action-plan vs ops parity** — after the `actions.py` CBO fix, `increase_adset_budget` in the
  action plan and `set_daily_budget` in the ops plan must agree on CBO behavior; a test asserts both
  classify the same fixture the same way.