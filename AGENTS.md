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
- Do not finalize a pause/scale/budget recommendation — structured *or* prose — until it survives the
  **fresh-context adversarial-review pass** (see the **Adversarial-review rule** under Interpretation
  Rules). The reviewer refutes by default and names the failing input; that pass only filters and
  downgrades proposals upstream of approval — it never approves an action, enables a write, or alters
  PAUSED-by-default.
- Do not assume the pixel or Conversions API is healthy just because Meta reports purchases.
- Do not assume revenue is healthy just because Meta reports results or app installs.
- Do not recommend scaling solely off hook rate without downstream conversion evidence.
- Do not collapse missing data into zeros unless the normalized output already did so intentionally.
- Do not execute Meta account changes directly from a written analysis. Use the action workflow: generate `action_plan.json`, require explicit approval, dry-run, then execute approved actions.
- Keep Meta AI / Advantage+ creative features off by default. Do not enable automatic text variations, image expansion, visual touch-ups, generated music, flexible media, or AI-generated creative variants unless a human explicitly requests that exact change.

## Read backend (direct vs MCP)

Meta **reads** flow through a swappable seam, `MetaReaderProvider` (`src/meta_ads_analysis/reader_provider.py`),
selected by the `META_READER_BACKEND` env var: `direct` (default — the live Graph API client) or `mcp`
(route reads through a Meta MCP read server). Default is `direct`, so behavior is unchanged unless an
operator opts in. The MCP path is **reads-only** (`MCPMetaReader`, consumed by the agent runtime with an
injected tool-executor); **writes always use the direct Graph client** and stay behind the
propose → approve → validate_only → execute guardrails. Two MCP options are documented in
[`docs/META_API_SETUP.md`](docs/META_API_SETUP.md): a community token-based server (a candidate,
unvetted entry parked disabled in `.mcp.json`) usable with the current token now, and Meta's official
hosted OAuth server as a config-only drop-in for later. Single-operator with the current token is the
supported path now; OAuth/multi-user is a later concern. Full hybrid-model docs + tool catalog land in
the `hybrid-model-docs-and-tool-catalog` ticket.

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
