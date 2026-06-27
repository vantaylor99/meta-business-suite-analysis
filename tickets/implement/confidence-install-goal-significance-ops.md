description: When you turn an app-install ad on or off, the system decided how sure it was by counting purchases — which install accounts almost never have — so the decision stayed stuck at "low confidence" even when lots of installs backed it. Count installs for those accounts instead.
prereq: confidence-install-goal-significance
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/write_grounding.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What lands

Make the two `control.py` grounding producers select their **significance sample** by account goal,
mirroring what `confidence-install-goal-significance` already did for the action plan
(`actions._select_sample_conversions`). Today both sites compute a goal-aware *metric* via
`_status_metric` (cost-per-install for install goals) but ground the *sample* on the **purchase**
count, so for a `maximize_in_app_subscriptions` account `purchases ≈ 0` and the conversions floor is
never cleared — enable/pause and budget grounding are structurally pinned at `low`/`abstain` even when
real install volume backs the decision.

Two grounding sites (the ticket references "control.py ~699 and ~1350"):

1. **`_attach_status_grounding`** (`control.py:635`, sample at `:699`) — grounds `build_enable_ads_plan`
   (enable, call site `:789`) and `build_pause_plan`'s `roas_below` pause (call site `:1245`).
2. **`_attach_budget_grounding`** (`control.py:1327`, sample at `:1359`) — grounds the budget-move /
   "ops" plan; already receives `goal` and selects its metric via `_status_metric(row, goal)`.

### Data-shape note (differs from the action plan — read this)

The metrics rows here come from `fetch_entity_metrics` (`control.py:~960-998`), whose dicts carry
`purchases` and `app_installs` (computed from the **distinct** `PURCHASE_KEYS` vs `APP_INSTALL_KEYS`
sets, `:973-974`) but **no separate subscriptions/`results` count**. So the install-goal selector here
cannot replicate the action plan's subscriptions-first → installs-fallback ladder; it collapses to the
single install-family signal available: **`app_installs`**. This is the deliberate, defensible default
the plan ticket sanctioned ("app installs when there is no purchase signal"). Do **not** thread a new
subscription count into the row in this ticket — that is a larger data-plumbing change out of scope
here; if it is ever wanted it belongs in its own ticket.

## Design (resolved — implement exactly this)

### New per-module helper (follow the convention, do not share cross-module)

The codebase convention is a per-module goal selector (`actions._select_action_metric` and
`control._status_metric` are duplicated, not shared; `actions._select_sample_conversions` was likewise
kept local). Add a sibling to `_status_metric`:

```python
def _status_sample_conversions(
    metrics_row: dict[str, Any] | None, goal: str | None
) -> float | None:
    """Conversion count that grounds a set_status / budget op's significance band — goal-aware so the
    sample speaks the SAME conversion language as _status_metric and actions._select_sample_conversions.

    - install goal (``maximize_in_app_subscriptions``) → ``app_installs`` (the conversion behind the
      cited ``cost_per_app_install`` metric). The metrics_row built by ``fetch_entity_metrics`` carries
      no separate subscription count, so the action plan's subscriptions-first ladder collapses to the
      install count here.
    - ROAS / default / unknown goal → ``purchases`` (unchanged behaviour).
    """
    row = metrics_row or {}
    if goal == "maximize_in_app_subscriptions":
        return _num(row.get("app_installs"))
    return _num(row.get("purchases"))
```

Key on the literal `"maximize_in_app_subscriptions"` string ONLY — exactly as
`actions._select_sample_conversions` does. Do **not** also branch on the no-goal-but-installs-present
case (where `_status_metric` falls through to `cost_per_app_install` at `:614`); the action-plan helper
deliberately leaves that on purchases, and parity with it is the whole point. (Edge case named below.)

### `_attach_budget_grounding` — select internally (it owns one goal-aware metric path)

It already has `goal` and its metric is `_status_metric(row, goal)`. Replace the present-row sample
(`control.py:1359`):

```python
window=window, sample_purchases=_status_sample_conversions(row, goal),
```

Metric and sample are now both goal-aware → they agree.

### `_attach_status_grounding` — select at the CALL SITE, not inside (it is shared by two metric paths)

`_attach_status_grounding` grounds two callers with **different** metrics:
- enable (`:789`) — metric is goal-aware `_status_metric(metrics_row, goal)`.
- `roas_below` pause (`:1245`) — metric is **hardcoded** `blended_roas` (the ad was *selected* by ROAS).

If the helper selected by `goal` *inside* `_attach_status_grounding`, an install-goal account on the
ROAS-pause path would cite ROAS as the metric but `app_installs` as the sample — a sample/metric
mismatch. The sample must agree with the metric the call site chose. So **pass the resolved sample in
as a parameter**:

- Add a parameter to `_attach_status_grounding`, e.g. `sample_conversions: float | None`. In the
  present-row `else` branch (`:693-705`) use `sample_purchases=sample_conversions` instead of
  `_num(metrics_row.get("purchases"))`. Leave the two `metrics_row is None` branches (`:668-692`)
  **exactly as they are** — they cite `None` (structural) or `0.0` (cold) regardless of goal.
- Enable call site (`:789`): pass `sample_conversions=_status_sample_conversions(metrics_row, goal)` —
  agrees with the goal-aware metric.
- `roas_below` pause call site (`:1245`): pass `sample_conversions=_num((metrics_row or {}).get("purchases"))`
  — agrees with the hardcoded `blended_roas` metric; **byte-identical behaviour to today**.

> Reviewer/implementer alternative considered & rejected: passing `goal` into
> `_attach_status_grounding` and selecting inside. Rejected because the `roas_below` path would then
> have to force `goal=None` to stay ROAS-consistent, which obscures intent and is error-prone. The
> parameter approach keeps sample/metric agreement local to each call site.

### Serialized key + wording

- **Keep** the `Evidence` field / kwarg / JSON key named `sample_purchases`. The structural rename to
  `sample_conversions` is owned by `confidence-sample-conversions-rename` (do not start it here).
- The grounded confidence band is rendered through the **shared** `confidence.py` /
  `render_evidence_line`, which `confidence-install-goal-significance` already changed to say
  "conversions". So the operator-facing band wording on the set_status/budget path is *already* correct
  — there is no control.py-local operator string to change. (Verified: the only `control.py` "purchases"
  operator text is the ROAS markdown report at `:909`/`:932`, a different report path showing literal
  purchase counts — out of scope, same call the prereq left alone in `analyze.py`.)
- Optional but recommended for accuracy now that grounding is goal-aware: update the two code
  comments/docstrings that say "purchases" describing the *sample* — `control.py:656`
  ("zero purchases/spend sample" → "zero conversions/spend sample") and `:1343`
  ("'9 purchases over 5 days' guard" → "conversions"). These are comments, not operator-facing; change
  them only if it keeps the prose honest, and do not touch the literal `sample_purchases` symbol.

## Edge cases & interactions

The implementer must cover these; the reviewer will check them.

- **Install-goal enable, 0 purchases + real app installs (core fix).** `metrics_row` present with
  `purchases=0`/absent and `app_installs` ≥ floor → sample = app_installs → conversions floor cleared →
  band can read `medium`/`high` instead of being pinned at `low`/`abstain`.
- **Cold-enable boundary (`metrics_row is None`, `cold_cites_zero=True`).** Routes through the
  `elif metrics_row is None` branch (`:680-692`) → cites `sample_purchases=0.0`, `sample_spend=0.0`,
  **untouched by the helper**. Cited zero → `assess` abstains → apply-time gate **BLOCKS** (you cannot
  confidently enable an ad with no delivery evidence). Must hold for install-goal accounts too.
- **Structural pause (`metrics_row is None`, `cold_cites_zero=False`).** Routes through the first branch
  (`:668-679`) → cites NO sample (`sample_purchases=None`, `sample_spend=None`) → structural abstain →
  gate **ALLOWS** (PAUSED-by-default safety writes stay allowed). Untouched by the helper.
- **`roas_below` pause path (`:1245`).** Sample stays `purchases` (passed explicitly), agreeing with the
  hardcoded `blended_roas` metric — byte-identical to today for every goal. An install-goal account does
  not use `roas_below`, but if it did, the sample must NOT switch to installs (it cited ROAS).
- **ROAS / default account (enable + budget).** sample = `purchases`, byte-identical. Populate
  `app_installs` with a decoy value in the fixture and assert it is ignored.
- **Present row but `app_installs` is None for an install goal.** Sample becomes `None` while the row is
  present (not the None-row branch). Because `sample_spend` in the present-row branch is always numeric
  (`_num(...) or 0.0`), the sample is STILL "cited" per `write_grounding.py:87`/`:139` → `assess` runs,
  the conversions floor is not cleared → abstain WITH a cited sample → gate BLOCKS. This is identical in
  shape to today's `purchases is None` present-row case — **no whether-a-sample-is-cited change**, only
  which conversion count fills it. Confirm this invariant explicitly.
- **No-goal-but-installs-present account.** `_status_metric` (`:614`) may pick `cost_per_app_install`
  while `_status_sample_conversions` returns `purchases` (it keys only on the explicit install-goal
  string). This sample/metric asymmetry is INTENTIONAL parity with `actions._select_sample_conversions`
  (which also branches only on `maximize_in_app_subscriptions`). Do not "fix" it here.
- **`_num` coercion.** `_status_sample_conversions` returns `_num(...)` (`float | None`) — matches the
  existing `sample_purchases=_num(...)` types; no new None/str coercion path introduced.

## Tests (add to tests/test_meta_ads_analysis.py — mirror the prereq's action-plan tests)

- **Install-goal enable, installs back it:** `build_enable_ads_plan` for a `maximize_in_app_subscriptions`
  account, metrics row `purchases=0`, `app_installs` above the conversions floor → the op's computed
  confidence band is above `low` (not abstain). Pre-fix this read `low`/abstain.
- **Install-goal budget move, installs back it:** `_attach_budget_grounding` (via the budget-ops
  builder) for an install-goal entity, `purchases=0`, real `app_installs` → band above `low`.
- **ROAS account unchanged (both paths):** ROAS-goal enable + budget move with `app_installs=999` decoy
  → band identical to a purchases-only fixture; assert the decoy installs are ignored (sample =
  purchases).
- **Cold-enable install-goal → BLOCKED:** enable with `metrics_row=None`, `cold_cites_zero=True` →
  cited zero sample → abstain → apply-time gate blocks the write.
- **Structural-pause install-goal → ALLOWED:** pause with `metrics_row=None`, `cold_cites_zero=False`
  → no sample cited → structural abstain → gate allows.
- **`roas_below` pause unchanged:** the `roas_below` path still grounds on purchases (sample = purchases,
  metric = `blended_roas`) for any goal.
- Run the full suite: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py` (prereq baseline was
  283 passed). No ruff/mypy/pyright is configured (pyproject declares only pytest).

## TODO

- [ ] Add `control._status_sample_conversions(metrics_row, goal)` as a sibling of `_status_metric`.
- [ ] `_attach_budget_grounding`: replace the present-row sample (`:1359`) with
      `_status_sample_conversions(row, goal)`.
- [ ] `_attach_status_grounding`: add a `sample_conversions: float | None` parameter; use it in the
      present-row branch; leave both `metrics_row is None` branches unchanged.
- [ ] Enable call site (`:789`): pass `sample_conversions=_status_sample_conversions(metrics_row, goal)`.
- [ ] `roas_below` pause call site (`:1245`): pass
      `sample_conversions=_num((metrics_row or {}).get("purchases"))`.
- [ ] (Optional) Update the `:656` / `:1343` sample-describing comments from "purchases" to
      "conversions"; do NOT rename the `sample_purchases` symbol/key.
- [ ] Add the tests above; run the full suite and confirm no regressions.
