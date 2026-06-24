description: Before any pause/scale/budget recommendation reaches the operator, run an automatic second-opinion check that judges each call using only the facts cited for it — is the sample big enough, is the window long enough, is it a correlation dressed up as cause, is the stated confidence actually earned, and does the call even agree with its own numbers. Calls that fail get downgraded or dropped (and shown, not hidden) so plausible-but-wrong advice doesn't slip through.
prereq: confidence-operator-brief
files: src/meta_ads_analysis/review.py (new), src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py
difficulty: hard
----
## Why

`grounded-recommendations` (now the `confidence-*` tickets) makes every operator-facing
recommendation carry a structured `Evidence` block plus a `Confidence` band computed from a
deterministic rubric. This ticket is the **verification layer that sits on top of that**: a
fresh-eyes adversary whose only job is to try to *refute* each recommendation, using the same cited
evidence, and to correct or drop the ones that can't survive the challenge before they reach the
operator brief.

The principle is the one TESS already demonstrated in its code-review stage: the agent that
produced a call is the worst judge of whether it's grounded. The grounded work moved confidence to a
deterministic rubric — which means the most reliable adversary for the *arithmetic/structural* claims
is **code, not another AI** (code can't rubber-stamp, and it can re-derive the band from scratch).
This ticket builds that code adversary. The *semantic* refutations a code gate can't make (does this
contradict a narrative learning in the KB? is this window cherry-picked relative to a known relearning
period? is a free-text prose call grounded?) are handled by the companion doc-procedure ticket
`adversarial-review-protocol`, which mirrors how `grounding-rules-and-external-evidence` is the
doc mirror of the confidence code.

## What to build — `src/meta_ads_analysis/review.py`

A **pure** module (no Meta API, no network, no clock — mirror `confidence.py`'s style:
`from __future__ import annotations`, `@dataclass(slots=True)`). It reasons over the structured
recommendation (the action's `evidence` + `confidence` blocks produced by `confidence-actions-analyze`)
**in isolation** — it judges each call from its *cited evidence and claimed band only*, deliberately
NOT trusting the producing `rationale`'s conclusion. It re-derives the band from the same evidence via
`confidence.assess` and compares.

### The verdict

Verdicts, most-conservative → least: `insufficient` > `refuted` > `downgrade` > `stands`.

```python
@dataclass(slots=True)
class ReviewResult:
    verdict: str               # "stands" | "downgrade" | "refuted" | "insufficient"
    original_band: str         # the band the recommendation claimed
    revised_band: str | None   # corrected band for downgrade/insufficient (None for stands/refuted)
    reasons: list[str]         # human-readable; EACH names the specific rubric input that fails
    failed_inputs: list[str]   # rubric-input keys: "sample_floor" | "window_length" | "causal"
                               #   | "band_earned" | "direction" | "external"
```

A vague "looks good" / "looks wrong" is not an acceptable result — every non-`stands` verdict MUST
name the specific failing rubric input (mirror the TESS review rule). `stands` carries an empty
`failed_inputs`.

### The refutation checks (each names its failing input; conservative — round toward refute)

`review_recommendation(*, evidence: dict, confidence: dict, action: dict, spend_floor,
conversions_floor, min_window_days, recency_stale_days) -> ReviewResult` runs all checks and returns
the **most-conservative** verdict, accumulating reasons from every check that fired:

1. **`sample_floor`** — `sample_purchases`/`sample_spend` below `conversions_floor`/`spend_floor`
   (neither floor cleared) → `insufficient` (abstain). Reason: "below the 25-purchase floor — should
   abstain." This is the "9 purchases over 5 days winner" case.
2. **`window_length`** — window span (parsed from the `"YYYY-MM-DD..YYYY-MM-DD"` window string) is
   shorter than `min_window_days` → `downgrade` (cap at most one band lower). Reason: "window may be
   unrepresentative; recommend a wider window." This is the "ROAS 1.1 over a 3-day window" case.
3. **`causal`** — `confidence.causal_flag` is true AND `grounding_tier != "ab_experiment"` AND the
   claimed band is stronger than the causal-capped band → `downgrade` to the capped band. Reason:
   "correlational — confirm via A/B." (Verifies `confidence.py`'s causal guard was actually applied,
   and catches a band that wasn't capped.)
4. **`band_earned`** — recompute the band from the cited evidence via `confidence.assess` (using ONLY
   the evidence + tier, not the rationale). If the claimed `confidence.band` is stronger than the
   recomputed band → `downgrade` to the recomputed band. Reason: "stated confidence exceeds what the
   rubric inputs support." This is the structural defense against a drifted/hand-edited band.
5. **`direction`** — the action's *direction* contradicts its own cited metric against the account
   goal: a scale/`increase_adset_budget`/`consider_scale_budget` whose cited ROAS is below the goal's
   target, or a `pause_ad` whose cited ROAS is comfortably *above* target (pausing a winner) →
   `refuted`. Reason: "recommendation contradicts its cited metric vs the account goal." (Read the
   goal/target from the action plan's `account_action_policy`, the same source `briefs._account_goal`
   already uses.)
6. **`external`** — `grounding_tier == "external"` but the claimed band is above `low` → `downgrade`
   to `low`. Reason: "external evidence is a hypothesis, not confirmation — route to `experiment
   define`." (Enforces the external-evidence cap from the grounding-rules ticket on live calls.)

A `downgrade` whose revised band lands on `abstain` becomes an `insufficient` verdict (so it renders
as "keep running," never a 🔴 Low confident call).

### Applying verdicts over a whole plan

`review_action_plan(plan: dict, *, spend_floor, conversions_floor, min_window_days,
recency_stale_days) -> dict` returns a **new** plan (don't mutate the input) where each
recommendation-bearing action gains a `"review"` block (the `ReviewResult` as a dict) and has its
`confidence`/`status`/`executable` adjusted per verdict:

- **`stands`** → unchanged.
- **`downgrade`** → replace `confidence.band` (and `data_band`/`grounding_band` as appropriate) with
  `revised_band`; append the reason(s) to `confidence.factors`. Action stays in its section but at the
  lower band.
- **`insufficient`** → `executable = False`, `status = "proposed"`, add `verdict =
  "insufficient_data"`; rationale reads "promising test / keep running," never winner/loser. (Same
  shape `confidence-actions-analyze` uses for below-floor abstention — be consistent.)
- **`refuted`** → `executable = False`; demote out of approved/executable; add `verdict = "refuted"`.
  Surfaced (see brief section below) — NEVER silently deleted.

**Materiality threshold:** only review **recommendation-bearing** actions — those carrying a
`confidence` block (`pause_ad`, `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`).
Actions with no `confidence`/`evidence` (informational, `measurement_review`,
`disable_meta_ai_controls` follow-ups) are skipped entirely and pass through untouched.

### Wiring into the brief flow (`briefs.py` + `cli.py`)

The gate must run **before** the brief is built so corrected/dropped calls never reach the operator:

- In `build_operator_brief` add a `review_enabled: bool = True` parameter. When enabled, run
  `review.review_action_plan(plan, ...)` on the incoming plan first and build the brief from the
  reviewed plan. The floors come from `config.py` (reuse the existing constants — see below).
- `_brief_action` carries the `review` block through (additive, like `evidence`/`confidence` in
  ticket 3).
- `render_operator_brief` adds a **"Refuted / Downgraded By Review"** section listing every action
  whose verdict was `refuted` or `insufficient` (with the failing input + reason), so the operator
  sees what was filtered and why. For `downgrade` verdicts that keep the action in its section, append
  a short "↓ Review: <reason>" line under the confidence block.
- `operator_brief_main` in `cli.py`: add a `--no-review` flag that passes `review_enabled=False`
  (escape hatch + lets the existing brief behavior be reproduced). Default is review ON.

### Config (`config.py`)

Add, next to the existing floors — do NOT introduce competing numbers:
- `REVIEW_MIN_WINDOW_DAYS` (suggest 7) — minimum representative window for the `window_length` check.

Reuse existing constants for the floor re-check so the gate and the producer share floors:
`MIN_WASTE_SPEND` (100.0) / `MIN_SCALING_SPEND` (75.0) for spend, the experiment conversions floor
(25) for purchases, and `CONFIDENCE_RECENCY_STALE_DAYS` (added in `confidence-core`) for recency.

## TODO

- [ ] Create `review.py`: `ReviewResult`, the verdict precedence, `review_recommendation` (the six
      checks above, conservative most-restrictive-wins, every non-`stands` naming its failing input),
      and `review_action_plan` (per-action `review` block + status/executable/confidence adjustments,
      returns a new plan, only touches confidence-bearing actions).
- [ ] Add `REVIEW_MIN_WINDOW_DAYS` to `config.py`; reference the existing floor + recency constants.
- [ ] Wire `review_enabled=True` into `build_operator_brief`; carry the `review` block through
      `_brief_action`; render the "Refuted / Downgraded By Review" section + per-action downgrade note
      in `render_operator_brief`.
- [ ] Add `--no-review` to `operator_brief_main` in `cli.py`.
- [ ] Tests in `tests/test_meta_ads_analysis.py` (see below).
- [ ] Run `python -m pytest tests/ -q 2>&1 | tee /tmp/review_gate.log` and confirm green.

## Key tests (TDD)

- **Below floor → insufficient.** A `consider_scale_budget` with 9 purchases over a 5-day window →
  `verdict == "insufficient"`, action `executable is False`, reason names the purchase floor. (Parent
  use case: the "9-purchase winner" never reaches the brief as a confident call.)
- **Short window → downgrade.** A `pause_ad`, ROAS 1.1, 3-day window → `verdict == "downgrade"`,
  `revised_band` below `original_band`, reason "window may be unrepresentative." `failed_inputs`
  contains `"window_length"`.
- **Causal correlational → downgrade.** A `consider_scale_budget` claiming `band == "high"`,
  `grounding_tier == "correlational"`, `causal_flag is True` → `downgrade` to the capped band,
  `failed_inputs` contains `"causal"`, reason mentions "confirm via A/B."
- **Direction contradiction → refuted.** A `pause_ad` whose cited ROAS is well above the account's
  `target_roas` → `verdict == "refuted"`, `failed_inputs` contains `"direction"`; the brief lists it
  under "Refuted / Downgraded By Review," NOT under "Approved To Execute."
- **Clean call → stands.** A `pause_ad` with 43 purchases / 14-day window / 🟢 High direct
  observation → `verdict == "stands"`, band intact, `executable` unchanged, `failed_inputs == []`.
- **Band-earned.** An action whose claimed `band` is stronger than `confidence.assess` recomputes from
  its evidence → `downgrade` to the recomputed band; `failed_inputs` contains `"band_earned"`.
- **Gate only ever demotes.** A `proposed` action is never promoted to `approved`, and no verdict ever
  raises a band or sets `executable` true. Assert this invariant directly.
- **Skips non-recommendation actions.** A `measurement_review` action (no `confidence` block) passes
  through untouched; existing `test_operator_brief_*` tests still pass with review ON by default.
- **Idempotent.** `review_action_plan(review_action_plan(plan)) == review_action_plan(plan)` — a call
  already corrected by review is not downgraded a second time.

## Edge cases & interactions

- **Fresh-context isolation.** The gate decides from the structured evidence + claimed band ONLY; it
  must not trust the `rationale`'s conclusion. (It may pass `rationale` to causal detection — that's
  re-checking, not trusting.) Mirror the TESS reviewer's evidence-only isolation.
- **No fabricated data.** The deterministic gate reasons over the cited evidence and may *recompute*
  the band from it; it must NOT invent a contradicting number and does NOT re-pull metrics (re-pulling
  over a standard window to catch semantic cherry-picking is the companion `adversarial-review-protocol`
  layer's job). Read-only w.r.t. Meta — no account writes.
- **Must not weaken the guarded-write gate.** This sits UPSTREAM of `build_api_operation`/approval.
  PAUSED-by-default, `proposed → approved → validate_only → execute`, and the Meta-AI param block are
  untouched. Review can only *demote* (executable→non-executable, approved-eligible→not); it can never
  flip a proposed action into an approved/executable one. A refuted/insufficient action becomes
  non-executable, so it can never be approved into a write.
- **Conservative tie-break.** When several checks fire, the most-conservative verdict governs
  (`insufficient` > `refuted` > `downgrade` > `stands`) and ALL reasons are accumulated, so the
  operator sees every problem, not just the worst.
- **Downgrade-to-abstain becomes insufficient.** A downgrade whose revised band reaches `abstain`
  renders as ⚪ "insufficient data — keep running," never 🔴 Low.
- **Refuted is surfaced, never hidden.** A demoted call appears in the "Refuted / Downgraded By Review"
  section with its failing input — silent deletion would read as "nothing was wrong."
- **Backward compatibility.** `review` is an additive JSON field and an additive brief section. With
  review ON by default, fixtures that carry no confidence blocks must be no-ops (gate skips them).
  Keep `rationale`/`evidence`/`confidence` intact.
- **One vocabulary.** Reuse `confidence.py`'s bands/emoji (🟢/🟡/🔴/⚪) and `assess`/`combine_bands`;
  introduce no second confidence scale.
- **Determinism.** No clock or network inside the rubric — `recency_days` is already on the
  evidence/derivable from the plan's `run_date`; tests stay clock-independent (matches the existing
  no-`datetime` test style).
