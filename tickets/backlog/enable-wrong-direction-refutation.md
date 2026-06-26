description: When turning an ad back on, the system can present a confident-looking proposal even though the ad's own numbers show it loses money against the account's goal — there is no check that flags "you're enabling a known loser."
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/review.py
difficulty: medium
----
## Problem

`control.build_enable_ads_plan` attaches a **computed** confidence band to each `set_status=ACTIVE`
op, and `review.review_ops_plan` re-derives that band from the cited sample. That protects against an
*over-confident* enable (the band can never exceed what the sample strength supports). It does **not**
protect against a **wrong-direction** enable.

The review gate's direction check (`review._direction_contradiction`) only fires when an op carries
both an `action_type` and the account policy supplies `target_roas`. Enable ops carry **no**
`action_type`, so the check no-ops on them. The consequence:

- Re-enabling an ad whose cited ROAS is clearly **below** the account's `target_roas`, on a
  high-spend / statistically-strong sample, produces a `medium`/`high` band and a `stands` verdict.
- The evidence block does show the real (below-target) ROAS, so an attentive operator *can* see it —
  but nothing actively refutes or down-ranks the proposal the way scaling a below-target entity is
  refuted for budget ops.

Enabling an ad is directionally a scale-up (0 → live). For a ROAS-goal account, turning ON an ad
whose own number sits below target arguably contradicts the goal in the same way a budget increase
does. The current behavior treats "the metric is statistically real" as the only axis, not "the
metric points the right way."

## Why this is a decision, not just a fix

It needs a product/semantics call before any code:

1. **Is a below-target enable actually wrong-direction?** A below-target ad might legitimately be
   re-enabled (a deliberate retest, a seasonal ad, a creative being rotated back in). A hard
   refutation could block valid operator intent. A softer down-rank (cap the band) may be the right
   strength instead of a `refuted` verdict.
2. **What "action_type-equivalent" does the gate key on?** The direction check is written around
   `_SCALE_ACTIONS` / `action_type`. Enables would need either a synthetic action_type (e.g.
   `enable_ad` mapped into a new direction set) or a new enable-specific direction rule. That choice
   ripples into `review._direction_contradiction` and the `_SCALE_ACTIONS` taxonomy.
3. **Install-goal accounts.** The direction check is intentionally ROAS-only today. An enable
   direction rule would have to decide whether/how cost-per-install-goal enables are direction-judged
   (currently their band caps at `low` because the conversion sample is purchases — see the related
   systemic note in the review findings of `enable-and-set-status-write`).

## Expected behavior (to be settled in plan)

A re-enable of an ad whose cited goal-metric contradicts the account goal should not reach the
operator looking as trustworthy as a re-enable of a genuine performer — whether by a `refuted`
verdict, a band cap, or a clearly surfaced warning. The exact strength and the install-goal handling
are the open questions.

This was flagged in the implement handoff for `enable-and-set-status-write` as an accepted
interaction; the review pass confirmed it is real and routed it here for a deliberate decision rather
than fixing it inline (it is not a regression — pre-grounding, enables carried no direction check at
all).
