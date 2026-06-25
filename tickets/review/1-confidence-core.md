description: A shared engine now computes — from objective numbers, not the model's gut — how much to trust each recommendation and packages the evidence behind it. This review checks that the trust math is sound and genuinely un-fakeable before other features plug into it.
prereq:
files: src/meta_ads_analysis/confidence.py (new), src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py, knowledge/README.md
difficulty: hard
----
## What landed

A new pure module `src/meta_ads_analysis/confidence.py` (no network, no clock, no I/O beyond a
config constant) that turns deterministic inputs into a transparent confidence band + the evidence
behind it. Plus one new config constant, 16 new unit tests, and a one-paragraph pointer in
`knowledge/README.md`. Full suite: **97 passed** (`.venv/bin/python -m pytest tests/ -q`).

### Public API (the contract the dependent tickets plug into)

- `Band(IntEnum)` — `abstain(0) < low(1) < medium(2) < high(3)`. Integer order is load-bearing:
  `combine_bands = min(...)`, and because `abstain` is the floor, combining anything with `abstain`
  yields `abstain`.
- `EvidenceTier(IntEnum)` — `model_inference < external < correlational < direct_observation <
  ab_experiment`. `_TIER_CEILING` maps each to its highest reachable band.
- `BAND_PRESENTATION` — `{emoji,label,range}` per band, **verbatim** to `knowledge/README.md`
  (🟢 High ~80–100% / 🟡 Medium ~50–80% / 🔴 Low <50% / ⚪ Insufficient data — abstain —). Pinned by
  a test so the two docs can't drift into two scales.
- `Evidence` / `Confidence` dataclasses (`slots=True`, `from __future__ import annotations` style).
- `build_regenerating_query(slug, level, date_from, date_to)` → exact `account_metrics …` string, or
  `None` if any arg is missing (never fabricates).
- `detect_causal_language(text)` → bool, word-boundary regex over because/causes/caused/drives/
  due to/leads to/results in/thanks to/responsible for (+ minor inflections).
- `data_strength(...)`, `grounding_strength(tier, *, causal_claim)`, `combine_bands(data, grounding)`.
- `assess(*, evidence, tier, spend_floor, conversions_floor, recency_days, pvalue=None,
  causal_text=None)` → `Confidence`. **No parameter accepts a pre-baked band/score.**
- `render_confidence_line` / `render_evidence_line` — compact one-line presentation helpers.
- `config.CONFIDENCE_RECENCY_STALE_DAYS = 14` (the only new constant; existing floors untouched).

## The rubric, exactly as implemented (review these — some are my calls, not the ticket's)

**data_strength** (`sample_purchases`, `sample_spend`, `spend_floor`, `conversions_floor`,
`recency_days`, `pvalue`):
- Below floor (NEITHER spend nor conversions floor cleared; `None` sample treated as 0) → `abstain`
  with a factor naming the floor. Never reports a low %.
- Base band: cleared conversions floor AND `purchases ≥ 4×conversions_floor` → **high**; cleared
  conversions floor but `< 4×` → **medium**; cleared *only* the spend floor (conversions below) →
  **low** (thin on conversions).
- Recency: `recency_days > stale_days` **or** `recency_days is None` → round **down one band**
  (floored at low). Recent → no change.
- Significance: only when `pvalue is not None` — `p<0.05` supports higher (no cap); `p≥0.05` caps at
  medium.

**grounding_strength**: band = tier ceiling; if `causal_claim and tier != ab_experiment` → down one
band (floored at low) + factor `"correlational — confirm via A/B"`.

**combine_bands**: `min(data, grounding)` — the weaker axis governs; grounding caps a strong sample.

## How to validate (use cases — treat my tests as a FLOOR, not the finish line)

The 16 new tests (search `tests/test_meta_ads_analysis.py` for `Band`, `assess`, `data_strength`,
`grounding_strength`, `detect_causal_language`, `build_regenerating_query`) cover:
- weaker-axis combine incl. abstain absorption;
- **the headline invariant**: 500 purchases / $50k / recent + `correlational` + causal text → band
  **low** (`data_band==high`, `grounding_band==low`) — grounding caps sample size;
- same evidence + `ab_experiment` + p<0.05 → **high** (causal guard does NOT downgrade an experiment);
- below-floor (3 purchases / $40) → **abstain**, not low;
- `None` sample → abstain (the anti-fabrication path), and `inspect.signature(assess)` has no
  band/score knob;
- stale vs recent rounds down exactly one level; unknown recency rounds down; non-significant p caps;
- causal detector true/false cases; exact regen-query string + `None` on missing args;
- presentation strings pinned to the README vocabulary.

Suggested adversarial probes for the reviewer:
- Fuzz the `4×` knee and the medium/high boundary — is a 1× vs 4× conversions split defensible, or
  should it be tiered finer? This threshold is **my choice**, not specified by the ticket.
- Push `detect_causal_language` for false positives ("results in the report", "because of course")
  and false negatives ("the lift comes from …", "→").
- Confirm `assess` truly has no back-door to set a band (e.g. via `Evidence` fields or kwargs).
- Verify combine still yields abstain for every `(abstain, X)`/`(X, abstain)` pair.

## Known gaps / honest caveats

- **Interpretation call on `pvalue=None`.** The ticket's parenthetical said "p≥0.05 or None caps at
  medium," but `pvalue` defaults to `None` and the cap is described as only applying "when a pvalue
  is supplied for a comparative claim." I read `pvalue is None` as *not a comparative claim → no
  cap* (otherwise every non-experimental rec would cap at medium even when grounding already governs).
  If the reviewer disagrees, the one-line change is in `data_strength`. **Flag for sign-off.**
- **Thresholds are a rubric, not a derivation.** `4×conversions_floor`→high, one-band recency
  step, floor-at-low downgrades — all reasonable but unvalidated against real account data. No
  empirical calibration was done.
- **`detect_causal_language` is a keyword detector** — deliberately simple; will have edge-case
  false positives/negatives. Fine for *flagging* a causal claim; not a parser.
- **Not wired into anything yet.** Action plan, brief, monitor, and experiment readouts still don't
  call this — that's the dependent tickets. So there are **only unit tests**, no integration tests,
  and the render helpers' exact format hasn't met a real renderer yet.
- **No ruff/lint gate exists** in the repo (only pytest is configured), so style was matched by
  hand, not enforced.

## Environment note (not a code issue)

The repo had **no virtualenv and no installed test deps** at HEAD. I created `.venv/` (Python 3.14,
git-ignored) and installed `pytest`, `duckdb`, `requests` to run the suite. Tests import the package
via `pythonpath=["src"]`, so the package itself need not be pip-installed. No `.pre-existing-error.md`
was needed — the suite is fully green.
