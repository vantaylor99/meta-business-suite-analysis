description: For app-install accounts, several recommendation paths still measure confidence by counting purchases (which those accounts rarely have) even though they already judge the account on installs — so they get stuck reading "low data" the same way the action plan used to. Extend the install-aware fix to those remaining paths.
prereq: confidence-install-goal-significance
files: src/meta_ads_analysis/control.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Why (the bug, generalized)

`confidence-install-goal-significance` made the **action plan** (`actions.evaluate_action_confidence`)
ground its significance sample on the conversion type that fits the account goal — in-app
subscriptions / app installs for `maximize_in_app_subscriptions`, purchases otherwise — via the new
helper `actions._select_sample_conversions`. It deliberately scoped the change to `actions.py` +
`confidence.py`.

But the action plan is only **one** of several grounded producers that feed the shared
`confidence.assess` engine, and the others still ground `sample_purchases` on the **purchase** metric
unconditionally, with **no goal awareness**:

- `control.py:699` (`build_enable_ads_plan` / cold-cite path) and `control.py:1350` (ops plan) —
  `sample_purchases=_num(metrics_row.get("purchases"))`.
- `authoring.py:358` — `sample_purchases=_num(row.get("purchases"))`.
- `rotation.py:203` — `sample_purchases=_num(row.get("purchases"))`.
- `monitor.py:210` (and `:95`) — `sample_purchases=m.get("purchases")` / `results`.

The sharp inconsistency: `control._select_set_status_metric` (control.py:602-615) **already** mirrors
`actions._select_action_metric` and picks `cost_per_app_install` as the *metric* for install-goal
accounts — but the *sample* that grounds its confidence is still the purchase count. `purchases` and
`app_installs` are computed from **distinct** key sets (`PURCHASE_KEYS` vs `APP_INSTALL_KEYS`,
control.py:973-974), so for an install account `purchases ≈ 0` while `app_installs` is populated.

### Impact

For a `maximize_in_app_subscriptions` account, the enable/pause `set_status` write path, the authoring
go-live grounding, and the rotation/fatigue grounding all compute their confidence band off a
near-zero purchase sample — so they read `low` / `abstain` even when backed by real install or
subscription volume. This is the **same** bug the action-plan fix removed, on paths that gate **writes**
(more consequential than the read-only action plan). `monitor.py`'s runaway path is ROAS-centric in its
metric too, so it is a weaker case — evaluate whether it should be goal-aware at all, or left as
ROAS/spend-only by design.

## Suggested approach

Promote the goal-aware selection to a **single shared helper** instead of copying
`_select_sample_conversions` into each module. Candidate home: a small goal-helpers module (or
alongside the metric selectors). Each producer that already resolves the account `goal`/`policy` then
grounds its sample through that one helper, exactly as `actions.evaluate_action_confidence` now does.
Coordinate with the field rename in `confidence-sample-conversions-rename` (same files) so the two
passes don't thrash the same lines — consider sequencing this one first (behavioral) then the rename
(cosmetic/structural), or folding them together.

## Acceptance criteria

- Install-goal accounts ground significance on subscriptions-then-installs (mirroring
  `_should_pause_ad` / `actions._select_sample_conversions`) on the control/authoring/rotation write
  paths; ROAS/default accounts are byte-identical to today.
- A decision (with rationale) on whether `monitor.py` should be goal-aware or stay ROAS/spend-only.
- The selection logic lives in one place, reused by `actions.py` and the other producers (no copy).
- New tests per producer: an install account with 0 purchases + real install volume reads above `low`;
  a ROAS account is unchanged.
- Full test suite + type-check/lint pass.

## Provenance

Found during review of `confidence-install-goal-significance`: that fix corrected the action plan but
left the sibling grounded producers (which share the same `assess` engine and the same
purchase-only sample) with the identical install-goal blind spot.
