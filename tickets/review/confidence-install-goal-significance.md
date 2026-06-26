description: For app-install accounts, a recommendation's confidence rating used to count only purchases — which those accounts rarely have — so it was stuck low even when lots of installs backed it. It now counts the conversion type that fits the account's goal, and the operator-facing wording reads "conversions" instead of "purchases".
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----

## What this implemented

The significance sample that grounds a recommendation's confidence band is now **goal-aware**, and
the operator-facing wording was generalized from "purchases" to "conversions". Two source files
changed; the serialized `sample_purchases` JSON key and the `data_strength(sample_purchases=…)`
parameter were deliberately **left untouched** (structural rename is deferred — see Gaps).

### Phase 1 — `actions.py`
- New private helper `_select_sample_conversions(ad, goal) -> float | None` (just below
  `_select_action_metric`, ~line 645):
  - `goal == "maximize_in_app_subscriptions"` → `total_results` when `total_results not in (None, 0,
    0.0)`, else `total_app_installs` — the **exact** fallback idiom `_should_pause_ad` uses, so
    significance grounding, pause detection, and metric selection now agree on the conversion signal.
  - `roas` / default / unknown → `total_purchase_count` (single fallthrough `return`; unchanged value).
- `evaluate_action_confidence` now sources `Evidence.sample_purchases` from
  `_select_sample_conversions(ad, goal)` (line 557) instead of `ad["total_purchase_count"]`. `goal`
  was already resolved at the top of the function.
- `_abstain_action` rationale wording aligned: local `purchases` → `conversions`, prose now reads
  "only N conversions / $S spend" (line 615–620). The `sample_purchases` JSON key it reads is
  unchanged.

### Phase 2 — `confidence.py`
- `_fmt_purchases` renamed → `_fmt_conversions` (internal; no callers outside this module).
- Three `data_strength` factor strings + the `render_evidence_line` sample string changed the
  operator-visible word "purchases" → "conversions" (lines 184, 199, 206, 417). `BAND_PRESENTATION`,
  band names, and all serialized keys/params are byte-identical.

### Phase 4 — docs
- `docs/META_ACTION_WORKFLOW.md` "Evidence and Confidence" now documents goal-aware grounding
  (subscriptions-first then app-installs for install accounts; purchases for ROAS/default), the
  goal-neutral "conversions" wording, and that the `sample_purchases` key is retained pending rename.

## Why it matters (the bug)

`pollen_sense` (`maximize_in_app_subscriptions`) reports `total_purchase_count == 0`, so the old
sample was always 0. With spend cleared, `data_strength` returned `low` ("thin on conversions") no
matter how many installs accrued — and the `evidence` block was internally inconsistent
(cost/install metric, purchases sample). Now an install pause backed by ≥100 installs reads `high`.

## How to test / validate

`pytest tests/test_meta_ads_analysis.py` — **283 passed** (ran via `.venv/bin/python`; `python` is
not on PATH, use the venv interpreter). No ruff/mypy/pyright is configured in this repo (pyproject
declares only pytest).

### New tests added (all under the action-plan pause block, ~line 5477+)
- `test_action_plan_install_goal_grounds_significance_on_app_installs` — install goal, 0 purchases /
  0 results / 100 installs, spend cleared, recent → band `high`; `evidence["sample_purchases"] ==
  100.0` (installs, not the zero purchase count); `metric_name == "cost_per_app_install"`.
- `test_action_plan_install_goal_grounds_on_subscriptions_not_installs` — **pins the decision-1
  tradeoff**: 3 subscriptions + 80 installs → grounds on the 3 (`sample_purchases == 3.0`), band
  stays `low`. The conservative "few subscriptions honestly is thin" behavior is intentional.
- `test_action_plan_install_goal_abstains_when_both_signals_and_spend_are_thin` — 5 installs / $40
  spend (both floors fail) → `band == "abstain"`, guarded pause flips to `verdict ==
  "insufficient_data"`, `executable == False`; rationale contains "conversions".
- `test_action_plan_roas_goal_still_grounds_on_purchase_count` — ROAS account with results/installs
  *also* populated → `sample_purchases == 120.0` (purchase count, never the 999 fallback).

### Stale assertions updated (wording change)
- Brief renderer test (~line 1275): `"120 purchases"` → `"120 conversions"`.
- `render_evidence_line` test (~line 5293): `"42 purchases"` → `"42 conversions"`.
- Pre-existing factor-string assertions (`"thin on conversions"`, `"floor"`) already used
  goal-neutral text and needed no change.

### Manual contrast worth re-deriving
The headline transition is `low → high` on the install fixture above; before the change that same ad
(sample 0, spend cleared) computed `low`. The `abstain` branch is only reachable when spend is *also*
below the floor — a real high-waste pause/scale candidate always clears spend, so don't expect a real
candidate to abstain on conversion volume alone.

## Known gaps / things for the reviewer to scrutinize

- **`confidence.py:297` `would_raise="more purchases / a more recent window / a completed A/B"`** still
  says "purchases". The ticket enumerated exactly which strings to change (the 3 factor strings +
  `render_evidence_line`) and did **not** list `would_raise`, so it was left as-is. It is operator-
  facing-adjacent (shown in the brief's "Would raise:" line) — reviewer may decide it should also read
  "conversions" for full honesty. Low-risk one-word change if wanted.
- **`review.py:234`** still emits `"sample of N purchases / $S spend is …"` for a below-floor review
  reason. Out of scope here (ticket limited source edits to `actions.py` + `confidence.py`), so a
  review-gate insufficient reason will say "purchases" while the action plan says "conversions". Minor
  cross-module wording drift; candidate for the tracked rename or a tiny follow-up.
- **Structural rename deferred**: `sample_purchases` field / param / JSON key intentionally unchanged
  (spans ~10 modules + serialized plans + apply-time grounding guard). Tracked in
  `tickets/backlog/confidence-sample-conversions-rename.md`. Stored `action_plan.json` files still
  deserialize unchanged.
- **No `conversion_kind` label** ("80 app installs" vs "80 subscriptions") — deliberately out of
  scope. `metric_display` already names cost/install, so the generic "conversions" wording suffices;
  a typed kind label is a possible future refinement, not a requirement.
- **Internal `purchases` locals retained** in `confidence.data_strength` (line 176) and as a fixture
  key in tests — these are not operator-facing and were left alone on purpose.
