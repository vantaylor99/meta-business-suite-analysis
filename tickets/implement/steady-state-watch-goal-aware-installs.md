description: On accounts whose goal is app installs (not purchases), the everyday "is this ad wasting money?" scan keeps flagging healthy mature install ads as urgent because it grades them on purchase return — a number install ads barely produce by design. Grade those ads on cost-per-install instead, the way the new-ad check already does.
prereq:
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py
difficulty: hard
----

## Investigation outcome (resolved — this IS a code fix, not docs)

The plan ticket asked to first confirm whether install-goal accounts actually run the watch
scan. They do. `cli.watch_main` (`cli.py:1907`) calls `monitor.build_watch_report` for **any**
`--account` slug with no goal filter; there is no ROAS-only deployment convention. The install-goal
account exists in `config/meta_ads_accounts.json:17` (`primary_goal:
"maximize_in_app_subscriptions"`, `secondary_cost_per_app_install_target: 3.0`,
`pause_if_no_primary_and_secondary_cost_above: 3.0`). So a mature install ad on the steady-state path
is today graded ROAS-only and wrongly surfaces `urgent`/`underperforming` on a ~0 ROAS it books by
design. Code fix.

## Design (decided)

### Key decision: parallel install grader, NOT a goal-parameterized `classify_ad`

Keep `monitor.classify_ad` **ROAS-only and byte-for-byte unchanged** (it is a public pure function
with direct tests via the `_cls` helper, and the early-life ROAS forced-decision branch
`monitor.py:348` calls it directly). Add a parallel install grader in `monitor.py` that **reuses the
existing goal-aware machinery** — `early_triage.classify_own_sample` + `early_triage.goal_kind` (both
already imported into `monitor.py`, lines 50–54) — so **no third copy of the install bar is created**.
This mirrors the precedent already set on the early-life path (`_forced_decision_install`,
`monitor.py:494`), keeping ROAS and install as two sibling paths rather than one entangled engine.

Rejected alternatives, with reasons:
- *Goal-parameterize `classify_ad`*: would mutate the pure function's contract/signature and threaten
  the "ROAS unchanged byte-for-byte" requirement and its existing tests. Not worth it for a binary
  install bar.
- *Classify install over-target as `pause_candidate`* (early-life's class name): a **non-early-life**
  `pause_candidate` row renders in **none** of the CLI buckets — `cli.py:2005-2009` filters
  non-early rows into `urgent`/`underperforming`/`watch` only, and `pause_candidate` is gathered
  solely from `early_life` rows. It would also be excluded from `flaggable` (`monitor.py:826`), so
  watchlist accounting would silently break. Rejected.
- *Classify install over-target as `underperforming`*: flaggable and CLI-rendered, but understates —
  the install bar reached through `_goal_thresholds` IS the account's pause threshold (the
  `secondary_cost_per_app_install_target` → `pause_if_no_primary_and_secondary_cost_above` ladder),
  and the early-life sibling `_forced_decision_install` treats over-bar as a pause candidate with
  full-spend at-risk. Use `urgent` for consistency (see below).

### Install steady-state grade → existing taxonomy

`classify_own_sample` exposes a **single** install bar (its `_goal_thresholds` returns
`(target, target)` for install — there is no floor/target split, so there is no `underperforming`
middle tier for install). Map its `OWN_SAMPLE_*` verdict onto the steady-state taxonomy:

| `classify_own_sample` verdict | steady-state classification | behaviour |
|---|---|---|
| `OWN_SAMPLE_INSUFFICIENT` (spend < `min_spend` **or** no install target in policy) | `insufficient` | skipped (`continue`); not flagged, no crash, no watchlist entry — graceful degradation |
| `OWN_SAMPLE_KEEP` (cleared floor, cost ≤ target) | `ok` | skipped (`continue`) — **the core fix**: a healthy cheap-install ad is no longer flagged |
| `OWN_SAMPLE_PAUSE` (cost > target, or ~0 installs on the spend) | `urgent` | flagged; `flaggable=True`; on the watchlist; `dollars_at_risk = round(spend, 2)` (full window spend, mirroring `_forced_decision_install`); `confidence` via `assess(... direct_observation ...)` |

Grace protection is preserved and goal-agnostic: a recently created/changed ad
(`days_since_change < grace_days`) is `watch` (learning, never `urgent`), exactly as `classify_ad`
does — even when its cost is over target. This is distinct from early-life probation (age-from
`first_seen`); only mature, non-probation ads reach this grader.

### `urgent` vs `underperforming` — chosen `urgent`

Install exposes one effective bar through `_goal_thresholds`; over it is the account's own
"too expensive to keep" signal, and the early-life sibling already treats over-bar as a pause
candidate with full-spend at-risk. `urgent` is the steady-state taxonomy's strong-pause class, it is
`flaggable` (so watchlist `times_flagged` accrues with zero accounting changes), and it renders in the
existing CLI with **zero CLI changes**. Document the single-bar rationale in the grader docstring.

### Where the branch lives

In `build_watch_report`'s per-ad steady-state body (`monitor.py:817-851`), after the
`early_life_branch` `continue`:

```python
goal = goal_kind(policy)
if goal == "install":
    grade = _classify_ad_install(
        spend=spend, window_metrics=m, days_since_change=days_since_change,
        accelerating=accelerating, min_spend=min_spend, grace_days=grace_days,
        policy=policy, roas_floor=roas_floor, roas_target=roas_target, recency_days=0,
    )
else:
    grade = classify_ad(  # UNCHANGED
        spend=spend, roas=m.get("roas"), results=m.get("purchases"),
        days_since_change=days_since_change, accelerating=accelerating,
        min_spend=min_spend, grace_days=grace_days, roas_floor=roas_floor,
        roas_target=roas_target, recency_days=0,
    )
cls = grade["classification"]
if cls in ("insufficient", "ok"):
    continue
flaggable = cls in ("urgent", "underperforming")   # unchanged; install never emits underperforming
...
# evidence: goal-aware
if goal == "install":
    cpi = m.get("cost_per_app_install")
    evidence = Evidence(metric_name="cost_per_app_install", metric_value=cpi,
                        metric_display=(f"cost/install ${cpi:.2f}" if cpi is not None else "cost/install n/a"),
                        window=f"{win_from}..{to}", sample_conversions=m.get("app_installs"),
                        sample_spend=round(spend, 2), entity_level="ad", entity_id=ad_id,
                        entity_name=info.get("name"),
                        regenerating_query=build_regenerating_query(account_slug, "ad", win_from, to))
else:
    evidence = Evidence(metric_name="roas", ...)   # UNCHANGED
```

Row dict shape stays identical to today (keep `roas`/`purchases` top-level fields for renderer/consumer
compatibility — the install grader leaves `roas` ~0/None and the real metric lives in `evidence`,
exactly as the early-life install rows already do). The watchlist-accounting + ordering block
(`monitor.py:846-854`) stays shared and unchanged — install `urgent` rows flow through it like ROAS
`urgent` rows.

### `_classify_ad_install` (new, in `monitor.py`)

Returns the SAME dict shape as `classify_ad`: `{"classification", "dollars_at_risk", "reasons",
"confidence"}`. Structure mirrors `classify_ad`:

1. `own = classify_own_sample(spend=spend, purchase_value=m.get("purchase_value"),
   purchases=m.get("purchases"), app_installs=m.get("app_installs"), policy=policy,
   roas_floor=roas_floor, roas_target=roas_target, min_spend=min_spend)`.
2. `OWN_SAMPLE_INSUFFICIENT` → `{"classification": "insufficient", "dollars_at_risk": 0.0,
   "reasons": own.reasons, "confidence": confidence_to_dict(_abstain_confidence(own.reasons))}`.
   (Covers both below-floor and no-target — `classify_own_sample` already collapses both to
   `INSUFFICIENT` with `target=None` in the no-target case.)
3. Grace: `days_since_change is not None and days_since_change < grace_days` → `watch` with an
   abstain confidence (`_abstain_confidence`), reasons noting "learning, protected from kill" (+ the
   cost-over-target note when `own.verdict == OWN_SAMPLE_PAUSE`), `dollars_at_risk = round(spend, 2)`
   when over target else `0.0`.
4. `OWN_SAMPLE_KEEP` → `{"classification": "ok", "dollars_at_risk": 0.0, "reasons": own.reasons,
   "confidence": ...}` (skipped downstream; confidence value is unused — abstain is fine).
5. `OWN_SAMPLE_PAUSE` (mature) → build a minimal `Evidence(metric_name="cost_per_app_install",
   metric_value=own.metric_value, sample_conversions=own.results, sample_spend=spend, entity_*=None,
   window="n/a")` (entity-less, exactly like `classify_ad`'s internal assess evidence), then
   `conf = assess(evidence=..., tier=EvidenceTier.direct_observation, spend_floor=min_spend,
   conversions_floor=CONFIDENCE_CONVERSIONS_FLOOR, recency_days=recency_days,
   causal_text="; ".join(reasons))`. Reasons = `own.reasons` (+ "spend accelerating vs its recent
   average" when `accelerating`). `classification="urgent"`, `dollars_at_risk=round(spend, 2)`.

No new imports — `classify_own_sample`, `goal_kind`, `OWN_SAMPLE_*`, `assess`, `Evidence`,
`EvidenceTier`, `confidence_to_dict`, `build_regenerating_query`, `CONFIDENCE_CONVERSIONS_FLOOR`,
`_abstain_confidence` are all already present in `monitor.py`.

## Edge cases & interactions

- **Core regression fix**: mature install ad, cheap cost-per-install, ~0 ROAS → `ok`/skipped, NOT
  `urgent`. (Today: `urgent`.)
- **No install target in policy** (neither `secondary_cost_per_app_install_target` nor
  `pause_if_no_primary_and_secondary_cost_above`): `classify_own_sample` returns `INSUFFICIENT` →
  skipped → keep/abstain, never crashes, never guesses a threshold.
- **Grace window**: recently created/changed install ad over target → `watch`, never `urgent`
  (goal-agnostic protection, preserved).
- **Early-life vs steady-state**: probation/young install ads still route through
  `_early_life_branch` → `_forced_decision_install`; only `handled is False` (mature, not on
  probation) ads reach `_classify_ad_install`. No double-handling.
- **Watchlist accounting / ordering**: install `urgent` rows are `flaggable`, accrue `times_flagged`
  across scans, and sort under the existing `order` map — identical plumbing to ROAS `urgent` rows.
- **CLI rendering**: install `urgent` rows land in the existing non-early `urgent` bucket
  (`cli.py:2005`) and render via `line(r)` (which prints `ROAS 0.00` with the cost/install detail in
  the reasons — identical to how early-life install rows already render). No CLI change required.
- **ROAS-account regression**: ROAS goal takes the unchanged `classify_ad` branch and the unchanged
  ROAS `Evidence`; steady-state rows must be byte-for-byte identical.
- **Determinism**: `recency_days=0` threaded in (window ends at `as_of`); no clock in logic, matching
  the existing no-`datetime` test style.

## Tests (add to tests/test_meta_ads_analysis.py)

Reuse the existing `_WatchFakeClient`. Build install insights with `actions` carrying an
`app_install` action type (see `APP_INSTALL_KEYS` in `sync_api.py:90`:
`mobile_app_install`/`app_install`/`omni_app_install`); `fetch_entity_metrics` derives `app_installs`
and `cost_per_app_install = spend / app_installs`. Pass `policy={...}` and `early_life=False` directly
to `build_watch_report` so every ad takes the steady-state path (no DuckDB/history needed). An install
policy: `{"primary_goal": "maximize_in_app_subscriptions",
"secondary_cost_per_app_install_target": 3.0, "pause_if_no_primary_and_secondary_cost_above": 3.0}`.

- **Cheap CPI install ad, mature, NOT flagged**: spend $300, ~200 installs → cost/install ≈ $1.5 <
  $3.0, purchases ~0 (ROAS ~0). Assert the ad is absent from `report["rows"]` and from
  `watchlist["ads"]`. (Today this ad is wrongly `urgent`.)
- **Expensive CPI install ad, mature, flagged**: spend $300, 10 installs → cost/install $30 > $3.0.
  Assert classification `urgent`, `evidence.metric_name == "cost_per_app_install"`,
  `dollars_at_risk == 300.0`, and the ad is on the watchlist with `times_flagged == 1`.
- **No install target → graceful**: same expensive-looking install ad but `policy` omits both
  `secondary_cost_per_app_install_target` and `pause_if_no_primary_and_secondary_cost_above`. Assert
  no exception and the ad is absent from `rows` (degrades to keep/abstain).
- **Grace protection (install)**: recently-changed install ad over target → `watch`, never `urgent`
  (set `updated_time` within `grace_days` of `as_of`).
- **ROAS regression**: keep/confirm an existing ROAS steady-state test still passes and asserts
  `evidence.metric_name == "roas"` on a flagged ROAS row (e.g. extend
  `test_build_watch_report_rows_carry_confidence_and_reproducible_evidence`).
- **Pure-unit grader** (optional but recommended): a `_classify_ad_install` direct test mirroring the
  `_cls` helper — assert cheap→`ok`, expensive→`urgent`, recently-changed→`watch`, no-target→
  `insufficient`.

## TODO

- [ ] Add `_classify_ad_install(...)` to `monitor.py` per the design above (reusing
      `classify_own_sample`/`goal_kind`; no new imports; ROAS-class taxonomy mapping).
- [ ] In `build_watch_report`'s steady-state per-ad body, branch on `goal_kind(policy)` for both the
      grade and the row `Evidence` (install → `cost_per_app_install`); leave the ROAS branch and the
      shared watchlist-accounting/ordering block byte-for-byte unchanged.
- [ ] Add the tests above to `tests/test_meta_ads_analysis.py`.
- [ ] Run `python -m pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/watch_test.log` (stream,
      don't silently redirect) and any type check the repo uses; fix regressions before handing off.
- [ ] Write the `review/` handoff noting any gaps (e.g. CLI still prints `ROAS 0.00` on install rows —
      acceptable, parity with early-life install rendering, flagged for the reviewer to confirm).
