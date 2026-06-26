description: A proposal to turn an ad back on now warns the operator when the ad's own numbers say it loses money against the account goal, so a known-loser re-enable no longer looks as trustworthy as a genuine performer.
prereq:
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/review.py, tests/test_meta_ads_analysis.py, docs/META_ACTION_WORKFLOW.md
difficulty: medium
----
## What shipped

Enable ops are now direction-judged the same way budget scale-ups already were. Enabling an ad is
directionally a scale-up (0 → live), so a re-enable whose own cited ROAS sits below the account's
`target_roas` is the same self-contradiction as scaling up a below-target budget — and is now
**refuted** by the review gate instead of reaching the operator as a `medium`/`high`-trust proposal.

- `control.build_enable_ads_plan` tags each enable op `action_type="enable_ad"` (mirrors `_budget_op`).
- `review.py` adds `_ENABLE_ACTIONS = {"enable_ad"}` and an enable-specific branch in
  `_direction_contradiction` (strict `<` vs the bare target — the scale-up convention).
- `refuted` flows through the existing `_apply_op_verdict` path: appends the contradiction reason to
  `confidence["factors"]`, sets `op["review_verdict"]="refuted"`, demotes approved→proposed (no-op for a
  freshly-built enable). It does **not** delete the op or add an apply-time block — `apply_ops_plan`'s
  gate keys on grounding + status, not `review_verdict`. So a refuted-but-grounded enable an operator
  deliberately approves still executes. This is locked plan decision #1.
- `docs/META_ACTION_WORKFLOW.md` enable-grounding section rewritten to describe the refutation.

## Review findings

Approach: read the implement diff (ffa607e) first, then traced the actual code — `_direction_contradiction`,
`review_recommendation`/`_resolve` verdict precedence, `_apply_op_verdict`, and the full `apply_ops_plan`
gate — and grepped every `action_type` consumer. Verdict: **implementation is correct and faithful to the
locked plan.** Two minor issues found and fixed inline; no major findings, no new tickets.

**Correctness / direction logic — checked, sound.** The enable branch sits right after `_SCALE_ACTIONS`,
guarded by the same ROAS-goal + numeric-target + `blended_roas` + present-`metric_value` preconditions.
Strict `<` against the bare target is the intended scale-up polarity (asymmetric with the pause-winner
1.5× margin, by design — enabling is a scale-up, not a scale-down). The handoff flagged this as
"confirm intended" — **confirmed intended.**

**Verdict precedence — checked, sound.** `_resolve` takes `max` over `_VERDICT_RANK`
(insufficient=3 > refuted=2 > downgrade=1 > stands=0). So below-target-AND-below-floor → insufficient
(most-conservative wins), and `refuted` leaves the band untouched (a warning, not a band-cap). Both
pinned by tests.

**`action_type` leakage — checked, none.** `validate_op` keys on `op` (op-type), `_build_request` keys
on `op_type`, the `apply_ops_plan` gate keys on `status` + `op_grounding_gap`. `briefs` counts
`action_type` only over `plan["actions"]`, never `plan["ops"]`. Nothing branches on an *op's*
`action_type`, so adding `enable_ad` is inert outside the direction check. Handoff's grep verified
independently.

**Warning-not-block semantics — checked, intended.** Re-derived the apply gate myself: it never reads
`review_verdict`. A grounded refuted enable set to `approved` returns `dry_run`/executes; a cold
(abstain) one is blocked by the grounding gate regardless of verdict. Matches locked decision #1.

**ROAS-only / install-goal deferral — checked, acceptable.** The `primary_goal != "roas"` guard returns
early for install goals before any metric comparison, and install enables already cap at `low` (the
conversion sample is purchases, not installs), so a below-target install enable can't present as
high/medium-trust. Deferral to backlog (`enable-wrong-direction-install-goal`) is a real scope cut, not
a hole — no ticket filed (already tracked by the plan).

**Tests — happy/edge/error/precedence all covered.** The 7 implementer tests plus the boundary test
added below cover: below-target strong-sample → refuted; above-target → stands; exactly-at-target →
stands; no-target guard; install-goal not-refuted; cold-ad insufficient-not-refuted; below-floor
insufficient-wins; operator-override survives. Idempotency is covered structurally (the
already-reviewed skip in `_review_plan_ops` prevents a second direction-reason append) and by the
pre-existing `test_enable_ads_review_is_idempotent`.

### Minor issues fixed inline

1. **Stale docstring** (`review.py` `_review_plan_ops`, ~line 689) — still asserted "neither [control nor
   authoring ops] carries an `action_type`", directly contradicted by this feature (and already
   inaccurate for budget ops). Rewritten to state that budget + enable ops set one so the direction
   check fires. The implementer had updated the sibling `review_ops_plan` docstring and the inline
   comment but missed this one.
2. **Missing boundary test** — the plan and handoff both flagged the intentional strict-`<` (an
   exactly-at-target enable is *not* refuted), but no test landed on `roas == target`. Added
   `test_enable_ads_exactly_at_target_roas_stands` (ROAS 2.0 == target 2.0 → stands, `direction` not in
   `failed_inputs`) so a future `<=` slip can't silently start refuting break-even re-enables.

### Docs

`docs/META_ACTION_WORKFLOW.md` enable-grounding section read in full against the new code — accurately
describes the refutation, band-vs-direction complementarity, warning-not-block semantics, and ROAS-only
scope. No further doc drift found.

## Validation

```
.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q
```

→ **291 passed** (was 290 at implement handoff; +1 boundary test added in review). No ruff/mypy/pyright
is configured (pyproject declares only pytest). No `.pre-existing-error.md` — suite fully green.
