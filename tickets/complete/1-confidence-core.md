description: A shared engine now computes — from objective numbers, not the model's gut — how much to trust each recommendation and packages the evidence behind it. Reviewed and accepted; the trust math is sound and has no back-door for a model-typed score.
prereq:
files: src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py, knowledge/README.md
difficulty: hard
----
## What shipped

A new pure module `src/meta_ads_analysis/confidence.py` (no network, no clock, no I/O beyond one
config constant) that turns deterministic inputs (sample size, recency, evidence tier, significance)
into a transparent confidence band + the evidence behind it. One new config constant
(`CONFIDENCE_RECENCY_STALE_DAYS = 14`), a one-paragraph pointer in `knowledge/README.md`, and unit
tests in `tests/test_meta_ads_analysis.py`.

Public contract the dependent tickets plug into: `Band`/`EvidenceTier` ordered enums,
`BAND_PRESENTATION`, `Evidence`/`Confidence` dataclasses, `build_regenerating_query`,
`detect_causal_language`, `data_strength`, `grounding_strength`, `combine_bands`, `assess`, and the
`render_confidence_line`/`render_evidence_line` helpers. The hard invariant holds: `assess` exposes
**no** parameter that accepts a pre-baked band/score — the only path to a band is the deterministic
inputs, and missing sample → below floor → `abstain`.

Not wired into anything yet (action plan / brief / monitor / experiment readouts) — that is the
dependent tickets' job. So unit tests only; no integration tests, by design.

Full suite: **98 passed** (`.venv/bin/python -m pytest tests/ -q`).

## Review findings

### What was checked
- **Implement diff read first**, then the handoff. Re-derived the rubric (`data_strength`,
  `grounding_strength`, `combine_bands`, `assess`) from the source rather than the summary.
- **Anti-fabrication invariant** — confirmed via source + `inspect.signature(assess)` that no
  band/score knob exists; `None` sample and below-floor inputs both drive `abstain`, never a guessed
  low %.
- **Grounding-caps-sample invariant** — confirmed `combine_bands = min(...)` and that a large-n
  correlational causal claim reads `low` (data `high`, grounding `low`); `abstain` absorbs in every
  `(abstain, X)`/`(X, abstain)` pair.
- **Regenerating query is real** — verified `account_metrics --account … --level … --date-from …
  --date-to …` matches the actual `metrics_main` CLI flags in `cli.py:813` (it is runnable, not
  fabricated), and returns `None` on any missing arg.
- **Negative / degenerate edge inputs** probed directly (see below).
- **Causal detector** probed for false positives/negatives.
- **Docs** — read every touched file. README pointer is accurate; it references the real downstream
  slug `grounding-rules-and-external-evidence` (present in `tickets/`). `learnings.md` did **not**
  need editing — the ticket asked `would_raise`/`would_lower` to *mirror its style*, not to edit it.
- **Lint** — no ruff/flake config exists in the repo (only pytest is configured), so there is no
  lint gate to run; nothing to enforce. Style matches the surrounding module by hand.
- **Tests** — full suite green before and after my change (97 → 98).

### Minor — fixed in this pass
- **The anti-drift test did not actually prevent drift.** The handoff claimed the presentation
  strings were "pinned by a test so the two docs can't drift into two scales," but
  `test_band_presentation_matches_knowledge_vocabulary_exactly` only pinned the code constants to
  identical literals — it never read `knowledge/README.md`. The README could change its emoji/label
  with no failing test. **Added** `test_band_vocabulary_actually_appears_in_knowledge_readme`, which
  reads the README via `PROJECT_ROOT` and asserts every band's emoji **and** label substring is
  present — closing the loop the handoff described. (Note: the `~80–100%` / `~50–80%` / `<50%`
  *ranges* are NOT in the README — they came from the implement ticket's own table — so the new test
  cross-checks emoji + label, which is what "one vocabulary" actually means here.)

### Minor — documented, deliberately not changed
- **`pvalue=None` does not cap at medium.** The implement ticket's parenthetical read "p≥0.05 or
  `None` caps at medium," but the implementer treats `pvalue is None` as *not a comparative claim →
  no cap*, capping only when a pvalue is actually supplied and is ≥0.05. **This is the correct call
  and is accepted:** capping every non-pvalue claim at medium would mean a `direct_observation`-tier
  factual metric (large, recent sample, no comparison) could never read `high`, directly
  contradicting the tier ceilings the same ticket specifies. The flagged "or None" was the imprecise
  phrase. The one-line reversal site is in `data_strength` if a human ever wants the literal
  reading; **flagging here for visibility**, but no change made.
- **`significance` never raises, only un-caps.** For `p<0.05` the factor reads "supports higher" but
  the band is not raised above what sample/recency already set (e.g. stale + p=0.01 stays `medium`).
  This is technically accurate ("no cap applied") but the wording can read as if it lifts the band.
  Cosmetic; left as-is.
- **No input validation on floors / recency.** `conversions_floor=0` makes 0 purchases read "over
  floor → high", and a negative `recency_days` reads as "recent". Real callers pass the existing
  gates (25 / 100.0) and compute non-negative recency, so these degenerate inputs can't arise in
  practice; guarding them inside a pure helper would add noise. Documented, not guarded.
- **Causal detector is a flagger, not a parser.** Confirmed false positives ("results in the
  report", "because of course", "responsible for tracking") and false negatives ("the lift comes
  from …", "→"). Acceptable and already acknowledged — its only job is to *flag* a causal claim for
  the grounding downgrade, and over-flagging errs conservative (toward a lower band).

### Threshold calibration (not a defect)
The `4×conversions_floor → high` knee, the one-band recency step, and the floor-at-low downgrades
are a reasonable rubric but are **not** empirically calibrated against real account data. There is no
data to calibrate against until the engine is wired in (dependent tickets) and run against live
accounts, so no calibration ticket is filed now — it would be premature. Revisit once real
recommendations are flowing through `assess`.

### Major findings
**None.** No new fix/plan/backlog tickets filed. The module is pure, deterministic, has no
band/score back-door, the grounding cap survives any sample size, and `abstain` is a first-class
verdict distinct from `low` — every invariant the parent feature depends on holds.

## Environment note (not a code issue)
The repo has no committed virtualenv and no installed test deps at HEAD. A git-ignored `.venv/`
(Python 3.14) with `pytest`/`duckdb`/`requests` was created by the implement stage and reused here to
run the suite. Tests import the package via `pythonpath=["src"]`, so no pip install of the package is
needed. No `.pre-existing-error.md` was written — the suite is fully green.
