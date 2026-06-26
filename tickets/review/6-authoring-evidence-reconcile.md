description: Proposals to create a new campaign, ad set, or ad now also carry the facts and confidence justifying why they're worth building and pass an automatic second-opinion check — while still always being created switched-off so they never spend on their own.
prereq: guarded-write-evidence-scaffold
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What landed

Reconciled `authoring.py` onto the grounded-write scaffold (no rewrite of the create mechanics).
Every authoring op now carries an `evidence` + **computed** `confidence` block, and every authoring
plan opts into `guardrails.requires_grounding` and is run through `review.review_authoring_plan`
before it is returned. PAUSED-by-default, `_guard_params`/`FORBIDDEN_FRAGMENTS`, and the create-only
scope are untouched.

### Grounding shapes (the core design decision)

- **Duplicate / scale-out** (`build_duplicate_ad_plan`): evidence is the **source ad's** own metric
  over a window, read via the reader (`fetch_entity_metrics`). Proven winner → real computed band
  (executable); undelivered source → zero sample → abstain. New helper `_attach_duplicate_grounding`.
- **Net-new spending create** (`build_video_ad_plan`, and any hand-authored campaign/ad set/ad):
  cites a **zero** sample → `abstain`. Review marks it `insufficient`; the apply-time gate **blocks an
  approved** net-new create. This mirrors the cold-ad enable boundary exactly (`control` uses the same
  zero-sample trick). New helper `_attach_netnew_grounding`.
- **Lookalike** (`build_lookalike_plan`): seed size/quality is not a ROAS/conversions metric, so it
  cites **no** sample — a *structural* abstain naming the seed. Structural abstain is gate-**allowed**
  (audiences are inert: no status, NOT in `PAUSED_KINDS`, never spend). New helper
  `_attach_lookalike_grounding`.

`_wrap_plan` now sets `requires_grounding`/`run_date`/`account_action_policy` and returns
`review.review_authoring_plan(plan)`. The three builders gained `date_from`/`date_to`/`run_date`
(+ `policy` where goal-relevant) params, and the CLI proposers (`propose-duplicate-ad`,
`propose-video-ad`, `propose-lookalike`) gained `--date-from`/`--date-to` and pass `run_date`.

## How to validate

- `.venv/bin/python -m pytest tests/ -q` → **257 passed**.
- Key tests (all in `tests/test_meta_ads_analysis.py`):
  - `test_build_duplicate_ad_plan_grounds_on_proven_winner` — proven winner → computed band ≥ medium,
    verdict `stands`, created PAUSED, copies source creative.
  - `test_authoring_netnew_create_abstains_insufficient_and_non_executable` — net-new campaign
    fixture: abstain → `insufficient` → blocked at apply, nothing created.
  - `test_create_video_ad_builds_object_story_spec_and_pauses` /
    `..._multi_text_uses_asset_feed_spec_...` — net-new video ad blocks under grounding; request shape
    + PAUSED verified via the conscious-override (drop `requires_grounding`) path.
  - `test_authoring_lookalike_structural_abstain_is_creatable` — structural abstain → `stands` →
    creatable.
  - `test_authoring_paused_invariant_holds_even_when_review_stands` — high-confidence duplicate that
    `stands` is STILL created PAUSED.
  - `test_authoring_grounded_create_still_blocks_advantage_param` — Advantage+ param still blocked.
  - `test_authoring_grounded_plan_is_json_serializable` — plan round-trips; results log keeps
    op_id/kind/status/created_id without leaking grounding keys.
  - `test_review_authoring_plan_is_idempotent` — re-review is a no-op.

## Honest gaps / decisions for the reviewer to scrutinize

- **Behavior change (deliberate, ticket-mandated): net-new video-ad creation now BLOCKS at apply**
  when the plan requires grounding and the op is approved (was: created). The ticket explicitly wants
  this ("auto-executing the create on no evidence should require a conscious operator override"). The
  override mechanism is dropping `requires_grounding` from the plan (or grounding via a duplicate).
  The two existing video-ad tests were updated to assert the block AND verify the request shape via
  the override path. **Confirm this matches product intent** — it's the most consequential call here.
  If the desired UX is a softer per-op override flag instead of a hard block, that's a follow-up
  (the scaffold's `op_grounding_gap` has no per-op override today; the enable path has the same hard
  block).
- **Lookalike asymmetry**: lookalike abstains *structurally* (allowed) while other net-new creates
  abstain with a *cited zero* (blocked). Justified by "audiences are inert / not in `PAUSED_KINDS`."
  Sanity-check the asymmetry is intended; it follows the ticket's explicit lookalike guidance.
- **`authoring` now imports private helpers from `control`** (`_resolve_grounding_window`,
  `_status_metric`, plus `fetch_entity_metrics`, `resolve_action_policy`) to avoid reimplementing the
  goal-metric/window logic. `authoring` already imported `FORBIDDEN_FRAGMENTS` from `control`, and the
  dependency is acyclic (`control` never imports `authoring`). Same cross-module-private style control
  already uses with `sync_api`. Flagging the added coupling.
- **Builders are now mildly impure**: the default evidence window uses `date.today()` via
  `_resolve_grounding_window` (same as `control.build_budget_plan`). No live Meta calls — tests pass
  explicit dates or `account_slug=None` (so `resolve_action_policy` does no file I/O). `confidence` /
  `review` / `write_grounding` stay pure.
- **Duplicate-ad reads source metrics at propose time** — a new `fetch_insights` call. The
  `_AuthoringFakeClient` test double gained a `fetch_insights` stub (default empty → abstain). The CLI
  duplicate proposer reads through the live direct client (unchanged from before — it already used the
  client as the reader, not the MCP seam).
- **No standalone create-campaign / create-adset CLI proposers exist** (only video-ad / duplicate /
  lookalike + hand-authored plans applied via `apply-authoring`). I did not invent new commands; the
  net-new campaign/ad-set chain is covered by the apply gate + the net-new fixture test. The
  reviewer should confirm that interpretation of "authoring create proposers" is acceptable.
- **Evidence drift** (source metrics read at propose, may be stale by execute) is documented in the
  `build_duplicate_ad_plan` docstring and the workflow doc as "propose-time justification, not a live
  precondition" — not separately tested.

## Out of scope (unchanged, confirmed)

No delete/archive. PAUSED-by-default (`PAUSED_KINDS` + hardcoded `status=PAUSED` in `_build_create`).
Meta-AI/Advantage+ block. The review gate stays demote-only and never touches PAUSED.
