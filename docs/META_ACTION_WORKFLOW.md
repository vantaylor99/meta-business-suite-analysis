# Meta Action Workflow

This repo now supports a guarded path from analysis to action:

1. Sync or ingest data.
2. Build the report.
3. Generate `action_plan.json`.
4. Generate `operator_brief.md`.
5. Review and approve specific executable actions.
6. Dry-run the approved actions.
7. Execute only after the dry run matches the operator's intent.
8. Review the timestamped action result log.

## Commands

Generate a plan:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-05-04
```

Generate a plan with current live ad status:

```powershell
python -m meta_ads_analysis propose-actions --account pollen_sense --run-date 2026-05-04 --enrich-live-state
```

Dry-run approved actions:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-05-04
```

Build the operator brief:

```powershell
python -m meta_ads_analysis operator-brief --account pollen_sense --run-date 2026-05-04
```

Execute approved actions:

```powershell
python -m meta_ads_analysis apply-actions --account pollen_sense --run-date 2026-05-04 --execute
```

## Approval Model

Actions are generated with `status: "proposed"`.

Only executable actions with `status: "approved"` are sent to the Meta Graph API. Non-executable actions remain operator tasks, even if their status is changed.

## Evidence and Confidence

Each recommendation-bearing action (`pause_ad`, `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`) carries two structured blocks:

- `evidence`: the deterministic facts behind the call — the metric the decision rests on (ROAS for ROAS-goal accounts, cost-per-install for install-goal accounts), the window, the sample (purchases / spend), the entity, and a `regenerating_query` that reproduces the metric.
- `confidence`: a computed band (`high` / `medium` / `low` / `abstain`) from the shared confidence engine. The band is never free-typed; it is derived from sample size, recency, and how causal the evidence is. Grounding caps data strength, so a large-sample correlational call can never read `high`.

For the executable pause/budget paths, a sample below the significance floor (too few conversions and too little spend) does **not** become a confident pause or scale. The action is flipped to a non-executable `verdict: "insufficient_data"` recommendation — "promising test, keep running and re-check as more data accrues" — with `executable: false` and `approval_required: false`, so thin data can never be approved into a write.

## Grounding on every write path (ops, authoring, rotation)

The same proof-and-trust discipline applies to **all** account-changing writes, not just the action
plan. The other write pipelines are:

- **ops** (`control.apply_ops_plan`): `set_status`, `set_daily_budget`, `set_creative`,
  `set_creative_features`, targeting ops, and `rename`.
- **authoring** (`authoring.apply_authoring_plan`): `create_campaign` / `create_adset` / `create_ad`
  / `create_video_ad` / `create_lookalike`.
- **rotation** (`rotation.apply_rotation_plan` and friends): custom-audience swaps.

Shared scaffolding lets each of these carry grounding uniformly:

- **`write_grounding.attach_op_grounding(op, …)`** attaches a serialized `evidence` block and a
  **computed** `confidence` band to a write op — computed by `confidence.assess` (or
  `confidence.abstain_confidence` when no sample is supplied), never free-typed. With no sample, or a
  sample below the significance floor, the band is `abstain` — never a defaulted `low`/`medium`.
- **`review.review_ops_plan` / `review.review_authoring_plan`** are the op-shaped siblings of
  `review_action_plan`. They iterate `plan["ops"]`, review only ops carrying a `confidence` block
  (informational / structural ops with no band pass through untouched), and are idempotent (an op
  already carrying a `review` block is left as-is). Like the action gate they are **demote-only**:
  they may lower a band and demote an op's `status` from `approved` back to `proposed`, but never raise
  a band, promote a status, or touch PAUSED-by-default. They use the op's own vocabulary
  (`status` + a `review_verdict` marker) rather than the action plan's `executable`/`rationale` keys.
  Because an op carries no `action_type`, the gate's `direction` check (scale-vs-ROAS-target) cannot
  fire here — op-level direction-contradiction is the per-capability ticket's job, since it knows the
  op's semantic. Rotation plans use `plan["rotations"]` / `plan["items"]` rather than `plan["ops"]`, so
  they get their own review wrapper in the rotation work, not `review_ops_plan`.

### Grounding-required set and the apply-time guard

`apply_ops_plan` / `apply_authoring_plan` enforce grounding at the gate, not just by convention. When a
plan opts in via `guardrails.requires_grounding: true`, an **approved** op that is *grounding-required*
is **blocked** before any write is sent when it is not adequately grounded:

- no `confidence` block at all → `blocked` ("approved write missing required evidence/confidence") —
  this closes the hole where a hand-edited plan could approve an ungrounded write;
- an `abstain` band **with a cited sample** (it tried to ground on data but the sample is below the
  floor) → `blocked` ("insufficient data — keep running");
- an `abstain` band with **no** sample (a structural / no-metric op, e.g. a safety PAUSE) → **allowed**:
  this is an honest, deliberate abstention, not a thin-data overclaim, so blocking it would needlessly
  break PAUSED-by-default safety writes.

The grounding-required set is "anything that changes spend / delivery / structure":

- **ops:** every supported op **except `rename`**. A pure rename is cosmetic — no spend, delivery, or
  structural change — so it is exempt. (`control.GROUNDING_REQUIRED_OPS`.)
- **authoring:** **all** create kinds, since every create changes account structure.
  (`authoring.GROUNDING_REQUIRED_KINDS`.) Grounding gates whether a create is *sent*; the create is
  forced `PAUSED` regardless, so the gate never weakens PAUSED-by-default.

The guard is inert on plans that do not set `requires_grounding`, so legacy/ungrounded plans are
unaffected; the grounded per-capability builders set the flag and attach the blocks together. The
`confidence.py` / `review.py` / `write_grounding.py` layers stay pure (no Meta / network / clock): the
live-state reads that build evidence and derive `recency_days` happen in the impure caller and are
passed in.

### Enabling and pausing ads (`set_status` grounding)

`control.build_enable_ads_plan` and `control.build_pause_plan` are grounded producers: they set
`guardrails.requires_grounding: true`, attach an `evidence` + computed `confidence` block to every
`set_status` op, and run the plan through `review.review_ops_plan` before returning it. The evidence
is the toggled ad's **own** performance over a stated window (`--date-from` / `--date-to`, defaulting
to a 30-day trailing window), with the metric chosen by the account goal — ROAS for ROAS-goal
accounts, cost-per-install for install-goal accounts (the same selection `actions._select_action_metric`
uses) — plus the purchases/spend sample, the entity, and a `regenerating_query`.

Enabling and pausing are deliberately **asymmetric** at the no-data boundary, because turning an ad ON
and turning it OFF carry opposite risk:

- **Enable a cold ad** — an ad paused long enough to have no recent insights cites a **zero**
  purchases/spend sample (an honest "spent $0 in the window"). That is below the significance floor, so
  the band is `abstain`, review marks the op `insufficient`, and — because a sample *is* cited — the
  apply-time gate **blocks** the write even if it is approved ("not enough data to safely turn this on —
  keep observing"). A freshly-authored (PAUSED) ad's go-live is the same path: thin/new data abstains,
  so flipping it live is a conscious, reviewed step, never an auto-confident enable.
- **Pause for a structural / safety reason** — a pause selected by name / ad-set filter with no
  performance metric cites **no** sample (a structural abstain). The gate **allows** it, because pausing
  is the conservative direction and blocking it would break PAUSED-by-default safety writes. A
  `--roas-below` pause instead rests on ROAS by construction, so it cites that metric and a computed
  band.

A high-spend ad that happens to be paused still grounds normally: its real window sample computes a
real band (e.g. `medium`), so re-enabling it is an evidence-backed, reviewable proposal. Ops carry no
`action_type`, so the review gate's `direction` check (scale-vs-ROAS-target) does not fire on an
enable; the protection against an enable whose metric contradicts the goal is that its band is computed
from sample strength alone and can never be *over*-confident. Enabling a campaign or ad set toggles only
that node — it does **not** un-pause PAUSED children — so evidence is attached at the level being
toggled. The ad list is read once at propose time, so live `effective_status` may drift before execute;
re-applying `ACTIVE` to an ad that is already ACTIVE is idempotent on Meta's side (and `--validate-only`
pre-flights the change), so the drift does not produce a confusing error.

### Authoring grounding (`create_*`)

`authoring.build_duplicate_ad_plan` (CLI: `propose-duplicate-ad`), `authoring.build_video_ad_plan`
(`propose-video-ad`), and `authoring.build_lookalike_plan` (`propose-lookalike`) are grounded
producers: they set `guardrails.requires_grounding: true`, attach an `evidence` + computed
`confidence` block to every create op via `write_grounding.attach_op_grounding`, and run the plan
through `review.review_authoring_plan` before returning it. Two shapes of justification:

- **Duplicate / scale-out of a proven entity** — the evidence is the **source ad's** own metric over a
  stated window (`--date-from` / `--date-to`, defaulting to a 30-day trailing window), metric chosen by
  the account goal (the same selection enable/pause use). A proven winner computes a real band (e.g.
  `medium`/`high`), so the duplicate is an evidence-backed, executable proposal; a source with no
  delivery cites a **zero** sample → `abstain`. The evidence reflects the *propose-time* justification,
  not a live precondition — the create copies the source creative regardless of any later drift in the
  source's metrics.
- **Net-new create** (a brand-new campaign / ad set / ad, including a fresh video ad — there is no
  entity to measure yet) — it cites a **zero** sample → `abstain`, exactly like the cold-ad enable
  boundary. Review marks the op `insufficient`, and the apply-time gate **blocks an approved net-new
  create**: creating PAUSED is fine, but auto-executing a create on no performance evidence requires a
  conscious operator override (e.g. dropping `requires_grounding`, or grounding it by duplicating a
  proven winner instead). Going live is a separate, separately-gated `set_status ACTIVE` (the enable
  path), never part of the create.

A **lookalike** is the deliberate exception. Its basis is the seed audience's size/quality, which the
metric pipeline does not express as ROAS/conversions, so it cites **no** sample — a *structural*
abstain naming the seed, never a fabricated band. A structural abstain is gate-**allowed**, because
creating an audience is inert: an audience has no status (it is **not** in `authoring.PAUSED_KINDS`)
and never spends, so it need not require the override a spending create does. The seed becomes a
spending decision only when an ad set targets it, which goes through the ops gate on its own.

PAUSED-by-default is non-negotiable and untouched by grounding: `authoring._build_create` forces
`status=PAUSED` for every kind in `PAUSED_KINDS` regardless of any review verdict, and the review gate
is demote-only — so even a `stands` on a high-confidence duplicate, or a `downgrade`/`insufficient` on
a thin one, can never set a created entity to `ACTIVE`. The `_guard_params` / `FORBIDDEN_FRAGMENTS`
Meta-AI / Advantage+ block and the create-only scope (no delete/archive) are likewise unchanged: a
well-grounded create that carries an Advantage+ param is still blocked.

## Account Goals

Account-specific action policy lives in `config/meta_ads_accounts.json`.

- `pollen_sense`: prioritize in-app subscription results first, regardless of cost. When subscription results are sparse, use app installs as the secondary signal with a target of `$3` per install.
- `divine_designs`: optimize toward `3.0` blended ROAS or better.

These goals change both waste detection and scaling recommendations. For example, a Pollen ad can be paused for missing subscription results and exceeding the install target, while a Divine Designs ad is judged primarily against ROAS.

## Executable Scope

The executor supports:

- `pause_ad`: pauses a specific ad with high waste risk or account-policy waste risk.
- `increase_adset_budget`: raises a daily ad set budget only when the proposed action includes the current daily budget, the proposed new daily budget, and the increase stays within the action's `max_increase_percent`.
- `set_daily_budget` (ops, via `propose-budget` / `apply-ops`): a CBO-aware daily-budget change — an **increase OR a decrease**, at the ad set **or** campaign level. See "CBO-aware budget +/-" below.

Budget increases are intentionally capped. The action-plan path can identify the ad set to scale, but live-state enrichment must populate the current daily budget before the executor will build an operation.

Writes go through the Meta Graph API (`MetaMarketingApiClient.update_ad` / `update_adset` / `update_campaign`), so the action workflow runs natively on any platform with no CLI/WSL dependency. Executing actions requires `META_ACCESS_TOKEN` to carry the `ads_management` permission; dry runs and live-state reads only need `ads_read`.

The workflow intentionally does not execute:

- campaign creation,
- ad set creation,
- creative creation,
- creative replacement,
- broad automated rules.

Those can be added later, but they need tighter account-specific controls because broad mutations are harder to unwind than pausing one clearly wasteful ad or applying a capped budget change.

### CBO-aware budget +/- (`control.set_daily_budget`)

`control.build_budget_plan` (CLI: `propose-budget`) proposes a single grounded daily-budget move and
runs it through `review.review_ops_plan`; `apply-ops` then validates/executes it under the same
propose → approve → validate_only → execute gate.

**CBO detection.** Under Meta's campaign-budget-optimization the **campaign** holds the budget and the
ad sets inherit it. When a `set_daily_budget` op (or the action-plan `increase_adset_budget`) finds the
ad set has no `daily_budget`, the code re-reads the **parent campaign** (`classify_adset_budget`) and
classifies:

- **CBO active** (campaign has a daily *or* lifetime budget, ad set has none) → the ad-set op is
  **not** silently blocked. The proposer emits a non-executable ad-set *pointer* op (marked
  `cbo_detected: true` with the `live_campaign_state`) **plus an actionable campaign-level op** carrying
  its **own** campaign metric as evidence (never a copy of the ad set's). At apply time an ad-set op
  that finds CBO is **blocked** ("change the campaign budget instead") — so a campaign that flipped CBO
  state between propose and execute is caught, not mis-applied.
- **Truly broken** (neither the ad set nor its campaign has a budget) → blocked with a clear error.
- **Ad-set-level budget present** → proceed as before (capped increase, now also decrease).

**Increase vs decrease — two separate caps, selected by sign of `(new − current)`:**

- **Increase** uses the op-param `max_increase_percent` (default 20%). Source unchanged.
- **Decrease** uses a separate symmetric guard: `MAX_BUDGET_DECREASE_PERCENT` (config default;
  op-param `max_decrease_percent` overrides; a per-account `max_budget_decrease_percent` in
  `action_policy` is folded into the op-param by the builder) **and** an absolute floor,
  `MIN_DAILY_BUDGET_CENTS` (account minor units), so a reduction can never silently pause delivery.
  Both must hold. Applying the wrong cap to the wrong direction can never happen — the cap is chosen by
  the sign of the change.

**Lifetime budgets.** A daily-budget op cannot edit a lifetime budget. A campaign carrying only a
lifetime budget classifies as CBO-active (the budget *is* at the campaign), but the redirected campaign
op is marked non-executable and **blocked at apply** with "lifetime budget — not adjustable via a
daily-budget op." Budgets are account-currency **minor units** (integer cents); `MIN_DAILY_BUDGET_CENTS`
is a conservative local floor and `--validate-only` surfaces Meta's real per-currency minimum as the
final check.

**Grounding + direction.** Every budget op carries `evidence` (the entity's ROAS / cost-per-result over
the window, with sample + `regenerating_query`) and a computed `confidence` band, and a below-floor
sample abstains into a non-executable "keep running" recommendation (the "9 purchases over 5 days"
guard). Budget ops set an `action_type` (`increase_adset_budget` / `increase_campaign_budget` /
`decrease_adset_budget` / `decrease_campaign_budget`) so the review gate's `direction` check fires on an
op: it **refutes** scaling up an entity whose cited ROAS is below the account target, and **refutes**
cutting the budget of a clear winner (ROAS comfortably above target).

## Meta AI / Advantage+ Policy

Keep Meta AI creative features turned off by default.

The executor only allows explicit status changes for approved V1 actions. It blocks action parameters that try to set Meta AI or Advantage+ creative controls such as:

- Advantage+ creative enhancements,
- automatic text variations,
- image expansion,
- visual touch-ups,
- music generation,
- flexible media or AI-generated creative variants.

Live-state enrichment also checks ad set payloads for signs of targeting automation or Advantage audience controls. When detected, the plan adds a non-executable remediation task so the operator can disable those controls in Meta. Disabling automation is intentionally left as an operator follow-up rather than an automatic write, so the executor never silently changes targeting automation.

If a future workflow needs to create ads, ad sets, or creatives, it should carry this policy forward by making all AI/Advantage+ controls explicit and defaulting them to disabled.

## Result Logs

Each dry run or execution writes:

```text
reports/<account_slug>/<run_date>/action_results_<timestamp>.json
```

Use this file as the audit trail for what was skipped, dry-run, executed, blocked, or failed.

## Fresh Data

Pull fresh data from the Meta Graph API (requires `META_ACCESS_TOKEN`):

```powershell
python -m meta_ads_analysis sync-api --account divine_designs --run-date 2026-06-16
```

This writes the normal raw, normalized, and report outputs by sourcing insights directly from the Graph API.

## Later Phase: Operator Brief

The operator brief turns the generated `action_plan.json` into:

- what changed since the last run,
- what is approved to execute,
- what still needs human judgment,
- and which account goal each action supports.

Under every recommendation the brief also surfaces the action's `evidence` and `confidence`
(carried through from the action plan, not recomputed): a compact `Evidence:` line (the number,
window, sample, and entity), a `Confidence:` band line in the shared 🟢/🟡/🔴/⚪ vocabulary, a
`Re-check:` line with the exact `account_metrics` command that reproduces the number, and what
would raise or lower the band. Abstain actions read as "Insufficient data — keep running" (never a
percentage), and a correlational causal claim shows the "confirm via A/B" caveat plus the offer to
file an experiment via `experiment define`.

Before the brief is assembled, an adversarial **review gate** (`review.py`) re-derives each
recommendation's confidence from its own cited evidence + claimed band — deliberately not trusting
the producing rationale's conclusion — and tries to refute it: sample below the significance floor
(→ insufficient/keep running), a window shorter than `REVIEW_MIN_WINDOW_DAYS` (→ downgrade), a
causal claim from non-experimental data, a band that exceeds what the rubric recomputes, external
evidence read above `low`, and an action whose direction contradicts its own cited metric versus the
account goal (→ refuted). The gate can only ever **demote** (lower a band, flip
executable→non-executable, demote out of `approved`) — it never raises a band or promotes a status,
so it sits upstream of the guarded-write approval and cannot weaken it. Refuted/insufficient calls
appear in their own `Refuted / Downgraded By Review` section (surfaced, never silently dropped); a
downgrade that keeps its section gets a compact `↓ Review:` line. Pass `--no-review` to
`operator-brief` to skip the gate. (Semantic refutations — KB-narrative contradiction, cherry-picked
windows — are the companion `adversarial-review-protocol` doc procedure, not this code gate.)

It writes:

```text
reports/<account_slug>/<run_date>/operator_brief.md
reports/<account_slug>/<run_date>/operator_brief.json
```

Keep `operator_brief_todo` enabled in account policy while this brief is still being refined against real operator use.
