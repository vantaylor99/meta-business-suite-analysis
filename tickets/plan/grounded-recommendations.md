description: Make every recommendation the tool gives an operator show its evidence — the number, the time window, how much data it is based on, and which ad/ad set/campaign — and let the tool say "not enough data to recommend yet" instead of guessing. The goal is that no advice can be acted on without the facts behind it being visible and checkable.
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

### 3. The rules half (procedural, for free-text analysis)

Not every recommendation comes from a code path — a lot are written by the agent in narrative
analysis. Add a short, enforceable rule to `AGENTS.md` and `knowledge/README.md`: any
operator-facing recommendation, including prose, must cite metric / window / sample size / entity,
and may abstain. This is the human/agent-facing mirror of the structural rule above, so the
discipline holds even where there is no schema to enforce it.

## Use cases / expected behavior

- A pause recommendation on an ad with 1.2 ROAS over 14d and 43 purchases reads, in the brief and
  the action plan, as a confident call WITH those four facts attached.
- The same ad with only 3 purchases and $40 spend over 4 days is surfaced as "insufficient data —
  keep running," not as "pause (loser)."
- An operator (or an auditor months later) can take any recommendation in a brief and re-run the
  named query to confirm the number behind it.
- A new specialist reading the output can see *why* every suggestion was made without trusting the
  agent.

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
