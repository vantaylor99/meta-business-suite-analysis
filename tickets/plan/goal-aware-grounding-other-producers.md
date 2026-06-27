description: For app-install accounts, several recommendation paths still measure confidence by counting purchases (which those accounts rarely have) even though they already judge the account on installs — so they get stuck reading "low data" the same way the action plan used to. Extend the install-aware fix to those remaining paths.
prereq: confidence-install-goal-significance
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/actions.py, tests/test_meta_ads_analysis.py
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

- `authoring.py:358` — `sample_purchases=_num(row.get("purchases"))`.
- `rotation.py:203` — `sample_purchases=_num(row.get("purchases"))`.
- `monitor.py:210` (and `:95`) — `sample_purchases=m.get("purchases")` / `results`.

> **Scope note:** `control.py` (both the enable/pause `set_status` grounding at `:699` and the
> budget-move/ops grounding at `:1359`) is **carved out to its own ticket
> `confidence-install-goal-significance-ops`** to keep each ticket to one agent run and avoid two
> implement tickets editing the same `control.py` lines. THIS ticket now covers `authoring.py`,
> `rotation.py`, and `monitor.py` only. The control.py fix uses a per-module helper
> (`control._status_sample_conversions`) following the established per-module convention.

The sharp inconsistency this generalizes (already fixed in control.py by the carve-out ticket): a
producer can pick `cost_per_app_install` as the goal-aware *metric* for an install account while still
grounding the *sample* on the purchase count. `purchases` and `app_installs` come from **distinct** key
sets, so for an install account `purchases ≈ 0` while `app_installs` is populated.

### Impact

For a `maximize_in_app_subscriptions` account, the authoring go-live grounding and the rotation/fatigue
grounding compute their confidence band off a near-zero purchase sample — so they read `low` /
`abstain` even when backed by real install or subscription volume. This is the **same** bug the action-plan fix removed, on paths that gate **writes**
(more consequential than the read-only action plan). `monitor.py`'s runaway path is ROAS-centric in its
metric too, so it is a weaker case — evaluate whether it should be goal-aware at all, or left as
ROAS/spend-only by design.

## Suggested approach

Follow the established **per-module helper** convention (as `actions._select_sample_conversions` and
`control._status_sample_conversions` do): give `authoring.py` and `rotation.py` each a small goal-aware
sample selector mirroring those, keyed on `maximize_in_app_subscriptions`, grounded through the goal
each producer already resolves. (A single shared helper was considered but rejected: the codebase
deliberately duplicates these tiny goal selectors per module rather than sharing them, and control.py
already shipped its own — a shared helper would force a refactor of just-landed code.) Watch each
producer's own row shape for whether a subscriptions/`results` count is available or the selector
collapses to installs (as control.py's does). Coordinate with the field rename in
`confidence-sample-conversions-rename` (same files) so the two passes don't thrash the same lines —
sequence this one first (behavioral) then the rename (structural).

## Acceptance criteria

- Install-goal accounts ground significance on the install-family signal (subscriptions-then-installs
  where available, else installs — mirroring `_should_pause_ad` / `actions._select_sample_conversions`)
  on the authoring/rotation write paths; ROAS/default accounts are byte-identical to today.
- A decision (with rationale) on whether `monitor.py` should be goal-aware or stay ROAS/spend-only.
- New tests per producer: an install account with 0 purchases + real install volume reads above `low`;
  a ROAS account is unchanged.
- Full test suite + type-check/lint pass.

## Provenance

Found during review of `confidence-install-goal-significance`: that fix corrected the action plan but
left the sibling grounded producers (which share the same `assess` engine and the same
purchase-only sample) with the identical install-goal blind spot.
