description: For app-install accounts, the confidence rating on a recommendation only ever looks at purchase counts, which those accounts rarely have — so even a recommendation backed by lots of installs is stuck at a low rating and the installs are ignored.
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, config/meta_ads_accounts.json, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Problem

The confidence engine measures "do we have enough data" against a **conversions floor** of 25
*purchases* (`CONFIDENCE_CONVERSIONS_FLOOR`). When `confidence-actions-analyze` wired the engine
into the action plan, `evaluate_action_confidence` always sources `Evidence.sample_purchases` from
`ad["total_purchase_count"]` — for **every** account goal.

For an install-goal account (`primary_goal == "maximize_in_app_subscriptions"`, e.g. `pollen_sense`),
the commercial signal is in-app subscription **results** (and app **installs** as the secondary
fallback), not purchases. Those accounts typically report `total_purchase_count == 0`. So:

- the conversions floor (25 purchases) is structurally **never** cleared for install accounts;
- `data_strength` therefore falls to its "spend cleared but thin on conversions → cap at `low`"
  branch whenever spend clears the floor, and to `abstain` when spend is also thin;
- an install-goal pause/scale **can never read above `low` confidence**, no matter how many
  installs or subscriptions accrued;
- the `evidence` block shown to the operator is internally inconsistent: `metric_name` /
  `metric_display` say cost-per-install (the metric the call rests on), but `sample_purchases`
  reflects purchases (usually `None`/0) — the installs that actually back the call are dropped.

Observed today (build_action_plan for `pollen_sense`, an ad with **80 installs / $250 spend**):

```
band: low
metric: cost_per_app_install | cost/install ...
sample_purchases: None        # 80 installs ignored
factors: ['sample: $250 spend cleared but only n/a purchases (< 25) — thin on conversions', ...]
```

This is **not a safety hole** — the direction is conservative (under-confidence, never
over-confidence), and the abstention guard cannot wrongly fire on a real high-waste pause because
`waste_status == "high"` already requires spend ≥ `MIN_WASTE_SPEND`. But it makes the confidence
band effectively meaningless for ~half the managed accounts, which defeats the point of wiring
confidence into the action plan for install-goal accounts.

## What needs deciding (the design question)

Which conversion count should ground significance for an install-goal account?

- **in-app subscription results** (`total_results`) — the account's *primary* commercial signal per
  `AGENTS.md` / `docs/META_ACTION_WORKFLOW.md`; or
- **app installs** (`total_app_installs`) — the documented *secondary* fallback when results are
  sparse; or
- a goal-aware choice (results first, installs as fallback), mirroring how waste/scale detection
  already prioritizes results then installs.

There is also a naming question: `Evidence.sample_purchases` / `data_strength(sample_purchases=...)`
is really "sample conversions." If the floor becomes goal-aware, consider renaming to
`sample_conversions` (and a matching conversions-floor-per-goal) so the field name stops lying for
install accounts. That touches `confidence.py` (reviewed/accepted `confidence-core`), so it needs a
deliberate call rather than a drive-by rename.

## Acceptance criteria (once the design call is made)

- An install-goal action grounds its confidence on the goal-appropriate conversion count, so a
  well-sampled install-goal pause/scale can read `medium`/`high` on its real signal.
- The `evidence` block's sample field reflects the same conversion type the band was computed from
  (no more "cost/install metric, purchases sample" mismatch).
- Tests cover an install-goal account reaching above `low` on genuine install/subscription volume,
  and an install-goal abstain when that conversion count is genuinely thin.
- `docs/META_ACTION_WORKFLOW.md` "Evidence and Confidence" section updated to state how
  significance is grounded per goal.

## Context / provenance

Introduced (faithfully and conservatively) by ticket `confidence-actions-analyze`; surfaced during
its review. The review left it as a design question rather than an inline fix precisely because
"which conversion grounds an install account" is a product decision, not a mechanical change.
