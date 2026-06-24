description: Before a recommendation reaches the operator, have a second agent with fresh eyes try to prove it wrong using the same data — checking whether the sample is big enough, the window wasn't cherry-picked, the claim isn't a correlation dressed up as cause, and the stated confidence is actually earned. Calls that can't survive the challenge get downgraded or dropped, so plausible-but-wrong advice doesn't slip through.
prereq: grounded-recommendations
files: src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/analyze.py, AGENTS.md, knowledge/README.md
----
## Why

Don't trust self-reported analysis. We already saw the value concretely: the TESS code-review stage,
given the diff with fresh eyes, caught a weak test the implementer had written. The same principle
applies to *recommendations* — an agent that produced a recommendation is the worst judge of whether
it's grounded. A fresh-context adversary whose only job is to **refute** each call, using the same
underlying data, catches the plausible-but-wrong recommendation before the operator (or a less-
skeptical MTC specialist) acts on it.

This is the verification layer that sits on top of `grounded-recommendations`: that ticket makes
recommendations carry their evidence + confidence; this ticket stress-tests them.

## What this is

A review pass over the set of recommendations + their cited basis (the structured output from
`grounded-recommendations`), run with **fresh context** — given only the recommendation and its
evidence, NOT the reasoning/conversation that produced it (the same isolation the TESS
adversarial-reviewer uses). For each recommendation the reviewer tries to refute it on concrete
grounds:

- Is the **sample** actually large enough, or is this below the floor (should it abstain)?
- Is the **window** cherry-picked — would a longer/standard window change the call?
- Is it **causal-from-correlational** — asserting cause where only correlation exists?
- Does it **contradict the knowledge base** or prior evidence?
- Is the **stated confidence earned** by the rubric inputs, or inflated?
- Is any **external/web** evidence being treated as confirmation rather than hypothesis?

Output: a per-recommendation verdict — **stands / downgrade (with new band) / refuted / insufficient
(abstain)** — each with the specific reason and the rubric input that fails. Refuted or downgraded
calls are corrected or dropped *before* they reach the operator brief. The reviewer is conservative:
when uncertain, it downgrades or refutes rather than passing.

The reviewer can be realized as a subagent pass (the existing `adversarial-reviewer` agent type is a
natural fit) and/or a step in the brief-generation flow — the plan stage decides the mechanism. It
may also run as a TESS-style stage so it has an audit trail.

## Use cases / expected behavior

- A "scale this — it's our winner" call backed by 9 purchases over 5 days is refuted ("below the
  25-purchase floor; should abstain") and never reaches the brief as a confident call.
- A "pause, ROAS 1.1" call measured over a 3-day window during a known relearning period is
  downgraded with "window may be unrepresentative; recommend wider window" rather than passed as-is.
- A causal claim from a correlational read is downgraded to the capped grounding band and tagged
  "confirm via A/B," with an offer to file the experiment.
- A clean, well-sampled, direct-observation call passes ("stands") with its confidence intact.

## Edge cases & interactions

- **The reviewer is itself an AI and could rubber-stamp.** Mitigate structurally: give it ONLY the
  recommendation + cited basis (fresh context, not the producing reasoning), instruct a
  refute-by-default / downgrade-when-uncertain stance, and require it to name the *specific* rubric
  input that fails — a vague "looks good" is not an acceptable verdict (mirror the TESS review rule).
- The reviewer must not fabricate new data; it reasons over the cited basis and may re-pull the same
  metric to check, but it cannot invent a contradicting number.
- Must not weaken the guarded-write gate or PAUSED-by-default — this sits upstream, filtering what
  gets proposed.
- Depends on `grounded-recommendations`: it reviews the structured recommendation + confidence band,
  so that representation must exist first (hence the prereq).
- Keep it read-only with respect to Meta (it may re-read metrics; it makes no account writes).
- Cost/latency: a per-recommendation agent pass adds tokens — the plan stage should consider
  batching or only reviewing recommendations above a materiality threshold (e.g. those that drive a
  pause/scale/budget action), not trivial informational lines.
