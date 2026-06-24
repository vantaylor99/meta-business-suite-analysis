description: Build the shared engine that decides — from objective numbers, not from the model's gut — how much to trust each piece of advice the tool gives, and packages the facts behind it (the number, the time window, how much data, which ad). This is the foundation the other tickets plug into.
prereq:
files: src/meta_ads_analysis/confidence.py (new), src/meta_ads_analysis/config.py, knowledge/README.md, knowledge/learnings.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## Why

This is the centerpiece of the grounded-recommendations feature (see the parent plan ticket).
Every operator-facing recommendation must carry (a) the **evidence** behind it and (b) a
**computed confidence band**. Both belong in one small, pure, heavily-tested module so the rest of
the repo speaks ONE confidence language and the scoring logic lives in exactly one place. The hard
constraint that defeats the whole feature if violated: **the confidence value must be computed from
deterministic inputs via a transparent rubric — never a number the model free-types.** A code path
that cannot compute the rubric inputs must return *abstention*, not a guessed score.

This ticket builds only the engine + its unit tests. Wiring it into the action plan, brief,
monitor, and experiment readouts happens in the dependent tickets.

## What to build — `src/meta_ads_analysis/confidence.py`

A pure module (no Meta API, no I/O beyond reading constants) with these public pieces. Treat the
type sketches as the contract; match the repo's existing dataclass + `from __future__ import
annotations` style (see `analyze.py`, `monitor.py`).

### Evidence (the facts behind a call)

```python
@dataclass(slots=True)
class Evidence:
    metric_name: str            # "blended_roas"
    metric_value: float | None  # 1.20
    metric_display: str         # "ROAS 1.20"  (human string)
    window: str                 # "2026-06-10..2026-06-24"
    sample_purchases: float | None
    sample_spend: float | None
    entity_level: str           # ad | adset | campaign | account
    entity_id: str | None
    entity_name: str | None
    regenerating_query: str | None  # the account_metrics command that reproduces metric_value
```

`build_regenerating_query(account_slug, level, date_from, date_to) -> str` returns exactly:
`account_metrics --account <slug> --level <level> --date-from <from> --date-to <to>`
(the real entry point — `pyproject.toml` maps `account_metrics = meta_ads_analysis.cli:metrics_main`).
Return `None`-safe: if any of slug/level/dates is missing, return `None` (don't fabricate a query).

### The two axes (keep them separate; the weaker governs)

Bands are an ordered enum: `abstain < low < medium < high`. Presentation table (reuse the
`knowledge/` vocabulary verbatim — 🟢/🟡/🔴, do NOT invent a second scale):

| band | emoji | label | range |
|------|-------|-------|-------|
| high | 🟢 | High | ~80–100% |
| medium | 🟡 | Medium | ~50–80% |
| low | 🔴 | Low | <50% |
| abstain | ⚪ | Insufficient data — abstain | — |

**Data-strength axis** — `data_strength(*, sample_purchases, sample_spend, spend_floor,
conversions_floor, recency_days, pvalue=None) -> tuple[Band, list[str]]`:
- Below the floor (`sample_spend < spend_floor` AND `sample_purchases < conversions_floor`, i.e.
  neither floor cleared) → `abstain` with a factor string naming the floor. This is the bridge to
  section-2 abstention: below the floor we do NOT report a low percentage, we abstain.
- Above the floor: start from sample size (more purchases / more spend over floor → higher),
  modulate by recency (`recency_days` since the window's end — stale windows round down) and, when
  a `pvalue` is supplied for a comparative claim, by significance (p<0.05 supports higher; p≥0.05
  or `None` caps at medium). Conservative rounding: when an input is missing/ambiguous, round DOWN.
- Return the band plus the human-readable factors that produced it.

**Grounding-strength axis** — `grounding_strength(tier, *, causal_claim) -> tuple[Band, list[str]]`:
- `EvidenceTier` ordinal, highest→lowest: `ab_experiment` > `direct_observation` >
  `correlational` > `external` > `model_inference`. Each tier maps to a **ceiling band**:
  `ab_experiment`→high, `direct_observation`→high, `correlational`→medium, `external`→low,
  `model_inference`→low.
- **Causal-language guard:** if `causal_claim` is true and tier is not `ab_experiment`, downgrade
  the ceiling by one band and emit a factor `"correlational — confirm via A/B"`. (This is the
  causal guard the parent ticket says lives here.)

**Combine** — `combine_bands(data: Band, grounding: Band) -> Band`: the weaker (min) governs. If
either is `abstain`, the result is `abstain`. Grounding can therefore CAP a strong sample — a
large-n correlational causal claim cannot read High.

### The orchestrator

```python
@dataclass(slots=True)
class Confidence:
    band: Band                  # combined
    data_band: Band
    grounding_band: Band
    grounding_tier: str
    factors: list[str]          # why this band — shown to the operator
    would_raise: str
    would_lower: str
    causal_flag: bool
```

`assess(*, evidence, tier, spend_floor, conversions_floor, recency_days, pvalue=None,
causal_text=None) -> Confidence`:
- `causal_flag = detect_causal_language(causal_text)` when text is supplied.
- Compute both axes, combine, assemble `factors` (sample size, recency, tier, causal flag).
- `would_raise` / `would_lower` mirror `learnings.md`'s lines (e.g. raise: "more purchases / a
  completed A/B"; lower: "smaller sample / contradicting window / a refuting A/B").
- There is **no parameter that accepts a pre-baked score** — the only way to get a band is through
  the deterministic inputs. If the caller cannot supply sample data, it must pass values that drive
  `abstain` (this is enforced naturally because missing sample → below floor → abstain).

`detect_causal_language(text: str | None) -> bool`: keyword/regex detection of cause assertions in
prose — `because`, `causes`, `caused`, `drives`, `due to`, `leads to`, `results in`, `thanks to`,
`responsible for`. Case-insensitive, word-boundary aware. Used to flag a recommendation that
asserts cause from non-experimental data.

`render_confidence_line(conf) -> str` and `render_evidence_line(evidence) -> str`: compact
one-line renderers (emoji + label + range + top factors; metric/window/sample/entity + query) so
the brief and any markdown renderer share one format. Keep the API-facing data in dataclasses;
these are presentation helpers only.

### Config

Add named floors to `config.py` so callers don't hardcode them and they stay consistent with the
existing gates. Reuse existing values where they already exist rather than introducing competing
numbers: `MIN_WASTE_SPEND` (100.0) and `MIN_SCALING_SPEND` (75.0) already exist; `monitor.py`
defaults `min_spend=100.0`; `experiment.py` defaults `min_conversions=25`. Add
`CONFIDENCE_RECENCY_STALE_DAYS` (suggest 14) as the recency knee. Do NOT change the existing
constants' values.

## TODO

- [ ] Create `confidence.py` with `Evidence`, `Confidence`, `EvidenceTier` ranks, band enum +
      presentation table, `build_regenerating_query`, `data_strength`, `grounding_strength`,
      `combine_bands`, `assess`, `detect_causal_language`, and the two render helpers.
- [ ] Add `CONFIDENCE_RECENCY_STALE_DAYS` to `config.py`; reference existing floor constants.
- [ ] Unit tests in `tests/test_meta_ads_analysis.py` (see key tests below).
- [ ] Update `knowledge/README.md` "Confidence & evidence" section with a one-paragraph pointer
      that the SAME 🟢/🟡/🔴 rubric is now computed in `confidence.py` for live recommendations
      (the human rubric and the code rubric are deliberately one language). Keep it short; the full
      prose rules land in the `grounding-rules-and-external-evidence` ticket.
- [ ] Run `python -m pytest tests/ -q 2>&1 | tee /tmp/conf_core.log` and confirm green.

## Key tests (TDD)

- `combine_bands` returns the weaker axis: (high data, medium grounding) → medium; (high, low) →
  low; (abstain, high) → abstain.
- A large sample (e.g. 500 purchases, $50k spend, recent) with `tier=correlational` and a causal
  `causal_text` → band is at most **low** (medium ceiling downgraded one for the causal flag), and
  `causal_flag is True` with the "confirm via A/B" factor present — proving grounding caps sample.
- The same evidence with `tier=ab_experiment` and p<0.05 → band **high** (grounding no longer caps;
  causal flag does not downgrade an experiment-backed claim).
- Below-floor inputs (3 purchases, $40 spend, floors 25/100) → `assess` returns band `abstain`,
  NOT a low percentage; factors name the floor.
- `detect_causal_language` true for "scaled because the new audience converts" / "drives ROAS";
  false for "ROAS is 1.2 over 14 days" (descriptive, no causal verb).
- `build_regenerating_query` returns the exact `account_metrics …` string; returns `None` when
  level or a date is missing.
- Stale window (recency_days well past `CONFIDENCE_RECENCY_STALE_DAYS`) rounds the data band down
  versus an identical-sample recent window.

## Edge cases & interactions

- **Missing inputs round DOWN, never up.** Any `None` sample, missing window, or missing tier must
  push toward `abstain`/`low`, never toward a confident band. This is the anti-fabrication invariant.
- **No model-typed score path.** Assert (in code + test) there is no public way to set a band
  directly; bands only come from the deterministic inputs.
- **Abstain is a first-class verdict, not "low".** Below the floor the result is `abstain` (⚪),
  which downstream renders as "insufficient data — keep running," never "low confidence."
- **Grounding cap must survive any sample size.** Test the large-sample correlational case
  explicitly so a future refactor can't let sample size average the grounding cap away.
- **Causal guard only downgrades non-experimental claims.** An `ab_experiment` tier with causal
  language is NOT downgraded (the experiment IS the causal evidence).
- **One vocabulary only.** The emoji/label set must match `knowledge/README.md` exactly; a test
  should pin the presentation strings so the two docs can't drift into two scales.
- Pure module: no network, no clock-dependent behavior inside the rubric (recency is passed in as
  `recency_days`, computed by callers) so tests are deterministic and the existing no-`datetime`
  test style holds.
