description: Make every recommendation the tool gives an operator show its evidence — the number, the time window, how much data it is based on, and which ad/ad set/campaign — AND a plain confidence indicator (e.g. a High/Medium/Low band with an approximate percentage) that tells the operator how much to trust the call and how likely it is to be an ungrounded guess. Let the tool say "not enough data to recommend yet" instead of guessing. The goal is that no advice can be acted on without the facts behind it, and the agent's confidence in it, being visible and checkable. External findings from the internet may be used as labeled, capped hypotheses that get routed to testing — never as a confidence boost to a live recommendation.
files: src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/analyze.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/experiment.py, AGENTS.md, knowledge/README.md
----
## Why

This repo will become a template for ~25 specialists who are less likely than the original
operator to catch a confident-but-ungrounded recommendation. The deterministic layer (metrics,
ROAS, p-values, the guarded write gate) is already trustworthy. The hallucination risk lives in
the **interpretive layer** — the sentence that turns a number into "pause this" / "scale this" /
"this will get you to 3.0 ROAS." Today a recommendation can be surfaced without the evidence
attached, and the tool is biased toward always producing an answer even on thin data.

This feature makes grounding **structural and checkable** rather than something the agent has to
remember to do.

## What this is

Two halves — a code half and a rules half. Resolve the design for both in the plan stage.

### 1. A structured, evidence-bearing recommendation

Every operator-facing recommendation produced by code (the operator brief, the proposed action
plan, the watch report classifications, the pause/enable plans) should carry, inline and
machine-readable, the basis for the call:

- **metric** — the value the recommendation rests on (e.g. ROAS 1.2, CPA $48)
- **window** — the date range the metric was measured over
- **sample size** — n purchases and/or spend over that window (the thing that says whether the
  metric is trustworthy)
- **entity** — the id + level (ad / ad set / campaign) the recommendation applies to

A recommendation that cannot populate these fields should not be emittable as a confident call.
The point is that a reader (or a later audit) can trace any "do X" back to the exact facts, and
re-derive them. Consider naming the regenerating query where practical (e.g. the
`account_metrics --level … --date-from … --date-to …` that reproduces the number).

### 2. Sample-size gating + abstention as a first-class verdict

Below a data floor, the tool must NOT call something a winner or a loser. This discipline already
exists in two places and should be generalized, not reinvented:
- `monitor.py` — the watch scanner's `min_spend` significance floor and the protective grace.
- `experiment.py` — the `min_conversions` (default 25) "needs more data" gate in `read_experiment`.

Scale / pause / budget recommendations below the floor should be labeled "promising test" or
"insufficient data — keep running," never "winner/loser." And "insufficient data to recommend"
must be a valid, explicitly-blessed output — removing the implicit pressure to always produce a
recommendation, which is a common source of fabrication.

### 3. A confidence indicator on every recommendation (how much to trust it)

This is the centerpiece. Every recommendation should carry a **confidence indicator** that tells
the operator, at a glance, how much to trust it — and specifically how likely it is that the call
is an ungrounded leap (a hallucination, or close to the threshold for one) rather than something
the data supports.

**Hard design constraint — the score must be computed, not felt.** The confidence value must be
derived from objective, mostly-deterministic inputs via a transparent rubric, and its contributing
factors must be shown alongside it. A number the model free-types ("73% confident") is itself the
exact kind of false precision / fabrication this whole ticket exists to remove — so that is not
allowed. If a code path cannot compute the inputs for a recommendation, that recommendation
abstains (section 2) rather than reporting a guessed score.

**Two axes — keep them separate; the weaker one governs.** Conflating them hides the real risk:

- **Data strength** — given the data, how reliable is the underlying number/effect? Driven by
  sample size (n purchases, spend vs the floor), statistical significance / interval width for
  comparative claims, and recency of the window. This is about the *data*.
- **Grounding strength** — how much of the recommendation is anchored to *observed data* versus
  *model inference* or *external claims*? Driven by an evidence tier, roughly:
  A/B-experiment-backed  >  direct API observation  >  correlational read  >  external "best
  practice"  >  model inference only. Downgraded when the recommendation asserts **causation** from
  non-experimental data. This is the *"how possible is it that this is hallucinated"* axis.

A recommendation can be statistically strong but weakly grounded (a confident causal story from a
large but correlational sample). The indicator must reflect the **weaker** axis — the grounding
axis should be able to **cap** the overall score, not be averaged away by a big sample.

**Presentation.** Headline as a **band with an approximate percentage range** — e.g. 🟢 High
(~80–100%), 🟡 Medium (~50–80%), 🔴 Low (<50%) — NOT a falsely precise single number like 73.4%,
with the contributing factors listed so the operator sees *why* it is a Medium and not a High.
**Reuse the existing knowledge-base confidence rubric** (🟢/🟡/🔴 with an evidence log; see
`knowledge/README.md` and `knowledge/learnings.md`) so the repo speaks ONE confidence language, not
two competing ones.

**Conservative and falsifiable.** When inputs are missing or ambiguous, the score rounds DOWN.
Each recommendation should carry a short "what would raise / lower this confidence" note (mirroring
the `would raise:` / `would lower:` lines already used in `learnings.md`), so the operator knows
what evidence would move it.

**Tie to abstention.** Below the data floor the indicator does not report a low percentage — it
flips to the "insufficient data — abstain" verdict from section 2. The confidence band is for
recommendations that clear the floor; everything under it abstains.

### 4. The rules half (procedural, for free-text analysis)

Not every recommendation comes from a code path — a lot are written by the agent in narrative
analysis. Add a short, enforceable rule to `AGENTS.md` and `knowledge/README.md`: any
operator-facing recommendation, including prose, must cite metric / window / sample size / entity
**and a confidence band with its rationale (data strength + grounding tier)**, and may abstain.
This is the human/agent-facing mirror of the structural rule above, so the discipline holds even
where there is no schema to enforce it.

### 5. External evidence (the internet) — a hypothesis source, never a confirmation

The tool may use external findings (Reddit, blogs, Meta's own docs, named practitioners) as input,
but they must occupy a fundamentally different role than account data. **Account data answers "is
this true for THIS account?" — external evidence answers "what's worth trying?"** The hard rule:
external evidence feeds the **hypothesis / experiment queue, never the confidence score of a live
recommendation.** If a post says "square video wins in Reels," the correct output is not "+confidence
on this ad" — it is "worth testing," which files an A/B via the experiment harness (`experiment.py`).
The web tells us what to test; the account tells us what's true; the A/B is the bridge.

How it slots into the design:

- **Grounding tier, at the bottom, capped.** In the section-3 grounding axis it sits below every
  first-party source: `A/B-backed > direct observation > correlational read > **external/web** >
  model inference`. Because the weaker axis caps the overall confidence, a recommendation grounded
  *only* in web evidence can never rise above Low/Medium no matter how popular the source — usually
  it reads as "🔴 Low (hypothesis — confirm via A/B)." This is the correct, conservative behavior.
- **Cite the source, quote don't paraphrase.** Same auditability we demand of account numbers: a
  link, a date, and a direct quote of the key claim — so the operator judges the source, and so the
  model summarizing the web (itself a hallucination surface) can't invent a "consensus."
- **Recency-weighted, NOT upvote-weighted.** Upvotes measure agreement/popularity at posting time
  and are gameable (marketing communities are semi-adversarial / full of self-promotion) — at most
  a weak tie-breaker, never a confidence multiplier. For Meta specifically, **recency dominates**:
  the platform changes constantly, so an old high-upvote post about a *platform tactic* is a trap
  (it may describe something that no longer works). Distinguish fast-rotting **platform tactics**
  from slow-rotting **evergreen principles** and weight accordingly.
- **Source-quality tiers.** Meta's own docs or a named practitioner who shows methodology rank above
  an anonymous "this worked for me" anecdote — but even Meta docs describe *general* behavior, not
  this account.
- **Where it earns its keep: cold-start.** External priors are most valuable when the account has
  *no* data on something new (a new creative direction, ad type, audience). There a labeled external
  prior beats pure model guessing. The moment first-party data exists, account data dominates.

Note: we already do this informally — the "practitioner consensus (Jon Loomer, Meta Help)" line in
`learnings.md` is exactly external evidence. The goal here is to formalize it so it is always
labeled `external`, capped, and routed to testing — so it can never masquerade as account-grade truth.

## Use cases / expected behavior

- A pause recommendation on an ad with 1.2 ROAS over 14d and 43 purchases reads, in the brief and
  the action plan, as a confident call WITH those four facts attached.
- The same ad with only 3 purchases and $40 spend over 4 days is surfaced as "insufficient data —
  keep running," not as "pause (loser)."
- An operator (or an auditor months later) can take any recommendation in a brief and re-run the
  named query to confirm the number behind it.
- A new specialist reading the output can see *why* every suggestion was made without trusting the
  agent.
- A pause backed by 14d / 43 purchases on a clean ROAS reads as **🟢 High (~85%)** with the factors
  that produced it (sample size, recency, direct observation) shown.
- A "scale this — it's our winner *because* of the new audience" claim drawn from a correlational
  read reads at most **🟡 Medium**, or **🔴 Low**, with "grounding: correlational — confirm via
  A/B" — *even if the sample is large* — because the grounding axis caps it.
- The same claim, once backed by a completed A/B experiment, reads **🟢 High** because the evidence
  tier is now top. The confidence visibly rises as the grounding improves.
- A Reddit thread saying "broad targeting beats lookalikes now" surfaces as a **hypothesis** —
  "🔴 Low (external, dated 2026-05; confirm via A/B)" with the link/quote — and offers to file an
  experiment, rather than nudging the confidence of any current targeting recommendation.
- For a brand-new ad type the account has never run, an external prior is allowed to inform the
  *initial* call (labeled external, capped Low/Medium); once the ad accrues spend/purchases, the
  first-party data takes over and the confidence is recomputed from it.

## Edge cases & interactions

- Recommendations where the metric genuinely has no sample (brand-new entity, zero spend) → must
  resolve to abstention, never a fabricated call.
- Must not weaken the existing guarded-write gate (proposed → approved → validate_only → execute)
  or the PAUSED-by-default behavior — this sits *upstream* of it, enriching what gets proposed.
- The watch scanner's protective grace for recently-changed ads must continue to win over a
  "pause" call (a young ad below the floor is "watch," not "urgent").
- Keep the change read-only with respect to Meta — this is about how recommendations are
  represented and surfaced, not about new account writes.
- Backward compatibility: existing report/brief consumers and tests should not break; if the
  recommendation representation changes shape, update the operator brief renderer accordingly.
- **The confidence score must never be a model-typed number.** It is computed from the rubric
  inputs; a code path that cannot compute them abstains rather than emitting a guessed score. This
  is the one edge case that, if gotten wrong, defeats the entire feature.
- High data strength must not mask weak grounding: a large-sample *correlational* claim asserting
  causation is capped by the grounding axis, never reported as High on sample size alone.
- No false precision — bands with approximate ranges, not two-significant-figure percentages.
- The confidence rubric must stay consistent with `learnings.md`'s 🟢/🟡/🔴 vocabulary; don't
  introduce a second, conflicting confidence scale.
- **External/web evidence must never raise the confidence of a live recommendation** — it is capped
  in the grounding tier and routed to the experiment queue. A code/agent path that tries to treat a
  web finding as confirmation (rather than hypothesis) is a defect.
- The model paraphrasing the web is a hallucination surface: require a link + date + verbatim quote
  for any external claim used; an external claim with no citable source is not usable.
- Upvotes/popularity must not be wired in as a confidence multiplier; recency + source-quality are
  the weighting signals, and platform tactics decay faster than evergreen principles.
