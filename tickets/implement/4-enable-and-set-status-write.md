<!-- resume-note -->
RESUME: A prior agent run on this ticket did not complete.
  Prior run: 2026-06-26T01:52:04.854Z (agent: claude)
  Log file: /Users/vantaylor99/Developer/projects/meta-business-suite-analysis/tickets/.logs/4-enable-and-set-status-write.implement.2026-06-26T01-52-04-854Z.log
Read the log to see what was done. Resume where it left off.
If the prior run hit a timeout or repeated error, be cautious not to rush into the same situation.
<!-- /resume-note -->
description: The existing "turn ads on/off" controls now must carry the facts and confidence behind each enable/pause, and pass the automatic second-opinion check, before an operator can approve them — closing the gap where a status change could be approved with no evidence.
prereq: guarded-write-evidence-scaffold
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## Why

Reversible status control already exists: `control.set_status` op (`SUPPORTED_OPS`, levels
ad/adset/campaign), `control.build_enable_ads_plan` (proposes `set_status=ACTIVE` for not-active
ads), and the action-plan `pause_ad`. LOCKED scope keeps these reversible controls — this ticket
**reconciles** them onto the grounding scaffold from `guarded-write-evidence-scaffold` so each
enable/set-status op carries `Evidence` + a computed `Confidence` band and passes `review.py`. It
does NOT add a new capability or duplicate the op.

## What to build

### Ground `set_status` ops

`build_enable_ads_plan` currently emits bare `set_status=ACTIVE` ops with a delivery-issue note. Wire
each op through `attach_op_grounding`:

- **Evidence**: the ad's recent performance over a stated window (metric per account goal — ROAS for
  roas accounts, cost-per-install for install accounts; mirror `actions._should_pause_ad` /
  `evaluate_action_confidence` metric selection), `sample_purchases`/`sample_spend` from that window,
  `entity_level=ad`, `entity_id`/`entity_name`, and `regenerating_query` via
  `build_regenerating_query`. Reads come through the reader provider; live state via the existing
  `_get_entity`/`iter_paginated` path.
- **Confidence**: computed via `confidence.assess` (tier `direct_observation` for an observed
  enable/pause; `abstain_confidence` when no sample) — never free-typed.
- Enabling a paused ad with no recent delivery (no sample) must **abstain** → the gate turns it
  `insufficient`/non-executable ("not enough data to safely turn this on — keep observing"). This is
  the boundary that prevents blindly enabling a cold ad.
- Run the resulting plan through `review.review_ops_plan` before it reaches the operator (or document
  that `apply_ops_plan`'s approval guard + the operator-brief path invokes it).

### `set_status` outside enable-ads

A direct operator-proposed `set_status` (pause an adset/campaign, enable a single ad) via the CLI
must also attach grounding. Update the CLI proposer(s) that build `set_status` ops
(`propose_enable_ads_main`, and any direct set-status path) to populate evidence from live reads.
Where the operator is pausing for a structural/safety reason with no metric, attach
`abstain_confidence` with a clear factor (consistent with the scaffold's no-metric policy) rather
than a fabricated band.

## TODO

- Extend `build_enable_ads_plan` to attach evidence/confidence per op via `attach_op_grounding`
  (metric selection mirrors the account goal; reads via reader provider).
- Ensure the enable plan passes through `review.review_ops_plan` (and the approval guard from the
  scaffold blocks an ungrounded approved enable).
- Update `propose_enable_ads_main` (and direct set-status proposers) in `cli.py` to fetch the window
  metrics needed for evidence.
- Tests (mock-only): a high-waste-but-paused ad proposed for enable carries computed confidence; a
  cold ad (no sample) abstains → insufficient/non-executable; review demotes an over-claimed enable;
  the existing `effective_status != ACTIVE` filter + name/adset scoping still works.
- Update `docs/META_ACTION_WORKFLOW.md` enable/pause section to describe the new evidence/confidence.
- `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Enabling a cold ad** — an ad paused so long it has no recent insights has `None` sample → must
  abstain, not enable confidently. This is the headline boundary; test it.
- **Goal polarity** — enabling on ROAS vs cost-per-install have opposite "good" directions; the
  `direction` review check fires for ROAS goals only today, AND only when the op carries an
  `action_type` + the plan carries `account_action_policy.target_roas` (see the scaffold ticket's
  note that ops lack `action_type`). Don't regress: ensure an enable whose metric contradicts the
  goal is at least not over-confident, and if you want the direction check to fire for enables,
  supply the action-type-equivalent the gate needs.
- **Re-read drift** — `build_enable_ads_plan` reads ad list once; live `effective_status` may change
  between propose and execute. `_update_entity` re-reads at execute (validate_only) — confirm a now-
  already-ACTIVE ad doesn't error confusingly (mirror the `already_resolved` idea from actions).
- **Campaign/adset-level enable** — enabling a campaign cascades to its (paused) children only if
  they're ACTIVE; document that enabling a campaign does not un-pause children, so evidence should be
  at the level being toggled.
- **PAUSED-by-default interaction** — a freshly authored (PAUSED) entity being enabled is the
  intended go-live path; its evidence is necessarily thin (new). Such an enable should abstain unless
  there's prior data — this is correct and must be tested so the go-live path is a conscious,
  reviewed step, not an auto-confident one.
- **Idempotent review** — re-running review on an already-reviewed enable plan is a no-op.