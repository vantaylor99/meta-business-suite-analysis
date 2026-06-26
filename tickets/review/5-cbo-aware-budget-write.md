description: Budget changes now work when the budget lives at the campaign level (Meta's campaign-budget-optimization) instead of the ad set, can lower budgets as well as raise them (with a floor so they can't be cut to near-zero), and every budget move must carry evidence, a confidence rating, and pass the automatic second-opinion check.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/cli.py, pyproject.toml, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: hard
----
## What landed

CBO-aware, grounded, reversible budget writes. The three coupled deliverables from the locked scope
("CBO-aware budget +/-" + "fix the CBO gap") all landed; full suite **249 passed** (was 228 at the
scaffold; +21 here, of which I added ~14 new budget tests).

### 1. CBO detection + campaign redirect (the locked gap-fix) — `control.py`

- **`classify_adset_budget(reader, adset_id)`** — the shared classifier. Re-reads the ad set; if it has
  a positive `daily_budget` → `adset_level`. Otherwise reads the **parent campaign** and returns
  `cbo_active` (campaign has a daily OR lifetime budget) or `broken` (neither has one). Returns a
  JSON-serializable dict recorded as `live_campaign_state`. This is the single source both the ops path
  and the actions path call, so they classify the same fixture identically (parity contract).
- **`_build_budget_request` (replaces the old `set_daily_budget` branch in `_build_request`)** — at
  apply time re-reads live budget and:
  - ad-set op → re-detects CBO via `classify_adset_budget`. `adset_level` proceeds; `cbo_active`
    **blocks** ("change the campaign budget instead"); `broken` blocks. This means a campaign that
    flipped CBO state between propose and execute is caught, not mis-applied (the re-read-drift guard).
  - campaign op → caps against the campaign daily budget; a **lifetime-only** campaign blocks with
    "lifetime budget — not adjustable via a daily-budget op."
- **`build_budget_plan(...)` (CLI `propose-budget`)** — the grounded producer. For a CBO ad set it emits
  TWO ops: a non-executable ad-set **pointer** (`cbo_detected: true` + `live_campaign_state` + note) and
  an **actionable campaign op carrying its own campaign-level evidence** (not a copy of the ad set's).
  Runs the plan through `review.review_ops_plan`. `_update_entity`'s campaign dispatch already existed.

### 2. Budget DECREASE (reversible control) — `control.py` + `config.py`

- `config.py`: **`MAX_BUDGET_DECREASE_PERCENT = 50.0`** and **`MIN_DAILY_BUDGET_CENTS = 100`** (new).
- `_capped_budget_request` selects the cap **by sign of `(new − current)`**: increases use the existing
  op-param `max_increase_percent` (default 20, **source unchanged**); decreases use the separate
  symmetric cap **and** the absolute `MIN_DAILY_BUDGET_CENTS` floor (both must hold). Decrease cap
  precedence: op-param `max_decrease_percent` → per-account `action_policy.max_budget_decrease_percent`
  (folded into the op-param by `build_budget_plan`) → config default.

### 3. Grounding + direction on every budget move — `control.py` + `review.py`

- Every budget op gets `evidence` (the entity's ROAS/cost-per-result over the window, sample +
  `regenerating_query`) + a computed `confidence` band via `attach_op_grounding`. Below-floor / no-delivery
  sample → `abstain` → review marks `insufficient` → apply-time gate blocks it (the "9 purchases / 5 days"
  guard). Spend floor = `MIN_SCALING_SPEND`.
- Budget ops set an `action_type` (`increase_adset_budget` / `increase_campaign_budget` /
  `decrease_adset_budget` / `decrease_campaign_budget`) so the review gate's `direction` check fires on an
  op (ops otherwise lack `action_type`). `review._direction_contradiction` now also **refutes cutting a
  clear winner** (decrease + ROAS ≥ target × 1.5), mirroring the scale-up-below-target refute. The new
  action types are added to `review._ACTION_SPEND_FLOOR` (all → `MIN_SCALING_SPEND`).

### 4. actions.py parity fix

`_populate_budget_params_from_live_state(action, reader)` now: if the ad set has a budget → populate
`current_daily_budget_cents` as before; if not → call the shared `control.classify_adset_budget` and,
on `cbo_active`, mark the action **non-executable** with a CBO note (Option A) + `cbo_detected` +
`live_campaign_state`. `enrich_action_plan_with_live_state` passes `effective_reader` through.

## How to validate / use cases (mock-only; no live Meta calls)

`.venv/bin/python -m pytest tests/ -q` → **249 passed**. Key new tests (search `-k "budget or cbo or
classify_adset or parity or decrease"`):

- `test_classify_adset_budget_levels` — adset_level / cbo (daily) / cbo (lifetime) / broken.
- `test_build_budget_plan_cbo_redirects_to_campaign_op` — pointer non-executable + campaign op actionable
  with campaign-level evidence + `cbo_redirect_from_adset_id`.
- `test_apply_ops_cbo_active_adset_blocked_at_execute` — re-read drift → blocked, no write.
- `test_apply_ops_budget_broken_blocked`, `test_apply_ops_campaign_lifetime_budget_blocked`.
- `test_apply_ops_budget_decrease_paths_and_caps` — within-floor decrease ok; over-cap decrease blocked;
  below-floor (cap lifted) blocked, isolating the floor.
- `test_build_budget_plan_adset_level_increase_grounded` (normal increase, stands),
  `test_build_budget_plan_thin_sample_abstains_and_is_blocked` (thin → abstain → apply blocked),
  `test_build_budget_plan_review_refutes_scale_up_below_target`,
  `test_build_budget_plan_review_refutes_cutting_a_clear_winner`.
- `test_actions_ops_cbo_classification_parity`, `test_actions_adset_level_budget_populates_current`.
- `test_build_budget_plan_direct_campaign_target`, `test_build_budget_plan_requires_exactly_one_target`.

CLI smoke: `propose-budget --help` parses (mutually-exclusive `--adset-id`/`--campaign-id`, required
`--daily-budget-cents`); registered in `pyproject.toml`.

## Honest gaps / what the reviewer should scrutinize

- **Config-constant overlap with `write-config-registry-controls` (ticket 8).** That ticket (lower in
  the pipeline, NOT a prereq of this one — so I could not assume it had landed) is also specced to add
  `MIN_DAILY_BUDGET_CENTS` / `MAX_BUDGET_DECREASE_PERCENT`. I added them HERE so this ticket is
  self-contained and functional. **Ticket 8 must reconcile to these definitions, not duplicate them**
  (it should detect they exist and only add the registry `max_budget_decrease_percent` field +
  reader-backend default). Flagged so the reviewer/runner doesn't get a duplicate-symbol surprise later.
- **Chosen constant values are judgment calls:** decrease cap default 50%, absolute floor 100 cents
  ($1/day). Conservative; `--validate-only` surfaces Meta's real per-currency minimum. Sanity-check.
- **Per-account decrease override** is read straight off the `action_policy` dict (which already carries
  arbitrary keys), so it works with no registry change today; the formal `MetaAdsAccount` field +
  example JSON are ticket 8's job.
- **Redundant read in the actions path:** `_populate_budget_params_from_live_state` re-reads the ad set
  via `classify_adset_budget` even though `_maybe_add_live_adset_state` already read it. Deliberate —
  guarantees byte-identical classification with the ops path (parity) over saving one mocked read.
- **CBO pointer op is "non-executable" by convention, not a flag.** Ops have no `executable` key; the
  pointer stays `status: proposed` and is blocked at apply by `_build_budget_request`. Confirm there is
  no path that force-executes it (I believe `apply_ops_plan` only sends `approved` ops, and an approved
  pointer still hits the CBO block — covered by `test_apply_ops_cbo_active_adset_blocked_at_execute`).
- **Lifetime-budget redirect:** the redirected campaign op for a lifetime-only campaign is emitted but
  marked non-executable and blocked at apply (no lifetime-budget write capability exists). Decided +
  tested both daily and lifetime CBO.
- **Currency/units:** budgets treated as integer minor units (cents) throughout, matching existing code.
- **Repo has no linter/type-checker** (pyproject has only pytest); tests are the gate.

## End
