description: Fix a bug where the automatic "day-3 keep-or-kill" check on an install-focused ad account can wrongly flag a healthy new ad for pausing because it grades the ad on purchase return-on-spend (which install ads don't generate) instead of on cost-per-install.
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/confidence.py
difficulty: medium
----

## Confirmed bug (reproduced)

`monitor._early_life_forced_decision` grades a probated ad's own life-to-date sample with
`classify_ad(...)`, which is **ROAS-only** — it has no notion of the account goal. On an install-goal
account (`primary_goal == "maximize_in_app_subscriptions"`) an ad books few/zero `purchase` actions by
design, so `m["roas"]` is ~0. Once such an ad reaches the decision age (`age >= EARLY_LIFE_DECISION_AGE`,
default 2 → "day 3") **and** has spent ≥ `min_spend` in the watch window, `classify_ad` returns
`urgent` (ROAS below pause floor), which the forced decision maps to `pause_candidate` and closes the
probation — even when cost-per-install is excellent.

Reproduced against current `HEAD` (a standalone harness mirroring the test doubles in
`tests/test_meta_ads_analysis.py`): an age-3 install ad on probation, `spend=$300`, **0 purchases but
200 installs → cost/install $1.50** (well under the `$3.00` `secondary_cost_per_app_install_target`)
returns:

```
classification: pause_candidate
verdict: pause_candidate
reasons: ['day-3 decision (age 3): own sample cleared the significance floor and is below the pause
          floor — pause candidate', 'ROAS 0.00 < pause floor 1.5 on $300', '~0 results', ...]
```

It should be a **keep** (`classification="watch"`, `verdict="keep"`).

## Root cause

The "own sample clears the significance floor" shortcut in
`monitor._early_life_forced_decision` (src/meta_ads_analysis/monitor.py:316-368) unconditionally calls
`classify_ad(spend=..., roas=m.get("roas"), results=m.get("purchases"), ...)`. That branch is reached
for *all* account goals, but `classify_ad` only understands ROAS. The grace window is also deliberately
overridden here (`days_since_change=None`), so the install ad doesn't even get young-ad protection — a
healthy install ad that merely spent fast in its first three days is force-flagged.

The data and the goal logic to do this correctly already exist:
- `control.fetch_entity_metrics` already returns `app_installs` and `cost_per_app_install` in the same
  window row `m` (src/meta_ads_analysis/control.py:983, 993).
- `early_triage` is already goal-aware: `_goal_kind` (line 156), `_goal_thresholds` (line 160),
  `_GoalProfile`/`_is_struggling`/`_cleared_target` (lines 141, 207, 223). Only the forced-decision
  own-sample shortcut bypasses it.

## Fix design (recommended: local to the forced-decision shortcut)

Keep the **ROAS** path exactly as it is today (`classify_ad`) — it is correct for ROAS goals and is
covered by existing tests (`test_watch_day3_probation_own_sample_clears_floor_keep_and_close`,
`...below_floor_pauses_and_closes`). Only add a **goal-aware install branch** so the install own-sample
decision uses cost-per-install instead of ROAS.

Centralize the goal logic in `early_triage` (the module that already owns goal-awareness and whose
docstring promises "no threshold numbers are duplicated"). Add one public own-sample classifier there
and have the monitor consume it for the install branch.

### New public API in `early_triage.py`

```python
# Own-sample (life-to-date observed window) verdicts — distinct from the analog VERDICT_* values above.
# Used by the monitor's day-3 forced decision so the keep/kill is graded on the ACCOUNT GOAL metric,
# not ROAS. classify_own_sample is the goal-aware analog of monitor.classify_ad.
OWN_SAMPLE_INSUFFICIENT = "insufficient"   # below the significance floor — caller defers to analogs
OWN_SAMPLE_KEEP = "keep"                   # cleared the floor and is at/above the goal bar
OWN_SAMPLE_PAUSE = "pause_candidate"       # cleared the floor and is below/over the goal bar

@dataclass(slots=True)
class OwnSampleVerdict:
    verdict: str            # OWN_SAMPLE_*
    kind: str               # "roas" | "install"
    metric_name: str        # "blended_roas" | "cost_per_app_install"
    metric_value: float | None
    target: float | None    # the goal floor/target used (None when the install target is unknown)
    results: float          # goal-aware result count (purchases for ROAS, installs for install)
    reasons: list[str]

def classify_own_sample(
    *,
    spend: float,
    purchase_value: float | None,
    purchases: float | None,
    app_installs: float | None,
    policy: dict[str, Any],
    roas_floor: float,
    roas_target: float,
    min_spend: float,
) -> OwnSampleVerdict:
    """Goal-aware grade of an ad's OWN observed window (NOT analogs). Below ``min_spend`` →
    OWN_SAMPLE_INSUFFICIENT (caller falls through to the analog verdict). Above it, build a `_Sums`
    from the window and reuse `_goal_kind`/`_goal_thresholds`/`_GoalProfile`/`_is_struggling` so the
    bar is identical to the analog engine: ROAS below pause floor / install cost over target (or zero
    results) → OWN_SAMPLE_PAUSE; otherwise OWN_SAMPLE_KEEP. When kind=='install' and the policy has no
    target install cost, returns OWN_SAMPLE_INSUFFICIENT so the caller defers to the analog path
    (which degrades to keep)."""
```

Implementation notes:
- Build `_Sums(spend=spend, results=_select_results-equivalent, purchase_value=purchase_value or 0.0)`
  where the goal-aware result count is `app_installs` for install and `purchases` for ROAS.
- Reuse `_goal_thresholds(kind, policy, roas_floor, roas_target)`; `None` → `OWN_SAMPLE_INSUFFICIENT`.
- Reuse `_is_struggling(sums, profile, non_trivial_spend=...)` for the pause/keep call so the install
  bar is "cost-per-install > target, or zero installs on spend". Note `_is_struggling` declines to
  judge below its own `non_trivial_spend` floor — but `classify_own_sample` is only consulted *after*
  the `spend >= min_spend` gate (min_spend ≥ EARLY_LIFE_MIN_SPEND in practice), so pass
  `non_trivial_spend=min_spend` (or `0.0`) so the gate is the single significance threshold and there
  is no second, conflicting floor.
- `metric_value` is `_metric_value(sums, kind)` (cost/install, or ROAS); `metric_name` is
  `_metric_name(kind)`.

### Monitor change (`_early_life_forced_decision`)

Branch on goal kind at the top of the own-sample step:

```python
kind = _goal_kind(policy)  # import _goal_kind (or a public goal_kind wrapper) from early_triage
```

- **ROAS goal** → unchanged: the existing `classify_ad` call + the `dcls in {urgent, else}` row build
  stay exactly as today.
- **Install goal** → call `classify_own_sample(spend=spend, purchase_value=m.get("purchase_value"),
  purchases=m.get("purchases"), app_installs=m.get("app_installs"), policy=policy, roas_floor=...,
  roas_target=..., min_spend=...)`:
  - `OWN_SAMPLE_INSUFFICIENT` → fall through to the analog path (the existing tail of
    `_early_life_forced_decision`, `triage_ad(...)`). **Refactor that tail into a small
    `_forced_decision_analog(...)` helper** so both the ROAS-insufficient case and the
    install-insufficient case reach it without duplicating the analog code.
  - `OWN_SAMPLE_PAUSE` → build a `pause_candidate` row (`classification=verdict=EARLY_PAUSE_CANDIDATE`).
  - `OWN_SAMPLE_KEEP` → build a `watch`/`keep` row.
  - Build install-flavored `Evidence(metric_name="cost_per_app_install", metric_value=cpi,
    metric_display=..., sample_purchases=installs, sample_spend=spend, window=f"{win_from}..{to}",
    entity_id=ad_id, ...)` and a **direct-observation** confidence via `confidence.assess(evidence=...,
    tier=EvidenceTier.direct_observation, spend_floor=min_spend,
    conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR, recency_days=0, causal_text=...)`. `assess` is
    metric-agnostic (it scores `sample_purchases`/`sample_spend`), so passing installs as the
    conversion count yields the same `direct_observation` banding the ROAS branch produces.
  - `dollars_at_risk`: `_dollars_at_risk` is ROAS-based and does not apply to install. Use `0.0` for a
    keep; for a pause, `round(spend, 2)` (the whole window spend is the waste estimate) is reasonable —
    the field is informational and has no test assertion either way.
  - Always return the `close_action` (probation is owed a decision regardless of the verdict), same as
    the ROAS branch.

This keeps `build_watch_report` write-free and keeps the early-life triage the single source of truth
for the early-life ad.

## Scope / out of scope

- **In scope:** the install-goal forced-decision own-sample shortcut only.
- **Out of scope (pre-existing, do not fix here):** the *normal* (non-early-life) watch path in
  `build_watch_report` also calls `classify_ad` with ROAS for every ad
  (src/meta_ads_analysis/monitor.py:646), so it is ROAS-centric for install accounts too. That is a
  broader limitation that predates this bug and warrants its own ticket; making the entire watch path
  goal-aware is a larger change. Note it in the review handoff so a follow-up backlog ticket can be
  filed; do not expand this ticket to cover it.

## Test plan

Add an install-goal forced-decision test mirroring
`test_watch_day3_probation_own_sample_clears_floor_keep_and_close`
(tests/test_meta_ads_analysis.py:8012). The existing `_watch_insight` helper only emits `purchase`
actions — extend it with an `installs` param that emits an
`actions: [{"action_type": "mobile_app_install", "value": str(installs)}]` blob (action type must be
one of `APP_INSTALL_KEYS` in src/meta_ads_analysis/sync_api.py:90), or build the install insight inline
in the test. Fixtures `_install_ad` / `_INSTALL_POLICY` already exist
(tests/test_meta_ads_analysis.py:7476, 7512).

Cases to cover:
- **Cheap installs → keep + close** (the reported bug): age-3 install ad on probation, `spend ≥
  min_spend`, 0 purchases, installs cheap enough that `cost/install ≤ target` → `classification="watch"`,
  `verdict="keep"`, `confidence["grounding_tier"] == "direct_observation"`, one `close` action. (Today
  it returns `pause_candidate` — assert it no longer does.)
- **Expensive installs → pause + close**: same setup but `cost/install > target` (or zero installs on
  ≥ min_spend) → `classification="pause_candidate"`, `direct_observation`, one `close` action.
- **Below-floor install ad still routes to analogs**: confirm the existing analog-governs behavior is
  unchanged for install (spend < min_spend → `OWN_SAMPLE_INSUFFICIENT` → analog path). The existing
  `test_watch_day3_probation_still_below_floor_analog_governs` is ROAS-only; add an install analog of
  it (cheap-install recovering analogs → keep_watch; non-recovering → pause_candidate).
- **Regression guard**: the existing ROAS forced-decision tests
  (`test_watch_day3_probation_own_sample_clears_floor_keep_and_close`,
  `...below_floor_pauses_and_closes`, `...still_below_floor_analog_governs`) must still pass unchanged.

A confirmed repro harness lives at the scratchpad path used during the fix investigation; the test
above is the canonical version to land.

## TODO

### Phase 1 — early_triage goal-aware own-sample classifier
- [ ] Add `OWN_SAMPLE_*` constants, `OwnSampleVerdict`, and `classify_own_sample(...)` to
      `src/meta_ads_analysis/early_triage.py`, reusing `_goal_kind` / `_goal_thresholds` /
      `_GoalProfile` / `_is_struggling` / `_metric_value` / `_metric_name`. No threshold numbers
      duplicated. Handle the missing-install-target case → `OWN_SAMPLE_INSUFFICIENT`.
- [ ] (Optional) expose a public `goal_kind(policy)` wrapper, or let monitor import `_goal_kind` — pick
      whichever matches the existing import style in monitor.py.

### Phase 2 — monitor forced-decision install branch
- [ ] Refactor the analog-governs tail of `_early_life_forced_decision` into a
      `_forced_decision_analog(...)` helper (returns `(row, close_action)`).
- [ ] Branch on `_goal_kind(policy)` in `_early_life_forced_decision`: ROAS → existing `classify_ad`
      path (unchanged); install → `classify_own_sample(...)`, mapping
      KEEP/PAUSE/INSUFFICIENT to keep-row / pause-row / `_forced_decision_analog(...)`.
- [ ] Build install-flavored `Evidence` (cost_per_app_install) + `direct_observation` confidence via
      `confidence.assess`. Set `dollars_at_risk` (0.0 keep / spend pause).

### Phase 3 — tests + validation
- [ ] Extend `_watch_insight` with an `installs` param (or build install insights inline).
- [ ] Add the four test cases above.
- [ ] Run the targeted suite and the type check, e.g.:
      `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -k "watch and (install or day3 or probation)" 2>&1 | tee /tmp/pytest.log`
      then the full `tests/test_meta_ads_analysis.py` module. Confirm no regression in the ROAS
      forced-decision tests. (Use `.venv/bin/python`; there is no bare `python` on PATH.)
