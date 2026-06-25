description: A second agent with fresh eyes must now try to disprove every pause/scale/budget recommendation before the operator sees it — checking the things a calculator can't (does it clash with what we already know, was the time window cherry-picked, is a plainly-written recommendation actually grounded) — and downgrade or drop any call that can't survive the challenge. This change is documentation only.
prereq:
files: AGENTS.md, knowledge/README.md, src/meta_ads_analysis/review.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/briefs.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped (documentation-only — plus one pin test added in review)

Adds the **agent-followed procedure** for the *semantic* half of the adversarial review — the
refutations code cannot make. It is the doc mirror of `review.py`, exactly as
`grounding-rules-and-external-evidence` was the doc mirror of `confidence.py`. The repo has no
LLM-invocation code, so this layer is a rule the agent follows, not Python.

- `AGENTS.md` — new **"Adversarial-review rule"** block under Interpretation Rules (directly after the
  Grounding rule) + a cross-referencing **Guardrails** bullet.
- `knowledge/README.md` — new **two-layer review** subsection under "Confidence & evidence".
- `tests/test_meta_ads_analysis.py` — `test_review_verdict_taxonomy_appears_in_docs` (added in this
  review pass; see findings).

## Review findings

### Read the implement diff first, with fresh eyes
Read `git show 06c78aa` (the AGENTS.md + README additions) before the handoff summary, then read
`review.py` and `confidence.py` end-to-end and grepped every cited symbol. This is a prose contract
over existing code, so the review was a read-for-accuracy + read-for-contradiction pass, not a
behavioral test.

### Accuracy of the prose vs. the code (the core of this review) — all confirmed
- **Verdict taxonomy faithful.** The four words in both docs (`stands` / `downgrade` / `refuted` /
  `insufficient`) match `review.py`'s `VERDICT_*` constants exactly, and the doc's
  "downgrade-landing-on-abstain → insufficient / most-conservative-wins" matches `_resolve`
  (`review.py:305-336`).
- **Six deterministic checks match.** sample-floor, window-length, causal(-cap), band-earned,
  direction (scale/pause), external(-cap) are exactly the six in `review_recommendation`
  (`review.py:216-300`).
- **Read-only / never-re-pulls is correct.** `review.py`'s module docstring (lines 20-22) explicitly
  defers the same-metric re-pull to *this* doc-procedure; the doc correctly assigns the re-pull to the
  **agent**, never implies the code does it.
- **Demote-only / upstream-of-write-gate stated correctly.** Matches `_apply_verdict` (`review.py:475`,
  only ever demotes band / flips `executable=False` / demotes `approved`→`proposed`); the doc never
  claims the pass approves, enables a write, or alters PAUSED-by-default.
- **Materiality claim accurate.** `review_action_plan` (`review.py:439`, `_ACTION_SPEND_FLOOR`) reviews
  only the four confidence-bearing actions (`pause_ad`, `increase_adset_budget`,
  `consider_scale_budget`, `refresh_creative`) and passes informational ones through.
- **One vocabulary.** The 🟢/🟡/🔴/⚪ emoji+labels in both new blocks match `BAND_PRESENTATION`
  (`confidence.py:65`). No second scale introduced.
- **Every cited symbol/command exists.** `next_7_day_actions` (`analyze.py:203`, `reporting.py:80`),
  `account_metrics` (`pyproject.toml:38`) + `regenerating_query` (`confidence.py`/`briefs.py`),
  `experiment define` (`cli.py:1377`), the ~5-day watch grace window (`monitor.py:156`,
  `cli.py:1458` — default `5`, claim is accurate), and the brief's "Refuted / Downgraded By Review"
  section (`briefs.py:211`).

### Cross-doc contradiction check (the implementer deferred this) — no contradiction found
`README.md:291` and `docs/META_ACTION_WORKFLOW.md:146-158` describe the *code* review gate and are
fully consistent with the new semantic-layer framing. `META_ACTION_WORKFLOW.md:157-158` already
explicitly defers semantic refutations (KB contradiction, cherry-picked windows) to "the companion
`adversarial-review-protocol` doc procedure" — i.e. these docs anticipated this ticket and complement
it rather than clashing. No edit needed.

### Minor finding — fixed inline
The implementer flagged that no test pinned the new AGENTS.md prose to `review.py` (the grounding
ticket had set the `*_match_knowledge_readme` precedent). **Fixed:** added
`test_review_verdict_taxonomy_appears_in_docs`, which asserts the four `VERDICT_*` constant strings
appear in both `AGENTS.md` and `knowledge/README.md`, so a rename in code or a drifted doc fails the
suite. Deliberately did **not** pin the six per-check names: the docs spell them as prose
("causal-cap" / "external-cap") that intentionally differ from the code's `failed_input` identifiers
("causal" / "external"), so a verbatim pin would be fragile in both directions — the genuinely
mechanical, drift-prone bit is the verdict taxonomy, and that is now pinned.

### Honest flags reviewed — accepted as-is (no action)
- **"TESS-style stage" is presented as an option ("may be run as"), not as built.** The wording does
  not over-promise; correct as written.
- **The semantic pass is unenforced by definition.** Intended — no LLM-invocation code in the repo;
  the contract is doc accuracy + the agent honoring the rule. The new pin test guards the *vocabulary*
  half of that contract; the judgment half is inherently unenforceable in code.

### Edge / error / interaction angles checked
SPP/DRY/modularity are not in play for a doc-only change. Checked that the doc does not contradict the
code's abstention path (`_apply_verdict` INSUFFICIENT → band `abstain`, `executable=False`,
`verdict="insufficient_data"` — matches "becomes ⚪ keep running, never 🔴 Low") and the refuted path
(demote `approved`→`proposed`, never the reverse). All consistent.

## Validation performed
- `.venv/bin/python -m pytest tests/ -q` → **142 passed** (was 141 at HEAD; +1 is the new pin test).
- Repo ships no ruff/mypy/flake8 and no CI; pytest is the only gate (consistent with prereq tickets).

## Findings summary by category
- **Correctness / accuracy of prose vs code:** all claims verified true — none wrong.
- **Cross-doc contradiction:** none (README.md + META_ACTION_WORKFLOW.md complement, not contradict).
- **Missing test coverage:** one gap (verdict taxonomy unpinned) — **fixed inline** with a new pin test.
- **Major findings requiring a new ticket:** none.
