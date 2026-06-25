description: A second agent with fresh eyes must now try to disprove every pause/scale/budget recommendation before the operator sees it — checking the things a calculator can't (does it clash with what we already know, was the time window cherry-picked, is a plainly-written recommendation actually grounded) — and downgrade or drop any call that can't survive the challenge. This change is documentation only.
prereq:
files: AGENTS.md, knowledge/README.md, src/meta_ads_analysis/review.py, src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/briefs.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped (documentation-only — no code touched)

This ticket adds the **agent-followed procedure** for the *semantic* half of the adversarial review —
the refutations that code cannot make. It is the doc mirror of `review.py`, exactly as
`grounding-rules-and-external-evidence` was the doc mirror of `confidence.py`. The repo has no
LLM-invocation code (it emits reports an agent/human reads), so this layer is a rule the agent
follows, not Python. Both prereqs (`adversarial-review-gate`, `grounding-rules-and-external-evidence`)
are landed in `complete/`, so the prose describes the system as actually built.

### `AGENTS.md` — new **"Adversarial-review rule"** block (under Interpretation Rules, directly after
the Grounding rule, so the two doc-mirrors of code sit together) + a cross-referencing **Guardrails**
bullet. It defines:

- The rule: every pause/scale/budget call must survive a **fresh-context** refutation pass — reviewer
  sees only the recommendation + cited basis (metric/window/sample/entity + claimed band), **never**
  the producing reasoning — before it reaches the operator.
- The four verdicts, matching `review.py` exactly: **stands / downgrade (new band named) / refuted /
  insufficient (abstain → ⚪ "keep running")**.
- The split: deterministic refutations are **already enforced by `review.py`** (sample-floor,
  window-length, causal-cap, band-earned, direction, external-cap); the agent does the **semantic**
  ones.
- The five semantic checks: KB contradiction (`learnings.md` / `decision-log.md`), cherry-picked
  window (with the **allowed re-pull** of the *same* metric via the evidence's `regenerating_query` /
  `account_metrics` — re-reading allowed, inventing a number forbidden), prose recommendations
  (`next_7_day_actions` and free-text get the same treatment as structured), external-as-confirmation
  (→ `experiment define`), confidence-earned.
- The anti-rubber-stamp structure (fresh context · refute-by-default/downgrade-when-uncertain · name
  the failing input · no fabricated data) and the materiality/TESS-style-stage note.
- The invariant: this pass **only filters/downgrades upstream of approval** — never approves an
  action, enables a write, or touches PAUSED-by-default; abstention is blessed.

### `knowledge/README.md` — new **two-layer review** subsection under "Confidence & evidence"
(`review.py` = deterministic layer; the AGENTS.md rule = semantic layer; both speak the same
🟢/🟡/🔴/⚪ vocabulary and the same four verdicts — one language, never two).

## Use cases / what the reviewer should verify

This is a prose contract over existing code, so the review is a **read-for-accuracy + read-for-
contradiction** pass, not a behavioral test. Concretely, confirm:

- **One vocabulary, no second scale.** The band emoji/labels (🟢 High / 🟡 Medium / 🔴 Low / ⚪
  Insufficient data — abstain) in the new AGENTS.md block and the new README subsection match
  `BAND_PRESENTATION` in `confidence.py`. (Pinned today by
  `test_band_vocabulary_actually_appears_in_knowledge_readme`.)
- **Verdict taxonomy is faithful.** The four verdict words match `review.py`'s `VERDICT_*` constants
  (`stands`/`downgrade`/`refuted`/`insufficient`) and the most-conservative-wins / downgrade-landing-
  on-abstain-becomes-insufficient behavior in `_resolve`.
- **Named checks match the code's actual checks.** The six deterministic checks the doc attributes to
  `review.py` (sample-floor, window-length, causal-cap, band-earned, direction, external-cap) are
  exactly the six in `review_recommendation`.
- **The re-pull claim is accurate.** `review.py` is read-only and **never** re-pulls metrics (its
  docstring explicitly defers that to *this* doc-procedure). The doc correctly assigns the same-metric
  re-pull to the **agent**, not the code. Verify the doc doesn't imply the *code* re-pulls.
- **Demote-only / upstream-of-write-gate is stated correctly.** The doc must not claim the pass can
  approve, enable a write, or alter PAUSED-by-default — matches `_apply_verdict` (demote-only) and the
  write gate keying on `executable` + `status == approved`.
- **Referenced symbols/commands exist** (all verified during implement): `next_7_day_actions`
  (`analyze.py` / `reporting.py`), `account_metrics` + `regenerating_query` (`confidence.py` /
  `briefs.py`), `experiment define` (`cli.py`), the ~5-day watch grace window (`monitor.py`).
- **Materiality claim is accurate.** `review_action_plan` reviews only confidence-bearing actions
  (`pause_ad`, `increase_adset_budget`, `consider_scale_budget`, `refresh_creative`) and passes
  informational ones through — the doc's "code gate already encodes this materiality threshold."

## Validation performed

- `.venv/bin/python -m pytest tests/ -q` → **141 passed** (unchanged from HEAD; doc-only edits don't
  touch the vocabulary/tier pin tests). Repo ships no ruff/mypy/flake8 and no CI; pytest is the only
  gate (consistent with prereq tickets).
- Verified every cited symbol/command/field exists in the tree (greps above).
- Re-read both edited docs end-to-end against `review.py` + `confidence.py` for one vocabulary and no
  contradiction.

## Known gaps / honest flags for the reviewer

- **No test pins the new AGENTS.md prose to `review.py`.** The grounding-rules ticket added
  `test_grounding_tier_ceilings_match_knowledge_readme` to pin the README tier table to `_TIER_CEILING`.
  There is no equivalent pin asserting the four verdict words / six check names in the new
  Adversarial-review block stay in sync with `review.py`'s `VERDICT_*` constants and check set. They
  could silently drift. **Consider** a sibling pin test (assert the verdict words and the check-name
  list appear in AGENTS.md) — minor finding, fix inline if cheap; otherwise note it. It's partly
  inherent (the block is mostly free prose), but the verdict words and check names are mechanical
  enough to pin.
- **"TESS-style stage" is an option, not an implementation.** The doc presents running this pass as an
  audited stage as a *possibility* (per the parent ticket). No such stage exists in code; verify the
  wording doesn't over-promise it as built.
- **The semantic pass is unenforced by definition.** Unlike `review.py`, nothing executes these
  checks — they rely on the agent following the rule. That's intended (no LLM-invocation code in repo),
  but it means the only "test" is doc accuracy + the human/agent honoring it. Flagged so the reviewer
  doesn't expect a behavioral guarantee.
- **Cross-doc skim was scoped to AGENTS.md + knowledge/README.md.** `docs/META_ACTION_WORKFLOW.md` and
  `README.md` already describe the `review.py` gate (updated by the `adversarial-review-gate` review);
  I did not re-edit them. Worth a glance that nothing there now contradicts the new semantic-layer
  framing.
