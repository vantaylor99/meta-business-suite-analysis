description: Show the operator, in the human-readable brief, the evidence and trust level behind every recommendation — the number, the time window, the sample size, which ad, a High/Medium/Low band with why, and the exact command to re-check the number themselves.
prereq: confidence-actions-analyze
files: src/meta_ads_analysis/briefs.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why

The operator brief (`briefs.py`) is the surface a specialist actually reads. The action plan now
carries structured Evidence + Confidence (from the `confidence-actions-analyze` ticket), but
`_brief_action` currently drops the `evidence` and only forwards `rationale`. This ticket surfaces
the evidence + confidence band inline so no advice can be acted on without the facts and the
agent's confidence in it being visible and checkable — the parent ticket's core goal.

## What to build

### Carry evidence + confidence through `_brief_action`

`_brief_action` builds the per-action dict for the brief JSON. Add the `evidence` and `confidence`
blocks from the source action (pass them through; don't recompute — the band is computed once, in
the action plan). Keep all existing fields for backward compatibility.

### Render them in `render_operator_brief`

For each action line in the section renderers (Approved To Execute, Ready For Review, Needs Human
Judgment, etc.), append a compact, scannable evidence + confidence block beneath the rationale,
using the `confidence.render_confidence_line` / `render_evidence_line` helpers so the format matches
the rest of the repo. Target shape per action:

```
- pause_ad_123 (pause_ad) targeting "Cody - Copy": High waste risk …
    Evidence: ROAS 1.20 over 2026-06-10..2026-06-24 · 43 purchases · $880 spend · ad 123
    Confidence: 🟢 High (~80–100%) — 43 purchases (> floor), recent window, direct API observation
    Re-check: account_metrics --account divine_designs --level ad --date-from 2026-06-10 --date-to 2026-06-24
    Would raise: a completed A/B · Would lower: a smaller sample or a contradicting window
```

- Actions whose confidence is `abstain` render as **"Insufficient data — keep running"** (⚪), NOT
  a low percentage, and should read as "promising test," never "winner/loser."
- A `causal_flag` action must show the visible **"correlational — confirm via A/B"** label and
  (per the parent ticket) note that an experiment can be filed via `experiment define` to confirm
  it. (The brief just surfaces the offer in text; it does not auto-file.)
- Keep the markdown skimmable — one short block per action, not a wall of text.

## TODO

- [ ] Pass `evidence` + `confidence` through `_brief_action`.
- [ ] Render the evidence/confidence/re-check/would-raise-lower block per action in
      `render_operator_brief`, using the shared `confidence.py` render helpers.
- [ ] Render abstain actions as "insufficient data — keep running"; render causal_flag actions with
      the "correlational — confirm via A/B" label + the offer to file an experiment.
- [ ] Tests (below); run `python -m pytest tests/ -q 2>&1 | tee /tmp/conf_brief.log`.

## Key tests (TDD)

- A built brief over an action plan with a high-confidence pause renders a band (🟢 High), the four
  evidence facts, and the `account_metrics …` re-check line. (Parent use case: an auditor can re-run
  the named query to confirm the number.)
- An abstain action renders "insufficient data — keep running," not a percentage.
- A causal_flag action renders "correlational — confirm via A/B" and mentions filing an experiment.
- `build_operator_brief` / `render_operator_brief` existing tests
  (`test_operator_brief_*`) still pass — the snapshot/section structure is preserved, the
  evidence/confidence block is additive.

## Edge cases & interactions

- **No false precision in the brief.** Render the band + range ("🟢 High (~80–100%)"), never a
  two-significant-figure number. A test should assert no `\d{1,3}\.\d%`-style precise score appears.
- **Backward compatibility.** Existing brief consumers/tests must not break; evidence/confidence are
  additive JSON fields and additive markdown lines.
- **Abstain rendering.** Must be visually distinct from "Low" — ⚪ "insufficient data," not 🔴 Low.
- **Missing evidence/confidence on an action** (e.g. measurement_review actions that carry none)
  should render gracefully (omit the block) rather than printing `None`.
- **One vocabulary.** The emoji/labels must match `confidence.py` (and `knowledge/README.md`); no
  second scale introduced in the renderer.
