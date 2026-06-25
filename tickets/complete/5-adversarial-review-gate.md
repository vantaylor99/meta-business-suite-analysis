description: Before any pause/scale recommendation reaches the operator, an automatic second-opinion check now judges each call from only the facts cited for it — big enough sample, long enough window, correlation-vs-cause, is the stated confidence actually earned, and does the call agree with its own numbers — and downgrades or drops the ones that fail (showing them, not hiding them) so plausible-but-wrong advice can't slip through.
prereq:
files: src/meta_ads_analysis/review.py, src/meta_ads_analysis/briefs.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/confidence.py, tests/test_meta_ads_analysis.py, README.md, docs/META_ACTION_WORKFLOW.md
----
## What shipped

A pure, I/O-free module `src/meta_ads_analysis/review.py` that adversarially reviews each structured
recommendation from its cited `evidence` + claimed `confidence` band only (not trusting the producing
rationale), re-deriving the band via `confidence.assess` and returning the most-conservative verdict
across six refutation checks (`sample_floor` → insufficient, `window_length` → downgrade, `causal`,
`band_earned`, `direction` → refuted, `external`). `review_action_plan` returns a new plan (never
mutates the input) where each recommendation-bearing action gains a `review` block and has its
`confidence`/`status`/`executable` demoted per verdict — demote-only, idempotent via a skip-guard.
Wired into `briefs.build_operator_brief` (gated by `review_enabled`, default on; `cli`'s `--no-review`
escape hatch) with a new "Refuted / Downgraded By Review" section and `↓ Review:` in-section lines.
`config.REVIEW_MIN_WINDOW_DAYS = 7` is the only new threshold. Full design detail is in the module
docstring and the implement commit (`fe4a32b`).

## Review findings

Reviewed the implement diff (`fe4a32b`) with fresh eyes against the producer (`confidence.py`,
`actions.py`), the brief assembly/render path, the write gate (`actions.apply_action_plan`), and the
docs. **Verdict: implementation is sound and the core invariants hold. No major defects found in
shipped behavior.** Minor items fixed inline; one deliberate coverage gap filed as a follow-up.

**Correctness / logic — checked, no defects.**
- Verified the gate is **demote-only** end to end: every check compares `original_band > X`, `_resolve`
  floors a downgrade at one band below the claim (and flips to `insufficient` when it lands on
  `abstain`), and `_apply_verdict` never raises a band, promotes a status, or sets `executable=True`.
  Confirmed it sits upstream of the guarded write gate (`apply_action_plan` keys on `executable` +
  `status == approved`, both of which the gate only ever lowers).
- Verified **no false positives** in the two recompute checks: `band_earned` deliberately omits
  `causal_text`/`pvalue`, so its recompute is ≥ the producer's actual band — it can only catch an
  *upward* drift, never punish a correctly-causal-downgraded call. `causal` and `band_earned` both
  firing on one call accumulate and the most-conservative target wins (now locked by a new test).
- Verified **recency parity**: review's `_recency_days_from_window(run_date, window_end)` is the same
  `run - last_seen` formula the producer feeds `assess` (`actions._recency_days`), so the recompute is
  faithful; missing dates round down identically on both sides.
- Verified the **A/B-is-causal-evidence exemption** (`tier != ab_experiment` guard in check 3) — a
  causal claim grounded in an experiment is not downgraded (new test locks it).
- Confirmed the **per-action spend-floor map** (`review._ACTION_SPEND_FLOOR`) matches
  `actions._ACTION_SPEND_FLOOR` exactly today (both: pause/refresh→`MIN_WASTE_SPEND`,
  budget→`MIN_SCALING_SPEND`); the hand-mirror is necessary because importing `actions` would break
  the module's purity. The duplication risk is real but low and called out in the docstring.

**Edge cases / robustness — checked, no defects.** Empty/missing evidence, missing/unparseable window
(`"n/a"`, single-date), unrecognized tier, and a missing claimed band all degrade safely (skip the
relevant check or no-op to `stands`) without crashing or fabricating a verdict. Added a test for the
no-claimed-band defensive no-op.

**Tests.** Implementer's 13 tests are a solid floor (happy path, each check, plan-level shape,
demote-only, idempotency, brief surfacing, escape hatch). Added 4 to cover untested invariants:
A/B causal exemption (no downgrade), scale-below-target refutation (mirror of the pause-a-winner
case), no-claimed-band no-op, and multi-downgrade accumulation (most-conservative wins, all inputs
named). **141 passed** (was 137).

**Docs (were stale, fixed inline).** Neither `README.md`'s "Build an operator brief" section nor
`docs/META_ACTION_WORKFLOW.md`'s "Operator Brief" section mentioned the review gate, the new section,
or `--no-review`. Updated both to describe the gate, its demote-only guarantee, the surfaced-not-
deleted behavior, and the escape hatch (and noted the semantic-refutation split to the companion
`adversarial-review-protocol`). Also fixed a misleading comment on `review._deepcopy_plan` (said
"shallow-copy the plan but deep-copy each action"; it does a full `deepcopy`).

**Lint/build.** No linter is configured in the repo (no ruff/flake/mypy config in `pyproject.toml`
or the venv); verified clean import of `review`/`briefs` and ran a manual render smoke test of an
insufficient action — the slight Confidence-line vs Review-line redundancy the implementer flagged
reads acceptably and no `None`/false-precision leaks. CLI `--no-review` help confirmed.

**Major → filed, not fixed here.**
- `tickets/backlog/review-gate-install-goal-direction.md` — the `direction` check fires only for
  ROAS-goal accounts; install-goal accounts (cost-per-install, opposite polarity) get no direction
  refutation. This was a deliberate conservative skip at ship time; the policy already carries the
  targets needed to implement it. Filed as backlog (enhancement, not a shipped bug), cross-linked to
  the existing `confidence-install-goal-significance` ticket.

**Other known-gap notes from the handoff — dispositioned, no action needed.**
- `recency_stale_days` param being inert: confirmed acceptable — `band_earned` defers to `assess`,
  which applies the same `CONFIDENCE_RECENCY_STALE_DAYS` constant, so behavior is consistent.
- `approval_required` not cleared on insufficient/refuted: confirmed **moot** — it is set but never
  read anywhere in `src/` (the write gate keys on `executable` + `status`), and the brief's
  `_brief_action` does not even carry it. No fix warranted.
- The gate informs the brief only and does not rewrite the persisted `action_plan.json`: confirmed
  **intentional** per the ticket scope (operator-facing filtering; the human still approves in the
  plan before `apply`). It cannot weaken the write gate.
- Companion semantic-review work (`adversarial-review-protocol`) is tracked in
  `tickets/implement/6-adversarial-review-protocol.md`. Confirmed present.
