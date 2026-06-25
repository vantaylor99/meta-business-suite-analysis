description: The written rules are now in place so that every recommendation — even ones an agent types in plain prose — must show its evidence and a trust band, and advice found on the web is treated only as "worth testing," never as proof about this account.
files: AGENTS.md, knowledge/README.md, knowledge/learnings.md, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped (implement stage)

**Documentation-only.** No code was touched. The prose mirror of the confidence engine
(`confidence.py`) now binds free-text analysis too, and external/web evidence is fenced off so it can
never read as account-grade truth. Three files changed:

### 1. `AGENTS.md` — the operator-contract grounding rule
- **Source Hierarchy** line ("Do not infer confidence not supported by the exported data") made
  concrete: every recommendation states a band (🟢/🟡/🔴/⚪) computed from sample/recency/grounding;
  below the floor, abstain instead of guessing a low %.
- **Interpretation Rules** gained a new **"Grounding rule"** block: cite **four facts**
  (metric · window · sample · entity), carry a **confidence band with rationale**, the **two axes**
  (data strength × grounding tier with `ab_experiment > direct_observation > correlational > external
  > model_inference`), **weaker axis caps**, **abstain when below floor** (25 conversions / $100 spend),
  and the **causal-language guard** (assert cause from non-A/B data → "correlational — confirm via
  A/B", downgrade one band). Points at `confidence.py` (canonical computation) + README rubric (shared
  vocabulary); explicitly "one language, never a second scale."
- **Guardrails**: the "no causal certainty" rule made concrete (apply the causal guard), plus a
  four-facts/abstain guardrail and an "external evidence never raises a live recommendation's band"
  guardrail.

### 2. `knowledge/README.md` — two new subsections after "Confidence & evidence"
- **"Grounding tiers (how causal is the evidence?)"** — states the two axes, a table mapping the
  code's `EvidenceTier` → its `_TIER_CEILING` band (ab_experiment/direct_observation → High,
  correlational → Medium, external → Low, model_inference → Low), weaker-axis-caps, and the causal
  guard. Added to keep the README's existing "Evidence strength" ladder and the code's `EvidenceTier`
  as **one** vocabulary rather than two.
- **"External evidence (the web) is a hypothesis source, never a confirmation"** — the hard rule
  (external → `experiment define` queue, never a live recommendation's confidence score; a path that
  treats web as confirmation is a defect), the **capped grounding tier** (at most 🔴 Low), **cite +
  verbatim quote** (no citable source ⇒ unusable), **recency- not upvote-weighted** (platform tactics
  rot fast, evergreen principles slowly), **source-quality tiers** (Meta docs / named-methodology
  practitioner > anonymous, but even Meta docs describe general behavior not this account), and the
  **cold-start exception** (labeled `external`, capped Low, recomputed from first-party data the
  instant it exists).
- Updated the forward-pointer that previously said these rules "land in the
  `grounding-rules-and-external-evidence` ticket" → now "are in the two subsections that follow."

### 3. `knowledge/learnings.md` — applied the new label to a real entry
- The existing "practitioner consensus (Jon Loomer, Metalla, Meta Help)" evidence line inside the
  creative-enhancements learning is now tagged `_(**external** evidence — grounding tier `external`,
  **capped 🔴 Low** … confirm via A/B …)_`. **Substance unchanged** (per the ticket: relabel, don't
  rewrite). The tag also clarifies the line does **not** prop up the entry's 🟡 Medium band (that rests
  on the first-party own-account reads) and notes it lacks a citable link+quote.

## How to validate

- **Tests:** `.venv/bin/python -m pytest tests/ -q` → **123 passed** (unchanged; doc-only). The
  relevant pin is `test_band_vocabulary_actually_appears_in_knowledge_readme` (test line ~3288) — it
  reads the README and asserts each band's emoji **and** label is present. Still green after the edits.
- **One-vocabulary spot check:** grep the three docs for band emoji/labels — they must match
  `BAND_PRESENTATION` in `confidence.py` exactly (🟢 High / 🟡 Medium / 🔴 Low / ⚪ Insufficient data
  — abstain). No competing scale (e.g. a 1–5 score, a different emoji set) should appear.
- **Tier-cap correctness:** the README table's ceilings must match `_TIER_CEILING` in `confidence.py`
  (confidence.py:54-61). In particular `external → Low` and `model_inference → Low`.
- **Read-for-contradiction:** confirm no doc claims external can raise a live band, or that abstention
  is discouraged, or that a recommendation may ship without its four facts.

## Use cases the rules must cover (review against these)

- A prose recommendation with a number but **no entity/sample** → should be called a guess by the rule.
- A **below-floor** ad (e.g. 3 purchases / $40) → "insufficient data — keep running," never a winner.
- A **large correlational** claim → capped 🟡 Medium, not High (weaker axis governs).
- A **causal** prose claim ("X drives ROAS") from non-A/B data → "correlational — confirm via A/B."
- A **web finding** ("square video wins in Reels") → `experiment define`, capped 🔴 Low, never +confidence.
- An **uncited** web claim → unusable (no link/date/quote).
- A **cold-start** new ad type with no account data → labeled `external` prior, recomputed once data lands.

## Known gaps / judgment calls for the reviewer

- **Deliberate divergence from the parent ticket's wording: external caps at 🔴 Low, not "Low/Medium."**
  The parent ticket twice said external is "capped Low/Medium," but the landed `_TIER_CEILING[external]
  = Band.low` means `combine_bands(data, external) = min(data, low) ≤ low` — it can **never** reach
  Medium. The ticket explicitly instructed writing against landed behavior ("must not contradict what
  the code tickets actually implemented"), so the docs say **at most 🔴 Low**. Reviewer: confirm you
  agree the code (Low) is authoritative over the ticket's looser phrasing. If product actually wants
  external to be able to reach Medium, that is a `confidence.py` change (raise the ceiling) and belongs
  in a new fix/plan ticket — **not** a doc edit here.
- **The grounding-tier vocabulary is NOT pinned by a test** (only the *band* vocabulary is, via
  `test_band_vocabulary_actually_appears_in_knowledge_readme`). The new README "Grounding tiers" table
  could drift from `EvidenceTier` / `_TIER_CEILING` with no failing test. A parallel pin
  (assert each `EvidenceTier` name + its ceiling band appears in the README) would close this loop —
  flagged as a candidate minor finding; not added here because this ticket is scoped doc-only and the
  reviewer may prefer to fix inline.
- **The relabeled learnings entry still has no real citation.** Per the ticket I only applied the label
  and did not invent a link/date/verbatim quote (that would be fabricating a source). The entry now
  self-documents that gap. If the reviewer wants the convention demonstrated *fully*, someone with web
  access would need to add a real Jon Loomer / Meta Help link + dated quote — out of scope for a
  doc-relabel and arguably its own small task.
- **No CLI/integration exercise** — there is nothing executable in this change. The "validation" is
  reading the three docs for internal consistency and against `confidence.py`; the only automated gate
  is the unchanged test suite.
- **Lint:** repo ships no ruff/mypy/flake8 and no CI; pytest is the only gate (consistent with the
  prereq tickets' notes).
