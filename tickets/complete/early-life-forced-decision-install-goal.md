description: The day-3 keep-or-kill check for new ads on an install-focused account now grades the ad on cost-per-install instead of purchase return-on-spend, so a healthy install ad is no longer wrongly flagged for pausing.
files: src/meta_ads_analysis/early_triage.py, src/meta_ads_analysis/monitor.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Summary of the shipped change

`monitor._early_life_forced_decision` now branches on the account goal. ROAS accounts keep the
unchanged ROAS `classify_ad` path; install accounts (`primary_goal == "maximize_in_app_subscriptions"`)
grade the probated ad's own life-to-date window on **cost-per-install** via the new
`early_triage.classify_own_sample` + `monitor._forced_decision_install`. A healthy install ad (few/zero
purchases by design, so ~0 ROAS) is no longer force-paused at day 3 when its cost-per-install is at or
under the account target. Below the significance floor both goals share `_forced_decision_analog`.

Implementation detail lives in the prior implement-stage handoff / commit `73eaad9`; this ticket is the
review record.

## Review findings

### What was checked

- **Read the implement diff first** (`git show 73eaad9`), then traced the live code in
  `early_triage.py` (`classify_own_sample`, the reused `_goal_thresholds`/`_is_struggling`/
  `_metric_value`/`_GoalProfile`/`_Sums` helpers) and `monitor.py`
  (`_early_life_forced_decision`, `_forced_decision_install`, `_forced_decision_analog`).
- **Data plumbing**: confirmed `fetch_entity_metrics` (`control.py:955`) actually populates
  `app_installs` and `purchase_value` in `window_metrics`, so the new `m.get("app_installs")` /
  `m.get("purchase_value")` reads are real, not silently `None`.
- **Threshold reuse / DRY**: confirmed the install bar in `classify_own_sample` comes from the same
  `_goal_thresholds`/`_is_struggling` the analog engine uses — no duplicated threshold numbers; the
  `spend >= min_spend` gate is pinned as the single significance floor (`non_trivial_spend=min_spend`).
- **Confidence**: read `confidence.assess`/`data_strength` and confirmed installs-as-conversion-count
  bands the same way the ROAS branch bands its own sample (the reviewer-flagged "high on raw install
  count" is consistent, intentional, and a documented judgment call — not a bug).
- **ROAS regression**: confirmed the ROAS path is unchanged — the install branch returns early at the
  top of `_early_life_forced_decision`, leaving the `classify_ad` block byte-for-byte intact; the three
  existing ROAS forced-decision tests still pass.
- **Lint/type/tests**: no ruff/mypy/pyright in this repo (pytest is the only gate, per the handoff);
  `py_compile` of both modules is clean; full suite green.

### Bugs found

None. The keep/pause boundary, the zero-install (undefined-metric) pause, the below-floor deferral, and
the no-target deferral all behave correctly. The `OWN_SAMPLE_PAUSE` string value collides with
`VERDICT_PAUSE_CANDIDATE` ("pause_candidate") but the two are compared only within their own namespaces,
so there is no defect — noted as a mild smell, not changed.

### Minor — fixed inline in this pass

- **Test gap on the new public function.** `classify_own_sample` is public and goal-generic but was
  only exercised indirectly through `build_watch_report`; two of its branches (the no-target-but-
  above-floor deferral, and the ROAS kind) had **no** coverage at all. Added six direct unit tests
  (`test_classify_own_sample_*`) covering: install keep (cheap), install pause (expensive), install
  zero-install pause with undefined metric, below-min-spend insufficient, install-no-target
  insufficient above the floor, and ROAS-kind keep/pause. While writing them I confirmed (not a code
  bug, but worth recording) that a ROAS "keep" requires a non-zero `purchases` count — zero purchases
  is struggling regardless of `purchase_value`, since the result-count is the gate. All 352 tests pass
  (`.venv/bin/python -m pytest tests/test_meta_ads_analysis.py`).

### Major — filed as new ticket(s)

- **`tickets/backlog/steady-state-watch-path-goal-aware-installs.md`** — the steady-state (non-early-
  life) watch path in `build_watch_report` (`monitor.py:817`) still grades **every** ad with ROAS-only
  `classify_ad`, so a mature install ad past `early_life_max_age` and not on probation is still
  wrongly flagged. Same class of bug as this ticket, larger blast radius (touches the
  classification taxonomy + watchlist accounting), deliberately out of scope here. The implementer
  already flagged this; confirmed at the source and filed.

### Things deliberately left as-is (with reason)

- **`dollars_at_risk` on install rows** = whole-window spend on pause, `0.0` on keep. `_dollars_at_risk`
  is ROAS-based and doesn't apply to installs; the field is informational with no downstream consumer
  that special-cases install rows. Acceptable.
- **`accelerating` is not consulted on the install branch.** The ROAS `classify_ad` path passes
  `accelerating`, but the install grade (like the analog engine `triage_ad`) is a cumulative
  life-to-date call where short-window acceleration matters little at the day-3 decision. Noted as a
  minor behavioral divergence, not worth a code change or a ticket.

### Validation run

```
.venv/bin/python -m pytest tests/test_meta_ads_analysis.py    # 352 passed
.venv/bin/python -m py_compile src/meta_ads_analysis/{monitor,early_triage}.py   # clean
```
