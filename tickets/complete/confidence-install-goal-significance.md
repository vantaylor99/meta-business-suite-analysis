description: For app-install accounts, a recommendation's confidence rating used to count only purchases — which those accounts rarely have — so it was stuck low even when lots of installs backed it. It now counts the conversion type that fits the account's goal, and the operator-facing wording reads "conversions" instead of "purchases".
prereq:
files: src/meta_ads_analysis/actions.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----

## What shipped (implement + review)

The significance sample that grounds an action-plan recommendation's confidence band is now
**goal-aware**, and operator-facing wording was generalized from "purchases" to "conversions".

### Implement phase (commit `12662dc`)
- `actions._select_sample_conversions(ad, goal)`: install goal (`maximize_in_app_subscriptions`) →
  `total_results` (in-app subscriptions) when present, else `total_app_installs` — the exact
  `total_results in (None, 0, 0.0)` fallback `_should_pause_ad` uses; ROAS/default → `total_purchase_count`.
  `evaluate_action_confidence` now grounds `Evidence.sample_purchases` through it.
- `confidence.py`: `_fmt_purchases` → `_fmt_conversions`; the three `data_strength` factor strings + the
  `render_evidence_line` sample string now read "conversions". `_abstain_action` rationale aligned.
- Serialized `sample_purchases` JSON key + `data_strength(sample_purchases=…)` param intentionally
  left as-is (structural rename deferred to `confidence-sample-conversions-rename`).
- Docs: `META_ACTION_WORKFLOW.md` "Evidence and Confidence" documents goal-aware grounding + the
  retained key.

### Review phase (this pass — fixes applied inline)
- **`confidence.py:297`** — `assess().would_raise` "more purchases …" → "more **conversions** …". This
  is rendered to operators as the brief's `Would raise:` line (`briefs.py:323`) on the action-plan
  confidence path; for an install account it had been telling the operator to get "more purchases"
  they structurally never have. The implementer flagged this as a known gap; it is squarely within
  this ticket's "operator-facing wording → conversions" mandate (the deferred rename ticket only owns
  the symbol/key), so it was corrected here.
- **`review.py:234`** — the review-gate below-floor reason "sample of N purchases / … below the
  M-purchase floor" → "N **conversions** / … M-**conversion** floor". Same action plan, same operator,
  same install-account lie; trivial and test-safe.
- Two regression assertions added (see below) so neither wording can silently revert.

## Review findings

**Read the implement diff with fresh eyes first**, then verified against the source. Disposition:

### Checked — correctness & parity
- **Goal-aware fallback parity** ✅ — `_select_sample_conversions` uses the *byte-identical*
  `ad.get("total_results") in (None, 0, 0.0)` idiom as `_should_pause_ad`, so grounding, pause
  detection, and metric selection (`_select_action_metric`) agree on the conversion signal. The
  raw-value `in (…)` check before `_number()` coercion matches the existing idiom exactly — no new
  string/None-coercion divergence introduced.
- **ROAS isolation** ✅ — ROAS/default path returns `total_purchase_count` unchanged; the new test
  populates `total_results`/`total_app_installs` to 999 and confirms they're ignored.
- **Rename safety** ✅ — `_fmt_purchases → _fmt_conversions` has no callers outside `confidence.py`
  (grepped). Serialized keys/params byte-identical; stored `action_plan.json` files still deserialize.

### Checked — tests
- Implementer's 4 new tests cover happy path (installs → `high`), the subscriptions-vs-installs
  decision tradeoff (3 subs → `low`, not 80 installs), abstain when both signals + spend are thin, and
  ROAS isolation. The `_pause_ad_payload` helper uses `waste_status: "high"`, so `_should_pause_ad`
  fires regardless of goal — fixtures are valid.
- **Added (review):** brief test now asserts `"more conversions" in markdown and "more purchases" not
  in markdown`; review below-floor test now asserts the reason says "conversions", not "purchases".
- Full suite: **283 passed** (`.venv/bin/python -m pytest tests/test_meta_ads_analysis.py`). No
  ruff/mypy/pyright configured (pyproject declares only pytest). No pre-existing failures.

### MAJOR finding — filed as new ticket (`backlog/goal-aware-grounding-other-producers.md`)
The action plan is only one of several grounded producers feeding `confidence.assess`. The others —
`control.py` (set_status enable/pause, both the cold-cite and ops paths), `authoring.py`,
`rotation.py`, and `monitor.py` — still ground `sample_purchases` on the **purchase** metric with **no
goal awareness**. Sharpest case: `control._select_set_status_metric` already picks
`cost_per_app_install` for install goals (goal-aware *metric*) but grounds the *sample* on
`metrics_row.get("purchases")`, and `purchases`/`app_installs` come from distinct key sets — so for an
install account the **write path** has the identical "stuck at low/abstain on a zero purchase sample"
bug this ticket just fixed for the read-only action plan. Deferred (cross-module, needs a shared
helper + design decision on `monitor`) rather than fixed inline — too large for this pass and
overlaps the rename ticket's files.

### Minor wording left deliberately (NOT silent)
- `control.py` / `authoring.py` / `rotation.py` / `monitor.py` / `write_grounding.py` operator strings
  still say "purchases" — these are separate subsystems whose **grounding is also still purchase-only**
  (see the major finding); their wording is accurate to current behavior and will be revisited when
  those producers go goal-aware and/or in the field rename. Fixing the wording alone there would be
  cosmetic-ahead-of-behavior.
- `analyze.py` waste-report prose ("N purchases", `next_7_day_actions`) — a different report path, not
  the confidence engine; "purchases" is the analyze-report metric, out of scope.
- Internal `purchases` locals in `confidence.data_strength` and `review.py` retained — not
  operator-facing; owned by the field-rename ticket.

### Empty categories
- **Resource cleanup / async / concurrency:** N/A — pure synchronous dict transforms, no resources.
- **Type safety:** no new type holes; `float | None` signatures consistent with callers.
- **Performance / scalability:** N/A — O(1) field lookups per ad.

## Follow-up tickets

- `backlog/goal-aware-grounding-other-producers.md` (MAJOR finding above) — extend goal-aware sample
  grounding to control/authoring/rotation (and decide on monitor) via a shared helper.
- `backlog/confidence-sample-conversions-rename.md` (pre-existing) — structural rename of the
  `sample_purchases` field/param/JSON key to `sample_conversions`.
