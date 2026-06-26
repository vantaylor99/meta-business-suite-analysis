# Meta Ads Analysis Agent Guide

> **Start here:** before analyzing data or taking any account action, read
> [`knowledge/README.md`](knowledge/README.md) and the relevant
> `knowledge/accounts/<account>/` files. That is the project's durable memory — account
> goals, what we've already changed, what we're testing, and lessons already learned (e.g.
> Advantage+ and dev-mode-app gotchas). Update the knowledge base at the end of any session
> that changes an account or teaches us something.

This file defines how an agent should analyze the normalized Meta ads data and how the final report should be written.

## Purpose

The goal is to help a human operator quickly understand:

- what is wasting budget,
- what is fatiguing or going stale,
- which ads hook well,
- which ads are strong candidates to scale,
- and whether the account's measurement quality makes ROAS trustworthy.

At this stage, prioritize recorded `results` as the primary commercial signal. Treat `app_installs` as the secondary fallback signal when result volume is sparse or subscription value is not yet reliable.

The agent should prefer clarity and actionability over sounding sophisticated.

## Source Hierarchy

Use these sources in this order:

1. `reports/<account_slug>/<run_date>/meta_ads_report.json`
2. `reports/<account_slug>/<run_date>/meta_ads_report.md`
3. `data/normalized/meta_ads/<account_slug>/<run_date>/ad_daily_metrics.csv`
4. `data/normalized/meta_ads/<account_slug>/<run_date>/creative_lookup.csv`
5. raw files in `data/raw/meta_ads/<account_slug>/<run_date>/` only when a detail needs to be verified

Do not infer confidence that is not supported by the exported data — every recommendation states a
band (🟢 High / 🟡 Medium / 🔴 Low, or ⚪ *Insufficient data — abstain*) computed from sample size,
recency, and grounding tier per the **Grounding rule** under Interpretation Rules. Below the
significance floor, abstain — do not guess a low percentage.

## Output Structure

Every written analysis should use this structure:

1. Executive summary
2. Budget waste findings
3. Fatigue and staleness findings
4. Hook-rate and creative-performance findings
5. Scaling candidates
6. Tracking and measurement concerns
7. Recommended actions for the next 7 days

## Metric Definitions

- `hook_rate = video_3s_plays / impressions`
- `hold_rate = thruplays / video_3s_plays`
- `blended_roas = purchase_value / spend`
- `average_order_value = purchase_value / purchase_count`
- `cost_per_result = spend / results`
- `cost_per_app_install = spend / app_installs`
- `fatigue_score` is a composite score based on rising frequency and degrading CTR plus worsening result efficiency, app-install efficiency, or ROAS between prior and recent windows
- `waste_score` is a composite score based on meaningful spend with weak or missing commercial output, prioritizing `results` first and `app_installs` second

## Interpretation Rules

- If an ad has no video metrics, do not pretend it has a hook rate. Treat hook analysis as not applicable.
- If purchase counts exist but purchase value is missing, explicitly say ROAS confidence is low.
- If results exist but purchase value is missing, explicitly say the account is showing outcomes without reliable revenue, so ROAS should not drive the decision.
- If an ad has low spend and weak results, call it `insufficient data` before calling it wasted budget.
- If an ad has strong ROAS but very small spend, call it a `promising test` rather than a clear scale winner.
- If an ad has rising frequency and falling efficiency over enough history, call out fatigue even if it still produces results.
- If export coverage is incomplete, say so plainly.

**Grounding rule (applies to EVERY operator-facing recommendation — including free-text prose, where
no code enforces it).** State four facts and a confidence band, or abstain:

- **Cite four facts:** the **metric**, the **time window**, the **sample size** (purchases/spend, or
  installs for install-goal accounts), and the **entity** (which ad / ad set / campaign). A
  recommendation with no entity and no sample is not a recommendation — it is a guess.
- **Carry a confidence band** in the shared vocabulary — 🟢 High / 🟡 Medium / 🔴 Low, or ⚪
  *Insufficient data — abstain* — **with its rationale**. The rationale rests on two axes:
  **data strength** (sample size, statistical significance, recency) and **grounding tier**
  (`ab_experiment` > `direct_observation` > `correlational` > `external` > `model_inference`).
  **The weaker axis caps the band** — a huge but correlational sample cannot read High, and a perfect
  A/B on three conversions cannot either.
- **When the data floor isn't cleared, abstain.** Below the significance floor in `confidence.py`
  (25 conversions / $100 spend), say "insufficient data — keep running." **Never invent a
  winner or a loser to avoid abstaining** — abstention is a blessed, correct output, not a failure.
- **Causal-language guard:** a recommendation that asserts a *cause* ("X drives ROAS", "the lift is
  *because* of Y", "Z *leads to* purchases") from non-experimental data must be labeled
  **"correlational — confirm via A/B"** and downgraded one band. Only a completed A/B experiment
  grounds a causal claim at full strength.

This is the human-/agent-facing mirror of what the code now enforces structurally. The **canonical
computation** is [`src/meta_ads_analysis/confidence.py`](src/meta_ads_analysis/confidence.py); the
**shared vocabulary** is the "Confidence & evidence" rubric in
[`knowledge/README.md`](knowledge/README.md). There is **one** confidence language across prose,
README, and code — never introduce a second scale.

**Adversarial-review rule (every pause / scale / budget recommendation must survive a fresh-context
refutation pass before it reaches the operator).** A recommendation that drives an account action —
a pause, a budget scale, a creative refresh — is not finalized until a second reviewer, working from
*only* the recommendation and its cited basis (the **metric / window / sample / entity** and the
claimed **band**) and **NOT** the reasoning that produced it, tries to **refute** it. The reviewer is
conservative: **when uncertain it downgrades or refutes — it never passes.** Every verdict must name
the specific input that fails; a vague "looks fine" is not a verdict. The four verdicts (the same
taxonomy [`src/meta_ads_analysis/review.py`](src/meta_ads_analysis/review.py) emits):

- **stands** — survives the challenge; reaches the operator unchanged.
- **downgrade** — earned a lower band; corrected to the **new band** (named) before the operator sees it.
- **refuted** — the call contradicts its own evidence; dropped (surfaced in the brief's "Refuted /
  Downgraded By Review" section, never silently deleted).
- **insufficient (abstain)** — cited basis is below the significance floor; becomes ⚪ "insufficient
  data — keep running," never a 🔴 Low call.

**The deterministic half of this pass is already enforced by code.** `review.py` re-derives the band
from the cited evidence via `confidence.assess` and runs the *arithmetic / structural* refutations —
sample-floor, window-length, causal-cap, band-earned, scale/pause direction, external-cap — returning
the most-conservative verdict. The agent's job in this pass is the **semantic** refutations code
cannot make (below). This pass only **filters and downgrades** what gets proposed: it **never approves
an action, never flips an action executable, and never touches PAUSED-by-default** — it sits *upstream*
of the guarded-write approval and cannot weaken it. **Abstention is a blessed output** — "insufficient
data to recommend — keep running" is correct, and there is no pressure to pass a call.

**What the fresh-context reviewer checks (the semantic refutations — each must name the failing
input):**

- **Contradicts the knowledge base.** Does the call conflict with a learning in
  [`knowledge/learnings.md`](knowledge/learnings.md) or a decision in
  `knowledge/accounts/<slug>/decision-log.md` — e.g. recommending a tactic a logged experiment
  already refuted? → **refute or downgrade**, citing the conflicting entry.
- **Cherry-picked window.** Is the cited window unusually short, or positioned over a *known
  relearning / recently-changed period* (cross-check `decision-log.md` and the ~5-day watch grace
  window)? The reviewer **may re-pull the same metric over a longer / standard window** — run the
  `account_metrics …` command already attached to the evidence as its `regenerating_query` — to see
  whether the call flips. Re-reading the same source is allowed; **inventing a contradicting number is
  not.** → **downgrade** with "window may be unrepresentative; widen the window." (The code gate is
  read-only and never re-pulls; this re-pull is the agent's job alone.)
- **Prose recommendations.** The narrative `next_7_day_actions` lines and any free-text analysis get
  the **same treatment as structured actions** — no schema enforces grounding on prose. If a prose
  call lacks the four facts, or its implied confidence isn't earned, **downgrade or refute** it.
- **External-as-confirmation.** If any web / external evidence is being treated as *confirmation* of a
  live call rather than a *hypothesis*, **refute** it and route it to `experiment define` — this
  enforces the external-evidence rule (above, and in `knowledge/README.md`) at review time.
- **Confidence earned.** Independently sanity-check that the stated band is justified by the rubric
  inputs. The code gate does this arithmetically; the reviewer catches the cases that hinge on
  judgment the rubric can't encode.

**Anti-rubber-stamp structure (the reviewer is itself an AI and could just agree — these mitigations
are mandatory, not optional).** Mirror the TESS adversarial-reviewer discipline:

- **Fresh context** — give the reviewer only the recommendation + its cited basis, never the producing
  conversation or reasoning.
- **Refute-by-default / downgrade-when-uncertain** — the burden is on the call to survive; ties go to
  the more conservative verdict.
- **Name the specific failing input** — a verdict without a named input (metric / window / sample /
  entity / band) is not acceptable.
- **No fabricated data** — reason over the cited basis; re-pull the *same* metric to check, but never
  invent a contradicting number.

**Materiality and audit trail.** Review the calls that drive a **pause / scale / budget** action — not
trivial informational lines; the code gate already encodes this threshold (it reviews only
confidence-bearing actions and passes informational ones through untouched). This pass may be run as a
**TESS-style review stage** so it leaves an audit trail of what was filtered and why.

## Severity Heuristics

- High waste risk:
  - high spend,
  - zero or near-zero primary results,
  - weak app-install support when results are sparse,
  - or clearly poor efficiency versus the rest of the account
- High fatigue risk:
  - enough history,
  - recent frequency up,
  - recent CTR down,
  - and either cost per result worse, cost per app install worse, or ROAS weaker
- Strong scaling candidate:
  - enough spend to matter,
  - strong result efficiency,
  - low fatigue,
  - and no obvious tracking issue

## Tone

- Be direct and business useful.
- Avoid jargon when a plain statement is clearer.
- Prefer statements like `This ad is absorbing spend without producing proportional value` over vague commentary.
- Separate findings from uncertainty.

## Guardrails

- Do not claim causal certainty from export data alone — export data is correlational. Apply the
  **causal-language guard** (see the Grounding rule under Interpretation Rules): label any
  cause-claim "correlational — confirm via A/B" and downgrade its band. Only a completed A/B
  experiment (`experiment define` → `experiment readout`) grounds a causal claim at full strength.
- Do not present a recommendation without its four facts (metric / window / sample / entity) and a
  confidence band; when the sample is below the significance floor, abstain ("insufficient data —
  keep running") rather than manufacture a confident call.
- Do not let external/web evidence raise the confidence of a live recommendation. External findings
  are hypotheses for the experiment queue, capped at the `external` grounding tier — see "External
  evidence" in [`knowledge/README.md`](knowledge/README.md).
- Do not finalize **any** account-changing write — the action plan *and* the control-ops, authoring,
  and rotation pipelines, structured *or* prose — until it survives the **fresh-context
  adversarial-review pass** (see the **Adversarial-review rule** under Interpretation Rules). Evidence,
  a **computed** confidence band, and a `review.py` pass are required on every grounded write, not just
  the action plan; below the significance floor, abstain (the gate hard-blocks an approved-but-thin
  `set_status` / `set_daily_budget` / create). The reviewer refutes by default and names the failing
  input; that pass only filters and downgrades proposals upstream of approval — it never approves an
  action, enables a write, or alters PAUSED-by-default. See the **Hybrid Meta integration** section for
  the full write catalog and per-capability gates.
- Do not assume the pixel or Conversions API is healthy just because Meta reports purchases.
- Do not assume revenue is healthy just because Meta reports results or app installs.
- Do not recommend scaling solely off hook rate without downstream conversion evidence.
- Do not collapse missing data into zeros unless the normalized output already did so intentionally.
- Do not execute Meta account changes directly from a written analysis. Use the action workflow: generate `action_plan.json`, require explicit approval, dry-run, then execute approved actions.
- Keep Meta AI / Advantage+ creative features off by default. Do not enable automatic text variations, image expansion, visual touch-ups, generated music, flexible media, or AI-generated creative variants unless a human explicitly requests that exact change.
- Budget-decrease operations are double-gated by two safety knobs in `config.py`: `MAX_BUDGET_DECREASE_PERCENT` (default 50 — a single op may not cut the live budget by more than this percent) and `MIN_DAILY_BUDGET_CENTS` (absolute floor in account minor units). A per-account override (`max_budget_decrease_percent` in `action_policy`) narrows the percent cap further; `validate_only` against Meta enforces the real per-currency minimum as the final check.

## Hybrid Meta integration (read model · auth · write catalog)

This is the single reference for how the agent and a fresh operator should think about the Meta
integration: where reads come from, what auth we actually run on, and the full set of guarded writes.
The deep procedural docs are [`docs/META_ACTION_WORKFLOW.md`](docs/META_ACTION_WORKFLOW.md) (workflow +
diagram) and [`docs/META_API_SETUP.md`](docs/META_API_SETUP.md) (MCP setup); the write catalog below is
the **single source of truth** — those docs cross-link here rather than re-listing it.

### Read model (hybrid, swappable)

Meta **reads** flow through a provider seam, `MetaReaderProvider`
(`src/meta_ads_analysis/reader_provider.py`), selected at every `*.from_env()` construction point by the
`META_READER_BACKEND` env var (`reader_from_env`):

- **`direct` (default)** — `DirectMetaReader`, a 1:1 pass-through to the live Graph API client
  (`MetaMarketingApiClient`). Unset or `direct` is byte-for-byte today's behavior; nothing changes
  unless an operator opts in.
- **`mcp` (opt-in)** — `MCPMetaReader`, which routes each read to a token-based Meta MCP read server's
  equivalent tool and translates the result back into the exact shapes `DirectMetaReader` returns. It is
  consumed by the **agent runtime**, which injects the MCP tool-call surface
  (`MCPMetaReader(tool_executor=…)`); the pure-Python CLI cannot synthesize that surface, so a CLI run
  with `META_READER_BACKEND=mcp` raises a clear error rather than silently degrading.

**The community MCP server is wired as config only, and as an *unvetted placeholder*.** It sits in
`.mcp.json` under the non-launched `_candidateMcpServers` key (`meta-ads-mcp-server@1.5.1`,
token-based), so **it is present but not started** — only `code-search` launches. Enabling it requires
an operator to vet the package, pin a known-good version, confirm its token env var, move it under
`mcpServers`, and set `META_READER_BACKEND=mcp`. This is **not** a live, vetted integration today. The
candidate covers 8 reads (`fetch_insights`, `fetch_ads`, `list_campaigns`, `get_campaign`,
`list_adsets`, `get_adset`, `get_ad`, `get_account`); the rest (`list_custom_audiences`,
`get_delivery_estimate`, `search_targeting`, `list_pixels`, `list_custom_conversions`, and the raw
`iter_paginated` escape hatch) are **not** mapped and raise a clear `NotImplementedError` naming the
read — fall back to `direct` for those.

Meta's **official hosted OAuth MCP server is a documented drop-in for later** — same seam, config-only,
**no code change** (see `docs/META_API_SETUP.md`). It is **not wired or tested here**; only the seam is
proven to support it.

**Writes are deliberately not part of this seam.** `create_*` / `update_*` / `upload_*` always use the
direct Graph client and stay behind the propose → approve → validate_only → execute gate, so the MCP
read path is reads-only and the existing `ads_read` token is enough for it (writes still need
`ads_management` + `--execute`).

### Auth posture (single operator now; multi-user later, not built)

We run as **one operator** today, authenticated with the current **long-lived `META_ACCESS_TOKEN`** (a
user/system-user token, no OAuth). That is the **only supported auth path right now** — for both the
direct client and the community MCP candidate (which reads the same token). **Multi-user auth / OAuth /
per-user login is a documented later concern that is not built.** When it is wanted, it plugs in at two
points and needs no rewrite of any call site: the read side swaps in the official OAuth MCP server at
the `MetaReaderProvider` seam, and the write side would need a future **per-user token store** feeding
`client_from_env`'s token lookup. Until then, assume one token, one operator.

### Write tool catalog

Every guarded write capability and its guardrails. All writes are **reversible or create-only —
there is no `delete` / `archive` anywhere** (`control.py` excludes them by design). "Pipeline" names the
apply entry point and the proposing CLI; commands are `python -m meta_ads_analysis <cmd>` unless noted.

| Capability | Pipeline · CLI | Level | Kind | Grounding & gate |
|---|---|---|---|---|
| `pause_ad` | action plan · `propose-actions` → `apply-actions` | ad | reversible | Grounded (evidence + computed confidence) + `review_action_plan`; thin sample → non-executable "insufficient". |
| `increase_adset_budget` | action plan · `propose-actions` → `apply-actions` | adset | reversible | Grounded + reviewed; needs live current budget; capped by `max_increase_percent`. |
| `set_status` (enable / pause) | ops · `propose-enable-ads` / `propose-pause-ads` → `apply-ops` | ad / adset / campaign | reversible | **Grounded + hard apply-time gate** (`requires_grounding`) + `review_ops_plan`. Cold-ad enable cites a zero sample → abstain → **blocked**; structural safety pause cites no sample → allowed. |
| `set_daily_budget` (+/-, CBO-aware) | ops · `propose-budget`¹ → `apply-ops` | adset / campaign | reversible | **Grounded + hard apply-time gate** + reviewed. CBO ad-set op redirects to a campaign op; increase cap `max_increase_percent` (20%), decrease cap `MAX_BUDGET_DECREASE_PERCENT` (50%) **and** floor `MIN_DAILY_BUDGET_CENTS`; direction check refutes scaling a loser / cutting a winner. |
| `set_creative_features` | ops · `propose-creative-features` → `apply-ops` | ad | reversible | **Approval-gated only** (`requires_explicit_approval`) — this builder does **not** attach grounding. `FORBIDDEN_FRAGMENTS` still blocks Advantage+/AI params; opt-in is additive, opt-out is text rewriting. |
| `set_creative` | ops · hand-authored (no CLI proposer) | ad | reversible | In the grounding-required set, but no grounded proposer ships; applied via `apply-ops` under the universal gate. |
| targeting: `set_age_range` / `set_genders` / `set_geo_locations` / `set_placements` | ops · hand-authored (no CLI proposer) | adset | reversible | Read-modify-write preserves other fields; in the grounding-required set, no grounded proposer ships; applied via `apply-ops`. |
| `rename` (op) | ops · hand-authored | ad / adset / campaign | reversible | **Exempt from grounding** (cosmetic — no spend/delivery/structure change). |
| `create_campaign` / `create_adset` | authoring · hand-authored → `apply-authoring` | campaign / adset | create-only | **Grounded + hard apply-time gate** + `review_authoring_plan`; forced **PAUSED**; net-new cites zero sample → abstain → approved create **blocked** unless the operator drops `requires_grounding`. |
| `create_ad` | authoring · `propose-duplicate-ad` → `apply-authoring` | ad | create-only | Grounded by the **source ad's** own metric (proven winner → executable; undelivered source → abstain → blocked); forced **PAUSED**. |
| `create_video_ad` | authoring · `propose-video-ad` → `apply-authoring` | ad | create-only | Net-new → zero sample → abstain → blocked unless overridden; forced **PAUSED**. (Asset comes from `intake-video` / `upload-video`.) |
| `create_lookalike` | authoring · `propose-lookalike` → `apply-authoring` | audience | create-only | **Structural abstain** (seed size/quality is not a ROAS/conversions metric) → gate-**allowed**; an audience is inert — **no status, not PAUSED, never spends**. |
| `audience_rotation` | rotation · `propose-rotation` → `apply-rotation` | adset (targeting) | reversible | Grounded at the **`correlational`** tier (caps at `medium`) + `review_rotation_plan` + **hard apply-time gate** (`requires_grounding`): an approved rotation citing a below-floor/zero fatigue sample → abstain → **blocked**; a structural abstain (no metrics supplied) is allowed. The apply-time **live-drift guard runs first and blocks regardless of band**. Prior audience set is logged, so a rotation is reversible. |
| `advantage_disable` | rotation · `propose-disable-advantage` → `apply-disable-advantage` | adset (automation) | reversible | **Structural abstain** + reviewed + `requires_grounding` apply-time gate — a structural abstain is gate-**allowed**, so an approved disable still executes. Only ever turns Advantage-Audience automation **off**, never on. |
| `adset_rename` | rotation · `propose-renames` → `apply-renames` | adset | reversible | **Exempt from grounding** (writes only the name); passes through review untouched. |

¹ **`propose-budget` ships only as the `propose_budget` console script** (`pip install -e .`); it is
**not** wired into the `python -m meta_ads_analysis` dispatcher, so `python -m meta_ads_analysis
propose-budget` currently fails. Tracked in `tickets/backlog/wire-propose-budget-into-m-dispatch`.

**One write class sits *outside* the propose→approve gate: media-library uploads.** `upload-video`
(`MetaMarketingApiClient.upload_video`) and the image upload used during ad authoring
(`upload_image`) push an asset to the account immediately — no plan, no review, no `--execute`. They
are intentionally ungated because the result is an **inert, unreferenced media asset**: it carries no
status, no budget, and no delivery, and nothing spends or serves until a *gated* `create_*` ad
references it. `intake-video` is purely local (transcription), not a Meta write. So the "every write
is gated" framing above and in the README applies to every write that changes spend / delivery /
structure / status; media uploads are the deliberate exception.

**The universal gate (every row above).** Writes are only ever **proposed**; an operator changes a
plan's status to `approved`; nothing reaches Meta without `--execute` (and `--validate-only` pre-flights
the change against Meta first); every dry-run/validate/execute appends a **timestamped results log** as
the audit trail. PAUSED-by-default holds for all created entities (`authoring._build_create` hardcodes
`status=PAUSED`); `FORBIDDEN_FRAGMENTS` blocks Advantage+/Meta-AI params on every write. Where a grounded
producer is used, the op carries an `Evidence` block + a **computed** `Confidence` band (never
free-typed — `confidence.assess` or an explicit `abstain`) and is run through the matching `review.py`
gate, which is **demote-only**. The hard apply-time grounding gate (`op_grounding_gap`, fired when the
plan sets `requires_grounding`) blocks an approved-but-ungrounded write for `set_status` /
`set_daily_budget` / authoring creates; rotation and `set_creative_features` are reviewed/approval-gated
but do **not** yet have that apply-time block (noted above).

### Where the review gate lives relative to the write gate

`review.py` sits **upstream** of the guarded-write approval and can only **demote** (lower a band,
flip executable→non-executable, demote `approved`→`proposed`) — it never raises a band, promotes a
status, enables a write, or touches PAUSED-by-default. Control/authoring ops live under `plan["ops"]`
and are reviewed by `review_ops_plan` / `review_authoring_plan`; rotation-family plans carry their
reviewable items under their **own** keys (`rotations` / `items` / `renames`, never `plan["ops"]`) and
are reviewed by `review_rotation_plan` — routing a rotation plan through the ops iterator would silently
review nothing.

## Tickets (tess)

This project uses [tess](tess/) for AI-driven ticket management.
Read and follow the ticket workflow rules in tess/agent-rules/tickets.md.
Tickets are in the [tickets/](tickets/) directory.

## Code search (tess)

**First tool** for any "where / how / why" question about this codebase: the local code-aware index wired to `mcp__code-search__*`. Reach for `grep`/`Glob` only when you already know the exact filename or literal string. Pick the right sub-tool — they are not interchangeable.

**Decision rule:**

- Query is identifier-shaped (any single symbol, camelCase, snake_case, or a list of names like `fooBar bazQux`)? → `find_references`.
- Query is prose ("where do we evict pages", "what handles JWT refresh", you don't yet know the identifier)? → `search_code`.
- About to run more than one `grep` to reconstruct context? → run `search_code` first instead. That is the moment it pays off, even when you already know an identifier.

`search_code` embeds the query as natural language. Identifier-bag queries can still work when the identifiers co-locate in real code, but prose phrasing is more reliable. If `search_code` returns a weak-top warning, the relative-percentage ranking is unreliable — switch to `find_references` or rephrase as prose, do **not** trust the ordering on noisy results.

**Tools:**

- `search_code(query, k?, path_filter?)` — semantic search. Scores are relative within each result set, not absolute. `k` defaults to 5 (max 50) — raise it for broad sweeps, lower it when you know the top hit is enough. `path_filter` is a SQL LIKE pattern, e.g. `"packages/lamina/%"`.
- `find_references(symbol, max?, path_filter?)` — literal substring; `|` ORs alternatives (`Foo|Bar`). Returns every hit (capped by `max`, default 50, max 500). This is the indexed replacement for `grep` on identifiers.
- `read_chunk(path, start_line, end_line)` — expand a snippet from either tool without a separate `Read`.

**Fallbacks:**

- Use `grep`/`Glob` only for filename patterns, regex with anchors/lookarounds, or when you need *every* literal hit (the index is chunk-granular and may miss adjacent matches inside one chunk).
- Never fall back to `grep` when `find_references` would suffice — it's strictly slower and pulls more bytes.

**What's indexed:** project source files tracked by git, minus `node_modules/`, `dist/`, `build/`, `.git/`, `tickets/`, `team/`, `docs/`, and a few cache dirs. If a query about prose-heavy material (long-form architecture docs, design notes, READMEs in nested folders) returns nothing, the file may be outside the indexed set — fall back to `Read`/`Glob` for those paths. Projects can override the filter via `tickets/index-config.json` (see tess README § Customize what gets indexed).
