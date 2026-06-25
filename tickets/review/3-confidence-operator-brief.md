description: The operator brief now shows, under every recommendation, the facts behind it (the number, the time window, the sample size, which ad), a High/Medium/Low trust band with why, and the exact command to re-check the number — so no advice can be acted on without its evidence visible and checkable.
prereq:
files: src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

Surfaced the structured Evidence + computed Confidence (attached to actions by the
`confidence-actions-analyze` ticket) inline in the human-readable operator brief, so the brief — the
surface a specialist actually reads — never hands out advice without the facts and the agent's trust
in it being visible and reproducible.

- `briefs.py` / `_brief_action`: now passes `evidence` + `confidence` through into the brief JSON
  (additive fields; all existing fields preserved). Empty dict when the action carries none, so
  consumers and the renderer degrade gracefully.
- `briefs.py` / `render_operator_brief`: for every action line in every section (Approved To
  Execute, Ready For Review, Needs Human Judgment, Do Not Touch Yet, Meta AI Follow-Ups) it appends
  a compact, 4-space-indented block beneath the rationale:
  - `Evidence:` — `render_evidence_line(evidence, include_regen=False)` → metric · window · sample ·
    entity.
  - `Confidence:` — `render_confidence_line` for scored bands (🟢/🟡/🔴 + range + top-3 factors);
    abstain is rendered specially (see below).
  - `⚠️ correlational — confirm via A/B …` — only when `confidence.causal_flag` is set; includes the
    offer to file a confirming A/B via `experiment define` (text only — the brief does NOT auto-file).
  - `Re-check:` — the exact `account_metrics …` command from `evidence.regenerating_query`.
  - `Would raise: … · Would lower: …` — from `confidence.would_raise` / `would_lower`.
- `confidence.py` / `render_evidence_line`: added a backward-compatible `include_regen=True` kwarg.
  The brief passes `include_regen=False` so the reproduce-it command appears once, on its own labeled
  `Re-check:` line, instead of being duplicated inline in the Evidence line. Default `True` preserves
  the existing call sites/tests.

## How to validate

`python -m pytest tests/ -q` → **121 passed** (use `.venv/bin/python -m pytest …`; there is no bare
`python` on this box, and tests need `PYTHONPATH=src` only for ad-hoc imports, not for pytest).

New tests (all in `tests/test_meta_ads_analysis.py`, alongside the existing `test_operator_brief_*`):

- `test_operator_brief_renders_high_confidence_evidence_and_recheck_line` — a high-confidence pause
  renders `🟢 High (~80–100%)`, the four evidence facts (number `ROAS 1.20`, window
  `2026-06-10..2026-06-24`, sample `120 purchases`, which ad `ad:123 'Cody - Copy'`), and the exact
  `Re-check: account_metrics --account divine_designs --level ad --date-from … --date-to …` line.
  Also asserts the evidence/confidence blocks are carried into the brief JSON.
- `test_operator_brief_abstain_action_reads_as_keep_running_not_a_percentage` — an abstain renders
  `⚪ Insufficient data — keep running`, **never** `🔴 Low`, and contains no `%` at all.
- `test_operator_brief_causal_flag_action_offers_an_ab_experiment` — a causal-flagged action renders
  `correlational — confirm via A/B` and mentions `experiment define`.
- `test_operator_brief_never_prints_false_precision_or_none` — the band range `~80–100%` is allowed
  but no precise `\d{1,3}\.\d+%` score appears; and an action carrying no evidence (a
  `measurement_review`) renders its bullet with no block and no `Evidence/Confidence: None`.

Existing `test_operator_brief_separates_review_manual_and_meta_ai_followups`,
`test_operator_brief_moves_failed_live_lookup_to_do_not_touch`, and
`test_render_helpers_produce_compact_lines` still pass unchanged — the block is additive.

Eyeball the rendered shape (the example in the original ticket is illustrative, not literal — the
real Evidence line uses the repo's `· window … · n=… purchases / $… spend · ad:id 'name'` format
from `render_evidence_line`, not the ticket's prose mock):

```
PYTHONPATH=src .venv/bin/python -c "from meta_ads_analysis.briefs import build_operator_brief, render_operator_brief; ..."
```

## Reviewer notes / known gaps & judgment calls

- **Intentional deviation on the abstain label.** `BAND_PRESENTATION[Band.abstain]['label']` is
  `"Insufficient data — abstain"`, but the brief renders `"Insufficient data — keep running"` (same
  ⚪ emoji, same band — no new scale/percentage/band-name introduced). This is mandated by the
  ticket ("render as 'Insufficient data — keep running'", framed as a promising test, never
  winner/loser). The non-abstain bands defer entirely to `render_confidence_line`, so the one-
  vocabulary invariant holds. If a reviewer prefers the verbatim presentation label, that is a
  deliberate trade-off to reconsider, not an accident.
- **The abstain `Confidence:` line still includes the engine's `…insufficient data, abstain` factor**
  as the explanatory "why". The headline reads "keep running"; the trailing factor is the technical
  reason. Judged informative, not contradictory — flag if you disagree.
- **`experiment define` offer is a static template** (`experiment define --account <slug> …`). The
  brief genuinely cannot know the control/variant/variable, and the parent ticket says the brief only
  surfaces the offer in text. Did not try to synthesize a fuller command.
- **`include_regen` kwarg on `confidence.py`** is a small scope addition beyond the ticket's declared
  `files:` (briefs + tests). Justified to avoid printing the long re-check command twice; backward
  compatible. Worth a sanity check that no other caller wanted the regen suppressed.
- **No new false-precision guard at the data layer** — the renderer never formats a numeric score
  (bands are names/ranges only), so false precision can't originate here; the test pins this. The
  ROAS metric value (`ROAS 1.20`) is a real metric, not a confidence score, and carries no `%`.
- Not exercised end-to-end through `build_action_plan` → `build_operator_brief` with a real report;
  tests construct the evidence/confidence blocks via the real `assess`/`*_to_dict` engine and feed
  them through `build_operator_brief`. A real-report smoke run would be a reasonable extra check.
