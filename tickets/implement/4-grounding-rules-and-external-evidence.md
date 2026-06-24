description: Write down the rules so the discipline holds even when a human or the agent writes advice in plain prose (where no code enforces it): every recommendation must cite its number, time window, sample size, which ad, and a High/Medium/Low confidence band with the reason — and findings from the internet are treated only as "worth testing" hypotheses, never as proof about this account.
prereq: confidence-core, confidence-actions-analyze, confidence-monitor-experiment, confidence-operator-brief
files: AGENTS.md, knowledge/README.md, knowledge/learnings.md
difficulty: medium
----
## Why

Not every recommendation comes from a code path — many are written by the agent in narrative
analysis, where there is no schema to enforce grounding. This ticket adds the **procedural,
enforceable rules** (the section-4 + section-5 halves of the parent ticket) so the same discipline
the code now enforces also binds free-text analysis, and so external/web evidence can never
masquerade as account-grade truth. It is documentation-only; it should be written AFTER the code
tickets land so it describes the system as actually built (hence the prereqs).

## What to write

### 1. The grounding rule (AGENTS.md + knowledge/README.md)

Add a short, enforceable rule — the human/agent-facing mirror of the structural rule the code now
enforces:

> **Every operator-facing recommendation — including prose — must cite metric / window / sample
> size / entity, AND a confidence band (🟢 High / 🟡 Medium / 🔴 Low) with its rationale (data
> strength + grounding tier). When the data floor isn't cleared, abstain — say "insufficient data
> — keep running," never invent a winner/loser.**

- Put the operator-contract version in `AGENTS.md` (it already has an "Interpretation Rules" /
  "Guardrails" section — extend those; today it says "Do not infer confidence not supported by the
  exported data" and "Do not claim causal certainty from export data alone" — make those concrete
  with the band + four-facts requirement).
- Point at `confidence.py` as the canonical computation and `knowledge/README.md`'s "Confidence &
  evidence" rubric as the shared vocabulary — ONE confidence language, not two.
- State the two axes plainly: **data strength** (sample/significance/recency) and **grounding tier**
  (A/B-backed > direct observation > correlational > external > model-inference), and that the
  **weaker axis caps** the band. Name the **causal-language guard**: a recommendation asserting
  cause from non-experimental data is labeled "correlational — confirm via A/B" and downgraded.

### 2. External-evidence rules (knowledge/README.md)

Add an "External evidence (the web) is a hypothesis source, never a confirmation" subsection:

- **Account data answers "is this true for THIS account?"; external evidence answers "what's worth
  trying?"** The hard rule: external findings feed the **hypothesis / experiment queue, never the
  confidence score of a live recommendation.** "Square video wins in Reels" → file an A/B via
  `experiment define`, not "+confidence on this ad."
- **Grounding tier, capped:** `external` sits below every first-party source and above only
  `model_inference`; because the weaker axis caps the band, a web-only recommendation reads at most
  🔴 Low / 🟡 Medium — usually "🔴 Low (hypothesis — confirm via A/B)."
- **Cite the source, quote don't paraphrase:** a link + a date + a verbatim quote of the key claim.
  An external claim with no citable source is not usable. (The model summarizing the web is itself a
  hallucination surface — quoting blocks invented "consensus.")
- **Recency-weighted, NOT upvote-weighted:** upvotes measure gameable popularity, never a confidence
  multiplier — at most a weak tie-breaker. For Meta, **recency dominates**: an old high-upvote post
  about a **platform tactic** is a trap; distinguish fast-rotting **platform tactics** from
  slow-rotting **evergreen principles** and weight accordingly.
- **Source-quality tiers:** Meta's own docs / a named practitioner showing methodology rank above an
  anonymous "this worked for me" — but even Meta docs describe *general* behavior, not this account.
- **Cold-start exception (where it earns its keep):** when the account has NO data on something new
  (new creative direction, ad type, audience), a labeled external prior may inform the *initial*
  call (labeled `external`, capped Low/Medium). The moment first-party data exists, account data
  dominates and the confidence is recomputed from it.

### 3. Reconcile the existing learnings entry

`knowledge/learnings.md` already contains the "practitioner consensus (Jon Loomer, Meta Help)" line
inside the creative-enhancements entry — that IS external evidence used informally. Add a short note
(or relabel) so it is explicitly tagged `external`, capped, and flagged as "confirm via A/B,"
demonstrating the new convention on a real entry rather than only describing it abstractly. Do not
rewrite the learning's substance; just apply the label.

## TODO

- [ ] Extend `AGENTS.md` Interpretation Rules + Guardrails with the four-facts + confidence-band +
      abstain requirement and the causal-language guard, pointing at `confidence.py` + README rubric.
- [ ] Add the "External evidence is a hypothesis source, never confirmation" subsection to
      `knowledge/README.md` (tier/cap, cite+quote, recency-not-upvotes, source tiers, cold-start).
- [ ] Tag the existing practitioner-consensus line in `learnings.md` as `external` / confirm-via-A/B.
- [ ] Re-read the three docs end-to-end for one consistent confidence vocabulary (no second scale).

## Edge cases & interactions (to cover in the prose)

- **External must never raise a live recommendation's confidence** — it is capped in the grounding
  tier and routed to `experiment define`. A path that treats a web finding as confirmation is a
  defect; say so explicitly.
- **No citable source ⇒ unusable.** Require link + date + verbatim quote for any external claim.
- **Upvotes are not a multiplier.** Recency + source-quality are the only weighting signals;
  platform tactics decay faster than evergreen principles.
- **Abstention is blessed.** The rules must explicitly permit "insufficient data to recommend" as a
  valid output so there is no implicit pressure to always produce a recommendation.
- **One vocabulary.** The band names/emoji in AGENTS.md must match `knowledge/README.md` and
  `confidence.py`; don't introduce a competing scale.
- **Doc-only, no behavior change.** No code is touched here; this ticket must not contradict what
  the code tickets actually implemented (write it against the landed behavior — hence the prereqs).
