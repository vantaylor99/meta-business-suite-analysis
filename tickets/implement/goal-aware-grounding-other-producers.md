description: For app-install accounts, the go-live (authoring) and audience-rotation recommendation paths still measure their confidence by counting purchases — which those accounts almost never have — so a decision backed by real install volume reads "low confidence." Make those two paths count installs for install-goal accounts, the same way the action plan and the enable/budget paths already do.
prereq: confidence-install-goal-significance-ops
files: src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Why (the bug)

`confidence-install-goal-significance` (action plan) and `confidence-install-goal-significance-ops`
(control.py enable/pause + budget) made the **significance sample** that grounds a confidence band
**goal-aware**: for `maximize_in_app_subscriptions` accounts they count installs (the conversion the
account actually produces) instead of purchases. Two sibling grounded producers that feed the same
`confidence.assess` engine were left behind and still ground `sample_purchases` on the **purchase**
count unconditionally:

- `authoring.py:358` — `_attach_duplicate_grounding`, present-row branch: `sample_purchases=_num(row.get("purchases"))`.
- `rotation.py:203` — `_attach_rotation_grounding`, present-row branch: `sample_purchases=_num(row.get("purchases"))`.

Both producers already pick their **metric** goal-aware (they call `control._status_metric`, which
returns `cost_per_app_install` for an install goal), but ground the **sample** on purchases. Because
`purchases` and `app_installs` come from distinct key sets in a `fetch_entity_metrics` row, an
install account has `purchases ≈ 0` while `app_installs` is populated — so the band reads `low` /
`abstain` even when backed by real install volume. These are **write paths** (authoring go-live,
rotation/fatigue swaps), so the blind spot is more consequential than the read-only action plan it
mirrors.

## Design decision: reuse `control._status_sample_conversions` (not a new per-module helper)

The plan ticket suggested adding a small per-module sample selector to each of `authoring.py` and
`rotation.py`, on the premise that "the codebase deliberately duplicates these tiny goal selectors
per module." Investigation of the actual code overturns that premise for these two modules:

- `actions.py` and `control.py` each have their **own** metric selector
  (`_select_action_metric` / `_status_metric`) and sample selector
  (`_select_sample_conversions` / `_status_sample_conversions`) — those two are the duplicated pair.
- **`authoring.py` and `rotation.py` do NOT duplicate** — they already **import and reuse**
  `control._status_metric` for metric selection (authoring at its top-level `from .control import (…)`
  block; rotation via a function-body `from .control import _status_metric` to dodge the
  `control → rotation` circular import).

Since the *metric* selector in both modules is already shared from `control`, the *sample* selector
must come from the same place to stay paired — sample/metric agreement is the exact invariant this
fix exists to preserve. `control._status_sample_conversions(metrics_row, goal)` is the natural
sibling of `control._status_metric` and operates on the **identical** `fetch_entity_metrics` row
shape both producers already consume (`app_installs` for the install goal, else `purchases`). It
collapses the action plan's subscriptions-first ladder to `app_installs` because
`fetch_entity_metrics` rows carry no separate subscription count — the same forced collapse
control.py documents and the ops review confirmed (rows carry `app_installs` + `purchases`, no
`results`/subscription field).

Reusing the existing control helper is **not** the "new shared helper" the plan ticket rejected: it
forces **zero** refactor of just-landed control code (just one more import, exactly as
`_status_metric` is already imported), and it guarantees authoring/rotation can never drift from the
metric language control already gives them. This is the cleaner, lower-risk option and is adopted
here.

### Confirmed row-shape provenance
- `authoring._attach_duplicate_grounding`: `row` comes from
  `fetch_entity_metrics(reader, ad_account_id, level="ad", …)` → carries `app_installs`/`purchases`.
- `rotation._attach_rotation_grounding`: `row = metrics_by_id.get(str(adset_id))`, and
  `metrics_by_id` is built in `cli.py:511` from `fetch_entity_metrics(client, …, level="adset", …)`
  → same shape.

## Design decision: monitor.py stays ROAS/spend-only (NO change)

The acceptance criterion asks for an explicit decision on `monitor.py`. **Decision: do not change it.**

`monitor.classify_ad` (the steady-state watch path) is structurally ROAS-centric — it takes
`roas`/`roas_floor`/`roas_target`, derives `dollars_at_risk` from ROAS, classifies
urgent/underperforming/ok by ROAS thresholds, and hardcodes `metric_name="roas"`. Its sample
(`results` = `m.get("purchases")`) is the conversion **behind** ROAS, so its sample **already agrees
with its metric**. monitor therefore does **not** exhibit this ticket's bug (goal-aware metric +
purchase sample = mismatch); it has a ROAS metric + a ROAS-consistent sample.

Critically, monitor's install-goal need is **already served** elsewhere: the early-life
forced-decision path is goal-aware (`_early_life_forced_decision` routes `goal_kind(policy) ==
"install"` to `_forced_decision_install`, which grounds `sample_purchases=own.results` as installs at
`monitor.py:550`). Switching only the steady-state *sample* to installs while leaving the *metric*
and *thresholds* on ROAS would manufacture the very sample/metric disagreement this ticket removes
everywhere else.

The genuinely separate concern — that a *mature* install-goal ad past the early-life window is graded
by `classify_ad` on ~0 ROAS at all — is a goal-aware-*classification* feature, not a sample-grounding
consistency fix, and is parked in `backlog/monitor-steady-state-install-goal-classification.md`.

## TODO

### Phase 1 — authoring.py
- Add `_status_sample_conversions` to the existing `from .control import ( … )` block (alongside
  `_status_metric`).
- In `_attach_duplicate_grounding`, present-row (`else`) branch only (line ~358), change
  `sample_purchases=_num(row.get("purchases"))` → `sample_purchases=_status_sample_conversions(row, goal)`.
  Leave `sample_spend=_num(row.get("spend")) or 0.0` untouched.
- Do **not** touch `_attach_netnew_grounding` (zero sample, cold create — goal-independent) or
  `_attach_lookalike_grounding` (None sample, structural). These mirror control's `metrics_row is
  None` branches, which the ops fix deliberately left goal-independent.
- Update the `_attach_duplicate_grounding` docstring's "cite a zero sample / real purchases/spend
  sample" line to say the present-row sample is the goal-aware conversion count.

### Phase 2 — rotation.py
- In `_attach_rotation_grounding`, extend the existing function-body import
  `from .control import _status_metric` → `from .control import _status_metric, _status_sample_conversions`.
- In the present-row (`else`) branch only (line ~203), change
  `sample_purchases=_num(row.get("purchases"))` → `sample_purchases=_status_sample_conversions(row, goal)`.
  Leave `sample_spend=_num(row.get("spend")) or 0.0` untouched.
- Do **not** touch the `metrics_by_id is None` branch (None sample, structural) or the `row is None`
  branch (zero sample) — both goal-independent, mirroring control.
- Update the `_attach_rotation_grounding` docstring bullet that says "cite its real purchases/spend
  sample" to name the goal-aware conversion sample.

### Phase 3 — tests (tests/test_meta_ads_analysis.py)
Mirror the test shapes the ops ticket landed for control (`test_enable_ads_*` /
`test_*_sample_conversions`). For **each** of authoring (duplicate) and rotation:
- **Install account, 0 purchases + real install volume reads above `low`.** Build a row with
  `purchases=0`, `app_installs` well above `CONFIDENCE_CONVERSIONS_FLOOR`, real `spend`, and an
  install-goal policy (`primary_goal="maximize_in_app_subscriptions"`); assert the produced
  `confidence.band` is **not** `low`/`abstain`, and that `evidence.sample_purchases` equals the
  install count (proves it grounded on installs, not the 0 purchases).
- **ROAS-account decoy / parity.** Same row but ROAS/default goal; populate `app_installs` to a large
  decoy value and assert `evidence.sample_purchases` equals `purchases` (installs ignored) — i.e. the
  ROAS/default path is **byte-identical** to today.
- **None/zero branches unaffected.** Confirm a no-row (zero-sample) and/or no-metrics
  (None/structural) rotation/authoring case still abstains regardless of goal — the goal-aware
  selector must touch only the present-row branch.

### Phase 4 — validate
- `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/goal-grounding.log`
  — full suite must pass.
- No ruff/mypy/pyright/black is configured (pyproject declares only `[tool.pytest.ini_options]`), so
  there is no separate lint/type-check step. If any pre-existing failure surfaces unrelated to this
  diff, follow the `.pre-existing-error.md` protocol and continue.

## Edge cases & interactions

- **Sample/metric agreement (the core invariant).** After the change, present-row authoring/rotation
  must cite the SAME conversion family their metric uses: install goal → `cost_per_app_install`
  metric + `app_installs` sample; ROAS/default → `blended_roas`/ROAS metric + `purchases` sample. A
  test must pin both directions.
- **No-goal-but-installs-present asymmetry.** `_status_sample_conversions` keys on the literal
  `"maximize_in_app_subscriptions"` string only. For an account with no goal where
  `_status_metric` falls through to `cost_per_app_install` (installs present, ROAS absent), the
  sample deliberately STAYS on `purchases` — byte-for-byte parity with control/actions. Do not
  "fix" this; it is the documented conservative default. A parity test is welcome but the behavior
  must not change.
- **None / zero / structural branches stay goal-independent.** Net-new create (zero), lookalike
  (None), rotation-without-metrics (None), and no-row-in-window (zero) must remain untouched — the
  selector applies ONLY where a real present row is cited. This mirrors the ops fix, which left both
  `metrics_row is None` branches goal-independent; a regression here would change cold-create /
  structural-abstain behavior.
- **Circular import (rotation only).** `control` imports `rotation` at module load, so rotation must
  keep the **function-body** import of `_status_sample_conversions` (not a top-level import) — same
  reason `_status_metric` is imported in-body. A top-level import will raise `ImportError` at load.
- **Apply-time gate carry-through.** Both producers' plans are gated at apply time via
  `write_grounding.op_grounding_gap(...)` on the CITED evidence — the gate trusts the cited
  `sample_purchases`/band and never recomputes from `purchases`, so the goal-aware install sample
  carries through to the gate decision unchanged (confirmed for the ops path; same gate here).
- **Rotation correlational cap.** Rotation grounds at `ROTATION_EVIDENCE_TIER = correlational`, so
  even with a healthy install sample the band is capped at `medium` (never `high`). "Above `low`"
  for the rotation test means `medium`/`low`-but-not-`abstain` per the cap — assert "not
  `low`/`abstain`", not "== `high`".
- **Serialized key name unchanged.** Keep writing the `sample_purchases` field/JSON key. The
  field/param/key rename to `sample_conversions` is owned by `confidence-sample-conversions-rename`,
  which lists THIS ticket as a prereq (runs after) — do not rename here or the two passes thrash the
  same lines.

## Provenance

Found during review of `confidence-install-goal-significance`; the control.py half landed as
`confidence-install-goal-significance-ops`. This ticket finishes the sweep on the remaining grounded
write producers (authoring, rotation) and records the monitor decision.
