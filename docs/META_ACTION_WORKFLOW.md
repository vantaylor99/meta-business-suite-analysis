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

## Unified write workflow (all pipelines)

There are **four** write pipelines, and they all run through the *same* gate. The action plan
(`pause_ad` / `increase_adset_budget`), the control ops (`set_status`, `set_daily_budget`, targeting,
creative), authoring (`create_*`), and rotation (audience swap / Advantage-disable / rename) each:
**propose → review → approve → validate_only → execute → audit.**

```
                 build plan (proposed)           operator           Meta Graph API
                 + grounding + review            edits status        (write client)
  read live  ┌────────────────────────┐  approve   ┌─────────┐  --validate-only   ┌──────────┐
  state ───▶ │ evidence + COMPUTED    │ ─────────▶ │ status: │ ───────────────▶  │ pre-flight│
  (reader)   │ confidence  →  review  │            │ approved│       --execute     │  → write  │
             │ (DEMOTE-ONLY gate)     │            └─────────┘ ───────────────▶  └────┬─────┘
             └────────────────────────┘                                                │
                  │ abstain / refute                                                   ▼
                  ▼                                                          timestamped results
            non-executable /                                                 log (audit trail)
            blocked at apply
```

- **Review is per-pipeline but shares one engine.** Control ops and authoring ops live under
  `plan["ops"]` and are reviewed by `review.review_ops_plan` / `review.review_authoring_plan`. Rotation
  plans carry **no `plan["ops"]`** — their reviewable items live under their own keys
  (`audience_rotation`→`rotations`, `advantage_disable`→`items`, `adset_rename`→`renames`) and are
  reviewed by `review.review_rotation_plan`. All three reuse the same `review_recommendation` core and
  the same demote-only applier; routing a rotation plan through the `ops` iterator would silently review
  nothing, which is exactly what the key-aware wrapper prevents.
- **Grounding is enforced at apply time across the account-changing writes.** `set_status`,
  `set_daily_budget`, authoring creates, **and the rotation family** (`audience_rotation`,
  `advantage_disable`) set `guardrails.requires_grounding: true`, so an approved-but-ungrounded write —
  or one resting on a **cited** below-floor/zero sample (`abstain` band with a sample) — is
  **hard-blocked** at apply (`op_grounding_gap`). A *structural* abstain (no sample cited — an honest
  "no metric to cite", e.g. an Advantage-Audience disable or a rotation built with no metrics) is
  allowed through. `set_creative_features` carries evidence/confidence and runs review but **does not**
  set the flag — its review remains **advisory** (demote-only at propose, no apply-time block).
  Rotation also keeps its own apply-time **live-drift guard**, which runs *before* the grounding gate
  and blocks a write regardless of band (drift-first: a stale plan must be re-proposed).

The **full per-capability write catalog (levels, reversible vs create-only, exact guardrails, and which
CLI proposes each)** is the single source of truth in
[`../AGENTS.md`](../AGENTS.md) under **Hybrid Meta integration** — it is not duplicated here. The reader
backend (direct vs MCP) and auth posture also live there and in [`META_API_SETUP.md`](META_API_SETUP.md).

## MCP guarded-write path (`mcp__meta-suite__*`)

The same guarded flow is now reachable over our **own** MCP server (`meta_mcp_server`, client key
`meta-suite` in `.mcp.json`). This is the important distinction to internalize:

- **`mcp__meta-ads__*`** is the *community* connector. Its write tools fire **immediately** against the
  Graph API — no propose, no dry run, no confidence, no review. Those write tools are **deny-listed** in
  `.claude/settings.json` and stay read-only. (This is the rule the Jun-2026 `$300/day IN_PROCESS`
  incident is named for.)
- **`mcp__meta-suite__*`** is *our* server. Its write tools are **gated by construction** — every one
  routes through `propose → human approve → validate → execute → verify`, PAUSED-by-default, with the
  same grounding + demote-only review as the CLI. They are deliberately **not** deny-listed, because the
  guardrail is a capability boundary enforced *in the server* (see `src/meta_ads_analysis/proposals.py`),
  not a prompt rule an agent can be talked out of. The monthly MCP-write auditor still flags any **new**
  ungated write tool on *either* prefix.

### The tool surface

- **`propose_*`** — build a grounded, reviewed plan and persist it. Returns only a **`plan_id`
  reference** plus a per-item summary (status / confidence band / review verdict / note) — never an
  approvable plan body. Each item comes back `proposed` (or demoted by review) — **no propose tool ever
  emits an `approved` item.** The propose surface spans all four write families, and every one routes
  through the same `execute_plan` gate:
  - **Control-ops** (`plan_type: ops`): `propose_set_status`, `propose_set_daily_budget`,
    `propose_rename`, `propose_set_creative`, `propose_set_creative_features`, `propose_set_age_range`,
    `propose_set_genders`, `propose_set_geo_locations`, `propose_set_placements`, plus the bulk
    `propose_enable_ads` / `propose_pause_ads`.
  - **Authoring** (`plan_type: authoring`, create-only, **every created spending entity forced PAUSED**):
    `propose_create_campaign`, `propose_create_adset`, `propose_create_ad`, `propose_create_video_ad`,
    `propose_duplicate_ad`, `propose_lookalike`. A **net-new** create (campaign / ad set / ad / video ad)
    cites a zero sample → `abstain`, so an approved net-new create is **blocked at apply** until a
    conscious operator override — creating PAUSED is fine, auto-executing a create on no evidence is not.
    `propose_duplicate_ad` instead grounds on the **source ad's** own metric (a proven winner is
    executable). `propose_lookalike` is a **structural abstain** — an audience is inert (no status, never
    PAUSED, never spends), so the gate allows it. **No delete / archive.**
  - **Rotation** (`plan_type: audience_rotation` / `advantage_disable`, reversible): `propose_audience_
    rotation` reads the account's ACTIVE ad sets, rotates each ad set's included custom audience forward
    by `offset`, and recomputes exclusions (grounded on each ad set's fatigue signal at the correlational
    tier); `propose_advantage_disable` turns Advantage Audience **off** on each ad set that has it
    enabled, preserving audiences verbatim (only ever off, never on — a structural abstain the gate
    allows). Bulk ad-set rename is intentionally **not** an MCP tool — the ops `rename` already covers it.
  - **Outcome verification is family-aware.** After execute, `execute_plan` re-reads each touched
    entity: an ops `set_status→PAUSED` emits a `verify_next_day_spend` follow-up; an **authoring** create
    is read back to confirm its `effective_status` is not ACTIVE (a created-ACTIVE entity is a red flag,
    since authoring forces PAUSED); a **rotation** write is read back to confirm the new audiences (and,
    for a disable, `advantage_audience=off`) registered.
- **`preview_plan(plan_id)`** — a local, **write-free** dry run: shows the request each *approved* op
  would send. No Meta write (the reader may re-read live state to build a budget/targeting request).
- **`execute_plan(plan_id)`** — the **only** tool that writes. It loads the plan **by id** (never a
  body handed in by the agent — that is the anti-forgery seam), refuses if the plan was already executed
  or has **zero approved ops**, runs a mandatory **`validate_only` pass first**, aborts on any validation
  failure, then applies the approved ops, writes the audit artifact, and re-reads each entity to verify.

### Approval seam — the human signs, the agent cannot

`execute_plan` consults an `ApprovalGate` before applying. The gate the running server wires
(`proposals.select_approval_gate_from_env`) is the **local, single-operator** approval gate:

- **`HmacApprovalGate`** (when `META_APPROVAL_SECRET` is set): approval is an **HMAC-SHA256 signature
  over the plan's approved content**, keyed by a secret the agent's MCP tool surface never holds. It is
  produced **out-of-band** by the human-run `approve_plan` CLI and verified (constant-time) inside
  `execute_plan`. The agent can freely edit the persisted proposal JSON — but any edit to the approved
  set (adding/removing an approved op, changing a param/target/level, or stripping the
  confidence/evidence the grounding gate needs) changes the recompute and is rejected, and the agent
  cannot produce a matching signature without the secret. The signed payload binds the `plan_id` (blocks
  copying an approval block from plan A onto plan B) and the `approved_at` timestamp (blocks
  forward-dating to defeat the TTL). Approvals expire after `META_APPROVAL_TTL_SECONDS` (default 24 h;
  empty/`0` disables).
- **`DeniedApprovalGate`** (when no secret is set): the seam **fails closed** — `execute_plan` refuses
  every write with setup guidance, while reads (never gated) keep working. This is the deliberate
  opposite of the old forgeable-open default.

Since **no `propose_*` tool ever emits an `approved` item**, a freshly-proposed plan also has zero
approved ops — so even before the gate runs, `execute_plan` would refuse. The gate is what makes a
*hand-edited* `status: approved` unforgeable.

**The local loop is: propose (agent) → review + `approve_plan` (you) → `execute_plan` (agent).**

```powershell
# One-time: generate a secret and set it in BOTH the server shell and your approve shell.
python -c "import secrets; print(secrets.token_hex(32))"   # -> META_APPROVAL_SECRET
# (export META_APPROVAL_SECRET=<hex>   — or point META_APPROVAL_SECRET_FILE at a file holding it.)
# Keep it OUT of the repo. It must never be returned by a tool or written into a proposal/audit.

# Agent proposes over MCP; it hands you a plan_id. You review and approve out-of-band:
approve_plan --plan-id <plan_id> --all          # or one/more --op-id <id> to approve a subset
approve_plan --plan-id <plan_id> --all --yes    # skip the confirm prompt (scripting); security is the secret

# The agent then calls execute_plan(plan_id) over MCP — HmacApprovalGate verifies your signature.
```

`server_info` exposes `approval_required: true` and `approval_configured` (a token-free health signal:
whether a usable secret is set) so an operator can see at a glance whether the gate is armed.

**Residual local limitation (accepted tradeoff, not a bug).** On a single-user machine an actor that can
read the MCP server process's environment or the secret file could forge a signature. That is exactly
what the multi-user, role-based ticket (`mcp-role-based-access-tiers`, Entra ID + server-side approval
state in Azure) removes — and it drops in behind this **same** `ApprovalGate` seam, without rewriting
`execute_plan`.

### Media uploads are NOT exposed (documented exception)

`upload_video` / `upload_image` create inert, **unreferenced** assets and bypass the gate, so they are
**not** MCP tools — the MCP write surface stays 100% gated. The operator uploads via the CLI
(`upload_video` / `upload_image`), obtains asset ids, then the agent proposes
`propose_create_video_ad` / `propose_create_ad` with those already-uploaded asset ids (the `video_id` /
`creative_id` argument). The authoring propose tools accept an id; they never upload.

### PAUSED ≠ delivery stopped — verify next-day spend

A `set_status → PAUSED` write *registering* is **necessary but not sufficient** proof delivery stopped:
same-day spend can still post. `execute_plan` therefore emits a structured `verify_next_day_spend`
follow-up marker for every executed pause, and re-reads each entity's `effective_status` as an outcome
check. Relatedly, pausing an ad set's **last ACTIVE ad** leaves the ad set nominally live but delivering
nothing — so `propose_set_status(status="PAUSED")` on such an ad also proposes a companion pause on the
parent ad set (each op independently approvable). Enabling (ACTIVE) deliberately does **not** cascade.

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

- `evidence`: the deterministic facts behind the call — the metric the decision rests on (ROAS for ROAS-goal accounts, cost-per-install for install-goal accounts), the window, the sample (conversions / spend), the entity, and a `regenerating_query` that reproduces the metric.
- `confidence`: a computed band (`high` / `medium` / `low` / `abstain`) from the shared confidence engine. The band is never free-typed; it is derived from sample size, recency, and how causal the evidence is. Grounding caps data strength, so a large-sample correlational call can never read `high`.

The sample that grounds significance is **goal-aware**, mirroring the metric selection and pause logic so all three agree on what the account's conversion signal is:

- **install-goal accounts** (`maximize_in_app_subscriptions`): in-app subscription results (`total_results`) when present, otherwise app installs (`total_app_installs`). This is why an install account that reports zero purchases is no longer stuck at `low` — its significance now rests on the installs/subscriptions that actually back the call. A handful of subscriptions still grounds on those few (and may honestly stay thin) rather than falling back to a richer install count; the installs fallback is only for "no subscription volume yet".
- **ROAS / default accounts**: purchases (`total_purchase_count`), unchanged.

The operator-facing sample wording is **"conversions"** (goal-neutral — a purchase, a subscription, and an install are all conversions). The serialized JSON key is now `sample_conversions`; the legacy `sample_purchases` key is still accepted on read, so older stored `action_plan.json` files continue to load.

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
  Because an op carries no `action_type`, the gate's `direction` check (scale/pause-vs-goal-target)
  cannot fire here — op-level direction-contradiction is the per-capability ticket's job, since it knows the
  op's semantic. Rotation plans use `plan["rotations"]` / `plan["items"]` / `plan["renames"]` rather
  than `plan["ops"]`, so they have their own key-aware wrapper, `review.review_rotation_plan`, **not**
  `review_ops_plan` (routing a rotation plan through the `ops` iterator would silently review nothing).

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
real band (e.g. `medium`), so re-enabling it is an evidence-backed, reviewable proposal. Enabling an ad
is directionally a **scale-up** (0 → live), so each enable op sets `action_type: enable_ad` — which lets
the review gate's `direction` check fire on it. On a ROAS-goal account with a numeric `target_roas`, a
re-enable whose own cited ROAS sits below target is **refuted** (the same verdict a below-target budget
scale-up gets), so a known loser cannot reach the operator looking as trustworthy as a genuine performer.
This complements the band protection above (which caps over-confidence): the band guards *how* confident
the claim is, the direction check guards *which way* it points. The refutation is a loud, evidence-named
warning, **not** a hard block — `apply_ops_plan`'s gate keys on grounding, not on `review_verdict`, so an
operator who genuinely wants the retest can still set the op to `approved` and execute it. The check is
goal-aware and runs in both polarities: on a ROAS-goal account it keys on `target_roas` (higher is
better); on an install-goal account it keys on `secondary_cost_per_app_install_target` and inverts —
turning ON an ad whose cited cost-per-install sits *above* target is refuted as enabling a loser (and,
on the action/budget surfaces, pausing or cutting an entity whose cost-per-install is comfortably
*below* target is refuted as killing a winner). Each branch fires only with a numeric goal target and
the matching cited metric, so an account with no configured target is never direction-judged. Enabling a campaign or ad set toggles only
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

### Audience rotation grounding (`rotation.*`)

Rotation is a reversible experiment — it swaps which saved audiences each active ad set targets — and
carries grounding like every other write, attached via the shared `write_grounding.attach_op_grounding`
and reviewed by `review.review_rotation_plan`. Both rotation plans set
`guardrails.requires_grounding: true`, so — like ops and authoring — an approved rotation whose fatigue
sample is a **cited** below-floor/zero sample (`abstain` band with a sample) is **hard-blocked at
apply** by `op_grounding_gap`; a *structural* abstain (no sample cited) is allowed through. The
rotation arithmetic and the apply-time live-targeting drift guard are unchanged.

- **Rotations (`build_rotation_plan` → `plan["rotations"]`).** Each item's evidence is the ad set's
  **own** performance over the fatigue window (`--date-from` / `--date-to`, metric by account goal,
  sample + `regenerating_query`), populated by `propose_rotation`'s reader. The band is computed at the
  **`correlational`** tier — "audience fatigue" is an *inference* from a decline, not a controlled
  observation — so even a large sample caps at `medium`; a rotation can never read `high` from a decline
  alone. An ad set with no delivery in the window cites a **zero** sample → `abstain` → review marks it
  `insufficient` at propose **and** the apply-time grounding gate hard-blocks it if approved anyway
  (keep observing; don't rotate on no evidence of fatigue).
- **Causal claims are downgraded.** A rotation rationale asserting the audience *caused* the drop
  (`causal_flag`) is downgraded by the gate's `causal` check — confirming cause needs an A/B, not a
  decline. (Tested in `test_rotation_causal_claim_is_downgraded`.)
- **Advantage-Audience disable (`build_advantage_disable_plan` → `plan["items"]`).** Turning Meta-AI
  audience automation **off** is a safety toggle with no performance metric, so each item is a
  **structural abstain** (named ad set, no cited sample). Review must not refute it for "contradicting
  its metric" (it has none); it `stands` as an honest abstention. Because the plan now sets
  `requires_grounding`, this item also hits the apply-time gate — but a structural abstain is
  gate-**allowed**, so an approved disable still executes. The disable only ever turns automation
  **off**, never on, and the `FORBIDDEN_FRAGMENTS` interaction is unchanged.
- **Renames (`build_rename_plan` → `plan["renames"]`).** A rename writes only the name — no spend,
  delivery, or structural change — so it is **exempt** from grounding (mirroring the `rename` op
  exemption). `review_rotation_plan` passes renames through untouched: no fabricated band, no review
  block.
- **Drift precedence (reversibility).** Review runs at *propose*; at *execute* `apply_rotation_plan`
  runs the live re-read + no-drift validation **first** and the grounding gate **second**. If the live
  targeting drifted since plan time, the write is **blocked regardless of the confidence band** — a
  high-confidence rotation still blocks on drift, and a thin-sample rotation that is *also* drifted
  reports the **drift** reason, not the grounding reason. Because the results log captures each ad
  set's prior audience set, a rotation is reversible: rotating back is simply another rotation.

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

`control.build_budget_plan` proposes a single grounded daily-budget move and runs it through
`review.review_ops_plan`; `apply-ops` then validates/executes it under the same
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
op: on a ROAS-goal account it **refutes** scaling up an entity whose cited ROAS is below the account
target, and **refutes** cutting the budget of a clear winner (ROAS comfortably above target). On an
install-goal account the same check runs inverted against `secondary_cost_per_app_install_target`:
scaling up an entity whose cost-per-install is above target, or cutting one whose cost-per-install is
comfortably below it, is refuted.

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
