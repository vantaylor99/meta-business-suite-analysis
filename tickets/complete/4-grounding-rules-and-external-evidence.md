description: The written rules are now in place so that every recommendation — even ones an agent types in plain prose — must show its evidence and a trust band, and advice found on the web is treated only as "worth testing," never as proof about this account.
files: AGENTS.md, knowledge/README.md, knowledge/learnings.md, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

Documentation-only change (plus one review-stage test pin). The prose mirror of the confidence
engine (`confidence.py`) now binds free-text analysis, and external/web evidence is fenced off so it
can never read as account-grade truth.

- **`AGENTS.md`** — Source Hierarchy made concrete (every recommendation states a band computed from
  sample/recency/grounding; abstain below the floor). New **"Grounding rule"** block under
  Interpretation Rules (four facts · confidence band + rationale · two axes · weaker-axis-caps ·
  abstain-below-floor · causal-language guard), pointing at `confidence.py` (canonical computation)
  and the README rubric (shared vocabulary). Three new Guardrails (causal guard, four-facts/abstain,
  "external never raises a live band").
- **`knowledge/README.md`** — two subsections after "Confidence & evidence": **"Grounding tiers"**
  (two axes + a table mapping each `EvidenceTier` to its `_TIER_CEILING` band) and **"External
  evidence is a hypothesis source, never a confirmation"** (route to `experiment define`, capped 🔴
  Low, cite+verbatim-quote, recency- not upvote-weighted, source-quality tiers, cold-start exception).
  The old forward-pointer to this ticket is now an in-page pointer.
- **`knowledge/learnings.md`** — the practitioner-consensus (Jon Loomer / Metalla / Meta Help) line
  inside the creative-enhancements learning is tagged `external` / capped 🔴 Low / confirm-via-A/B.
  Substance unchanged.

## Review findings

**Scope reviewed:** the full implement diff (`eae8cff`) read first, then the handoff; all three
changed docs read end-to-end against the canonical code (`confidence.py`); cross-doc sweep for any
other file referencing grounding/external/confidence concepts.

- **One-vocabulary consistency (checked — clean).** The band emoji+labels in `AGENTS.md`,
  `knowledge/README.md`, and `knowledge/learnings.md` match `BAND_PRESENTATION` in `confidence.py`
  exactly (🟢 High / 🟡 Medium / 🔴 Low / ⚪ Insufficient data — abstain). No competing scale was
  introduced.
- **Tier-cap correctness (checked — clean).** The README "Grounding tiers" table matches
  `_TIER_CEILING` row-for-row: ab_experiment/direct_observation → High, correlational → Medium,
  external → Low, model_inference → Low.
- **Read-for-contradiction (checked — clean).** No doc claims external can raise a live band; every
  doc treats abstention as a blessed output and requires the four facts. The causal-language guard
  wording ("correlational — confirm via A/B", downgrade one band) matches `grounding_strength`.
- **External-cap divergence from parent-ticket wording (checked — implementer was right).** The
  parent ticket said external is "capped Low/Medium," but `_TIER_CEILING[external] = Band.low` means
  it can never reach Medium. Docs correctly say **at most 🔴 Low** — code is authoritative. No change.
- **Cross-doc drift (checked — clean).** `docs/META_ACTION_WORKFLOW.md` and
  `knowledge/accounts/divine_designs/experiments.md` already describe the shared engine and
  "grounding caps data strength" consistently; nothing there contradicts the new rules.
- **Minor finding — FIXED INLINE.** The README grounding-tier table was *not* pinned by any test
  (only the band vocabulary was, via `test_band_vocabulary_actually_appears_in_knowledge_readme`), so
  it could silently drift from `_TIER_CEILING`. Added
  `test_grounding_tier_ceilings_match_knowledge_readme`, a sibling pin that asserts the README row
  naming each `EvidenceTier` carries that tier's true ceiling emoji+label. It has teeth: if code and
  prose diverge (e.g. external raised to Medium in one place only), no matching row is found and the
  test fails.
- **Accepted, not a defect — relabeled learnings entry has no real citation.** Per the ticket the
  implementer only applied the `external` label and did not fabricate a link/date/quote. The entry
  self-documents that gap and notes it does not prop up the entry's 🟡 Medium band. Adding a real
  citation needs web access and is its own small task, not a doc-relabel — left as-is.
- **Major findings:** none. No new fix/plan tickets filed.

## Validation

- **Tests:** `.venv/bin/python -m pytest tests/ -q` → **124 passed** (was 123; +1 the new pin).
- **Lint:** repo ships no ruff/mypy/flake8 and no CI; pytest is the only gate (consistent with the
  prereq tickets).
