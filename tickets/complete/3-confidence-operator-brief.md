description: The operator brief now shows, under every recommendation, the facts behind it (the number, the time window, the sample size, which ad), a High/Medium/Low trust band with why, and the exact command to re-check the number — so no advice can be acted on without its evidence visible and checkable.
prereq:
files: src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/confidence.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

The completed implementation surfaces the structured `evidence` + computed `confidence` (attached to
actions upstream by `confidence-actions-analyze`) inline in the human-readable operator brief, beneath
every action bullet in every section. Per action: a labeled `Evidence:` line (metric · window ·
sample · entity), a `Confidence:` band line in the shared 🟢/🟡/🔴/⚪ vocabulary, a `⚠️ correlational`
caveat + `experiment define` offer when the claim is causal, a `Re-check:` line with the exact
`account_metrics …` command, and a `Would raise: … · Would lower: …` line. The block is additive
(empty actions like `measurement_review` render their bullet with no block, never `None`), and
`render_evidence_line` gained a backward-compatible `include_regen=False` kwarg so the re-check
command appears once on its own labeled line rather than twice.

See the implement commit `e2e1ec4` for the full diff.

## Review findings

Adversarial pass over the implement diff (`e2e1ec4`) and the files it touched/should have touched.
**Disposition: 2 minor findings fixed inline; no major findings (no new tickets filed).**

### Checked — and what was found

- **End-to-end data flow (interaction).** Verified the brief's keys match what the upstream pipeline
  attaches: `actions.py:_attach_confidence` sets `action["evidence"]`/`action["confidence"]`, and
  `_abstain_action` flips guarded thin-data actions to non-executable *while preserving* the
  `band: "abstain"` confidence block — so an abstained action lands in "Needs Human Judgment" and
  renders "⚪ Insufficient data — keep running" as designed. No mismatch. **OK.**
- **`include_regen` kwarg scope addition (DRY / blast radius).** The only non-test caller of
  `render_evidence_line` is the brief; the existing test call (`test_render_helpers_…`) uses the
  `True` default. The kwarg is backward compatible and no other caller wanted regen suppressed.
  **OK.**
- **Abstain label deviation from `BAND_PRESENTATION` ("…abstain" → "…keep running").** Confirmed this
  is explicitly mandated by the implement ticket ("render as 'Insufficient data — keep running'"). The
  ⚪ emoji and `abstain` band are preserved and the JSON still stores `"abstain"`, so the one-
  vocabulary invariant holds. **OK — intentional, spec-mandated.**
- **False-precision / `None` guards (type safety, edge).** The renderer never formats a numeric score
  (bands are names/ranges only); pinned by `test_operator_brief_never_prints_false_precision_or_none`.
  No-evidence actions render gracefully. **OK.**
- **Graceful-degradation branches (edge coverage gap → fixed).** The implementer's tests covered the
  happy path (full evidence+confidence), abstain, and causal. They did **not** cover the two
  *independent* degradation branches of `_render_action_evidence`: (a) evidence present but
  `regenerating_query` is `None` (real path — `build_regenerating_query` returns `None` on missing
  inputs) and (b) confidence present with no evidence block. **Fixed inline:** added
  `test_operator_brief_evidence_without_regen_omits_recheck_line` and
  `test_operator_brief_confidence_without_evidence_renders_band_only`, asserting no orphan
  `Re-check:`/`Evidence:` lines and no `None`. Both pass.
- **Docs reflect the new reality (minor → fixed).** `docs/META_ACTION_WORKFLOW.md` "Later Phase:
  Operator Brief" described only the high-level brief contents and omitted the new inline
  evidence/confidence/re-check block. **Fixed inline:** added a paragraph describing the `Evidence:` /
  `Confidence:` / `Re-check:` / would-raise-lower lines, the abstain phrasing, and the causal A/B
  offer. README's brief section only documents the command + output paths (unchanged, still accurate).

### Noted, not changed (judgment calls)

- **Abstain `Confidence:` line still carries the engine factor "…insufficient data, abstain".** The
  headline reads "keep running"; the trailing explanatory factor (verbatim from the confidence
  engine's `data_strength`) contains the word "abstain". Judged informative rather than contradictory
  — the verdict is not presented as a winner/loser, and rewording the engine's factor string here
  would fork the one-vocabulary source. Left as the implementer's documented judgment call.
- **4-space-indented sub-lines under markdown bullets.** The block renders as plain skimmable text
  (matching the implement ticket's mock) rather than nested markdown; the brief is read primarily as
  text/terminal output and the existing brief already mixes `##`/`-` plain formatting. Not a defect.
- **`experiment define` offer is a static template** (`experiment define --account <slug> …`). Correct
  per the parent ticket — the brief only surfaces the offer in text and cannot know
  control/variant/variable.

### Validation

- `.venv/bin/python -m pytest tests/ -q` → **123 passed** (121 pre-existing + 2 added). No failures,
  no regressions in the existing `test_operator_brief_*` / `test_render_helpers_*` suite.
- **Lint:** the project ships no lint/type tooling (no ruff/mypy/flake8 in `.venv`, no `[tool.ruff]`
  or `[tool.mypy]` in `pyproject.toml`, no CI workflows). Nothing to run; flagged here for honesty
  rather than silently claiming a clean lint.
- Not exercised through the full `sync → build_action_plan → build_operator_brief` CLI path against a
  real report; tests construct evidence/confidence via the real `assess`/`*_to_dict` engine and feed
  them through `build_operator_brief`, and the upstream key contract was verified by reading
  `actions.py`. A real-report CLI smoke run remains a reasonable extra check but is not blocking.
