description: Write down the rule that, before a recommendation reaches the operator, a second agent with fresh eyes must try to prove it wrong using the same data — checking the things a calculator can't (does it clash with what we already know about this account? was the time window cherry-picked? is a plainly-written recommendation actually grounded?) — and must downgrade or drop the call when it can't survive the challenge.
prereq: adversarial-review-gate, grounding-rules-and-external-evidence
files: AGENTS.md, knowledge/README.md
difficulty: medium
----
## Why

The `adversarial-review-gate` ticket builds the **code** adversary — the reliable judge of the
arithmetic/structural refutations (sample floor, window length, causal cap, band-earned, direction).
But several of the refutations the parent ticket asks for are **semantic** and can't be made by code:
does a recommendation contradict a narrative learning in the knowledge base? Is the cited window
cherry-picked relative to a *known relearning period* recorded in the decision log? Is a free-text
prose recommendation (the kind `analyze.py` emits, where no schema enforces grounding) actually
earned? Is a web finding being treated as confirmation instead of a hypothesis?

This ticket is **documentation-only** — it adds the enforceable agent rule for a **fresh-context LLM
adversarial pass**, exactly the way `grounding-rules-and-external-evidence` is the doc mirror of the
confidence code. The repo has no LLM-invocation code (it produces reports for an agent/human to read),
so this layer is realized as a procedure the agent follows, not Python. It must be written AFTER the
gate and the grounding rules land, so it describes the system as actually built (hence the prereqs).

## What to write

### 1. The adversarial-review rule (AGENTS.md)

Extend the "Guardrails" / "Interpretation Rules" section with a rule of roughly this shape:

> **Before any pause/scale/budget recommendation is finalized in the operator brief, it must survive
> a fresh-context adversarial pass.** A reviewer — given ONLY the recommendation and its cited
> evidence (metric / window / sample / entity / band), NOT the reasoning that produced it — tries to
> *refute* it. The reviewer is conservative: when uncertain it **downgrades or refutes, it does not
> pass**. Every verdict must name the specific input that fails (a vague "looks fine" is not a
> verdict). Verdicts: **stands / downgrade (with the new band) / refuted / insufficient (abstain)**.
> Refuted or downgraded calls are corrected or dropped before the operator sees them.

State plainly that the **deterministic checks are already enforced by `review.py`** (cite it), and
that the agent's job in this pass is the refutations code can't make — the semantic ones below. The
agent must never weaken the guarded-write gate or PAUSED-by-default; this pass only filters what gets
proposed, it never approves anything.

### 2. What the fresh-context reviewer checks (AGENTS.md + knowledge/README.md)

The semantic refutations, each requiring the reviewer to name the failing input:

- **Contradicts the knowledge base.** Does the call conflict with a learning in
  `knowledge/learnings.md` or a decision in `accounts/<slug>/decision-log.md`? (e.g. recommending a
  tactic a logged experiment already refuted.) → refute or downgrade, cite the conflicting entry.
- **Cherry-picked window.** Is the cited window unusually short or positioned over a *known relearning
  / recently-changed period* (cross-check `decision-log.md` and the watch grace window)? The reviewer
  may **re-pull the same metric over a longer/standard window** (`account_metrics …`, the
  `regenerating_query` already on the evidence) to see whether the call flips — re-reading the same
  source is allowed; inventing a contradicting number is not. → downgrade with "window may be
  unrepresentative; widen the window."
- **Prose recommendations.** The narrative `next_7_day_actions` lines and any free-text analysis get
  the same treatment as structured actions — if a prose call lacks the four facts or its implied
  confidence isn't earned, downgrade/refute it.
- **External-as-confirmation.** If any web/external evidence is being treated as confirmation of a
  live call rather than a hypothesis, refute it and route it to `experiment define` (this enforces the
  external-evidence rule from `grounding-rules-and-external-evidence` at review time).
- **Confidence earned.** Independently sanity-check that the stated band is justified by the rubric
  inputs (the code gate does this arithmetically; the reviewer catches the cases that hinge on
  judgment the rubric can't encode).

### 3. The anti-rubber-stamp structure (both docs)

Because the reviewer is itself an AI and could rubber-stamp, the rule must mandate the structural
mitigations (mirror the TESS adversarial-reviewer):
- **Fresh context** — give the reviewer only the recommendation + cited basis, never the producing
  conversation/reasoning.
- **Refute-by-default / downgrade-when-uncertain** stance.
- **Name the specific failing input** — a verdict without a named input is not acceptable.
- **No fabricated data** — reason over the cited basis; re-pull the same metric to check, but never
  invent a contradicting number.

Note the option (per the parent ticket) to run this pass as a **TESS-style stage** so it has an audit
trail, and the **cost/materiality** guidance: review the calls that drive a pause/scale/budget action,
not trivial informational lines (the code gate already encodes this materiality threshold).

### 4. Knowledge-base pointer (knowledge/README.md)

Add a short subsection under "Confidence & evidence" pointing at this two-layer review: `review.py`
for the deterministic checks, this agent rule for the semantic ones, and that the same
🟢/🟡/🔴/⚪ vocabulary is used throughout — one confidence language, not two.

## TODO

- [ ] Add the adversarial-review rule to `AGENTS.md` (Guardrails/Interpretation Rules), citing
      `review.py` as the deterministic half and defining the four verdicts + the refute-by-default,
      name-the-failing-input, fresh-context discipline.
- [ ] Document the semantic checks (KB contradiction, cherry-picked window + allowed re-pull, prose
      recommendations, external-as-confirmation, confidence-earned) in `AGENTS.md`.
- [ ] Add the two-layer review pointer subsection to `knowledge/README.md` "Confidence & evidence."
- [ ] Re-read `AGENTS.md` + `knowledge/README.md` + the grounding-rules additions end-to-end for ONE
      consistent confidence vocabulary and no contradiction with what `review.py` actually does.

## Edge cases & interactions (to cover in the prose)

- **Reviewer must not rubber-stamp.** The fresh-context + refute-by-default + name-the-input structure
  is the mitigation; state it explicitly, don't just hope.
- **No fabricated data.** Re-pulling the same metric to check is allowed; inventing a contradicting
  number is a defect. Say so.
- **Must not weaken the guarded-write gate.** This pass only filters/downgrades proposals upstream;
  it never approves an action or enables a write, and PAUSED-by-default is untouched.
- **Abstention is blessed.** "Insufficient data to recommend — keep running" is a valid output; there
  must be no implicit pressure to always pass a call.
- **Doc-only, no behavior change.** No code is touched here. Write it against the landed behavior of
  `review.py` and the grounding rules — it must not describe checks the code doesn't implement or
  contradict the code's verdict taxonomy.
- **One vocabulary.** The band names/emoji and verdict words must match `review.py`, `confidence.py`,
  and `knowledge/README.md`; introduce no competing scale.
