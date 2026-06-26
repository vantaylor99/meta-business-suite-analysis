description: Turning an ad on or off now has to carry the numbers and a confidence rating behind it, and pass an automatic second-opinion check, before an operator can approve it — so no one can flip an ad live (or off) on a hunch with no evidence.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What changed

The two `set_status` builders in `control.py` — `build_enable_ads_plan` (CLI `propose-enable-ads`)
and `build_pause_plan` (CLI `propose-pause-ads`) — are now **grounded producers**, reconciled onto
the `guarded-write-evidence-scaffold`. Each `set_status` op now carries an `evidence` block + a
**computed** `confidence` band, the plan sets `guardrails.requires_grounding: true`, and the plan is
run through `review.review_ops_plan` before it is returned. This closes the gap where a status change
could be approved with no evidence. **No new op or capability was added** — the existing reversible
`set_status` controls were reconciled, per the LOCKED scope.

> Provenance note: this work was committed by the runner's error-recovery commit
> `59ee6ec` (the prior run died on a connection error *immediately before* writing this handoff).
> Three later tickets (`cbo-aware-budget-write`, `hybrid-model-docs-and-tool-catalog`,
> `rotation-apply-time-grounding-gate`) have since landed **on top** of it, all green — so the change
> is already integrated, not a loose branch. Reviewer is treating committed, integrated, test-passing
> code; the diff for *this* ticket is what `59ee6ec` introduced.

### Implementation (control.py)

- New pure helpers: `_status_metric` (mirrors `actions._select_action_metric` — ROAS for `roas`
  goals, cost-per-install for `maximize_in_app_subscriptions`, else whichever metric is present),
  `_resolve_grounding_window` (resolves `date_from`/`date_to` + `recency_days` + `run_date_iso` the
  same way `review` re-derives recency from `plan["run_date"]`, so the gate's recompute is faithful),
  and `_attach_status_grounding` (builds `Evidence` and calls the shared
  `write_grounding.attach_op_grounding`, tier `direct_observation`, `spend_floor=MIN_WASTE_SPEND`).
- `build_enable_ads_plan`: now reads per-ad metrics via `fetch_entity_metrics` (reader provider),
  attaches grounding per op with `cold_cites_zero=True`, sets `run_date`/`account_action_policy`/
  `selection` window + `requires_grounding`, and returns `review.review_ops_plan(plan)`. The
  `effective_status != ACTIVE` filter and the name/ad-set scoping are unchanged.
- `build_pause_plan`: gained `run_date`; attaches grounding with `cold_cites_zero=False`, and returns
  the reviewed plan. A `--roas-below` pause rests on ROAS by construction → cites that metric + a
  computed band. A purely structural pause (name/ad-set filter, no metric) cites **no** sample.
- `control.py` now imports `review` (`from . import account_registry, review`). No circular-import
  issue (`review` is pure; verified by importing `control`, `cli`, end-to-end).

### CLI (cli.py)

- `propose_enable_ads_main`: added `--date-from` / `--date-to`, passes them + `run_date` to the
  builder.
- `propose_pause_ads_main`: passes `run_date` to `build_pause_plan`.
- These are the **only** two CLI paths that construct `set_status` ops (confirmed by grep). There is
  no other direct set-status proposer to update.

### The enable/pause asymmetry (the crux — review this first)

The whole point of the ticket lives in `_attach_status_grounding`'s `cold_cites_zero` branch:

- **Enable a cold ad (`cold_cites_zero=True`, no metrics row):** cites a **zero** sample
  (`sample_purchases=0.0, sample_spend=0.0`) — an honest "this ad spent $0 in the window." Below the
  floor → `assess` abstains → review marks it `insufficient` → and because a sample **is** cited, the
  apply-time gate (`write_grounding.op_grounding_gap`) **blocks** the write even when approved. This
  is the headline boundary: you cannot blindly turn ON an ad with no evidence it still works.
- **Structural / safety pause (`cold_cites_zero=False`, no metric):** cites **no** sample
  (`sample_*=None`). The gate treats this as a *structural* abstain and **allows** it — pausing is the
  conservative direction and blocking it would break PAUSED-by-default safety writes.

The distinction the reviewer must confirm is sound: **cited-zero sample → block (enable)** vs
**no-sample-cited → allow (pause)**. It is enforced by `op_grounding_gap`, exercised end-to-end by the
two apply-time tests below, and is the same rule `rotation-apply-time-grounding-gate` relies on.

## Use cases / validation

Run with `.venv/bin/python -m pytest tests/ -q` (no `python` on PATH; repo uses `.venv`). **278
passed.** No type checker / linter is configured in `pyproject.toml` (`lint_vault` is an unrelated
vault-content CLI, not a code linter).

New tests (`tests/test_meta_ads_analysis.py`):
- `test_enable_ads_paused_ad_with_strong_sample_carries_computed_band` — a high-spend paused ad
  proposed for enable carries the band the rubric **computes** (30 purchases → `medium`), review
  `stands`, `requires_grounding` true. (The "high-waste-but-paused → computed confidence" case.)
- `test_enable_ads_cold_ad_abstains_and_gate_blocks_turn_on` — cold ad (no insights) → `abstain`,
  `review_verdict == "insufficient"`, `sample_spend == 0.0`; approving anyway and calling
  `apply_ops_plan` returns `blocked` with reason "insufficient data". The headline boundary.
- `test_enable_ads_thin_new_ad_abstains_so_go_live_is_a_reviewed_step` — a freshly-authored (PAUSED)
  ad with thin below-floor data abstains, so go-live is a conscious reviewed step.
- `test_review_ops_plan_demotes_overclaimed_enable` — an op claiming `high` on 30 purchases is
  demoted to below-high (`downgrade`); input plan not mutated.
- `test_enable_ads_review_is_idempotent` — re-running review on a reviewed enable plan is a no-op.
- `test_pause_roas_below_carries_grounded_band` — a `roas_below` pause cites `blended_roas` + a real
  (non-abstain) band when spend clears the floor.
- `test_pause_structural_abstains_but_gate_allows_safety_pause` — structural pause → `abstain`,
  `sample_spend is None`; approved + `apply_ops_plan` → `dry_run` (allowed, not blocked).
- Existing `test_build_enable_ads_plan_targets_only_inactive_ads` still passes (the inactive-only
  filter + scoping is intact). `_ControlFakeClient` gained `fetch_insights`; `_enable_client` helper
  seeds insights rows.

Docs: `docs/META_ACTION_WORKFLOW.md` § "Enabling and pausing ads (`set_status` grounding)" describes
the evidence/confidence, the enable/pause asymmetry, the direction-check note, and the re-read-drift
behavior.

## Known gaps / what the reviewer should scrutinize (this is a floor, not a finish line)

- **Mock-only coverage.** Per the ticket, all tests use fake clients. The reader path
  (`fetch_entity_metrics` → `fetch_insights`) is never run against live Meta, so the real insights
  field shapes (`purchase_roas`, `action_values`, `actions`) are *assumed* correct. They match the
  shapes the existing `fetch_entity_metrics`/action tests use, but no integration test pins them.
- **Direction check never fires on enables.** Ops carry no `action_type`, and the review gate's
  ROAS-direction check requires both `action_type` and `account_action_policy.target_roas`. So an
  enable whose cited ROAS *contradicts* a `roas` goal (e.g. re-enabling a clearly-below-target ad on a
  high-spend sample) is **not** actively flagged as wrong-direction — it is only prevented from being
  *over*-confident (the band is computed from sample strength alone). The ticket flagged this as an
  accepted interaction; the reviewer should decide whether "not over-confident" is sufficient
  protection here, or whether a fix/ ticket is warranted to supply the action-type-equivalent the
  direction check needs for enables.
- **No grounded campaign/adset-level enable builder.** `build_enable_ads_plan` emits **ad-level** ops
  only (it iterates `/{ad_account_id}/ads`). A one-off campaign- or adset-level enable has no grounded
  CLI proposer; it would be a hand-authored ops plan, which (lacking `requires_grounding`) the gate
  leaves inert — consistent with "legacy/ungrounded plans unaffected," but worth a conscious call.
  The docs correctly note enabling a campaign does **not** cascade to un-pause children.
- **Re-read drift is documented, not code-enforced.** No explicit `already_resolved` short-circuit was
  added (unlike `actions`). A now-already-ACTIVE ad relies on Meta idempotency + `--validate-only`
  pre-flight rather than a code path. The doc states this; confirm `_update_entity`'s execute-time
  re-read does not surface a confusing error in that case.
- **`cold_cites_zero` zero-sample is a deliberate fabrication-adjacent choice.** A cold ad's
  `sample_*=0.0` is an *honest* zero ("spent $0"), not an invented metric — but it is the one place a
  sample value is synthesized rather than read. Confirm this reads as honest and that
  `op_grounding_gap` keys "block" on *any* cited sample (including zero), not on a positive sample.

## End
