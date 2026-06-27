description: When you turn an app-install ad on/off or move its budget, the system now measures how sure it is by counting installs (which those accounts actually produce) instead of purchases (which they almost never have), so a well-backed decision is no longer stuck at "low confidence."
files: src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py
----

## What shipped

Both `control.py` grounding producers (`build_enable_ads_plan` → `_attach_status_grounding`, and the
budget surface → `_attach_budget_grounding`) now select their **significance sample** by account goal,
mirroring the goal-aware *metric* the prereq (`confidence-install-goal-significance`) already landed.

- New helper `_status_sample_conversions(metrics_row, goal)` (control.py ~619): `app_installs` for
  `goal == "maximize_in_app_subscriptions"`, else `purchases`. Keys on the literal goal string only —
  same selector shape as `actions._select_sample_conversions`.
- `_attach_status_grounding` gained a required keyword `sample_conversions`; the **present-row** branch
  cites it. **Both `metrics_row is None` branches are unchanged** (structural pause → `None`; cold
  enable → `0.0`), so the cold-ad and structural-pause asymmetries are untouched.
- Three call sites pass a sample that agrees with their own metric: enable → goal-aware installs;
  `roas_below` pause → purchases (agrees with the hardcoded `blended_roas`); budget → goal-aware
  installs.
- The serialized `sample_purchases` JSON key / `Evidence` field is intentionally **not** renamed —
  that rename is owned by `confidence-sample-conversions-rename` (sits in `tickets/plan/`).

## Review findings

**Verdict: clean. No major findings → no new tickets filed. No minor findings → nothing fixed inline.**
Reviewed the implement diff (`d0e73fe`) with fresh eyes before reading the handoff.

### What was checked

- **Lint + tests.** No lint tooling is configured (`pyproject.toml` declares only
  `[tool.pytest.ini_options]` — no ruff/mypy/pyright/black). `.venv/bin/python -m pytest
  tests/test_meta_ads_analysis.py` → **361 passed in 0.45s**. Working tree clean.
- **Every `Evidence(` construction in `control.py` (5 sites)** — confirmed the two None-row branches in
  each producer (697, 710, 1385) stay goal-independent (`None`/`0.0`), and only the two present-row
  branches (723, 1392) route through the goal-aware sample. No producer left on a hardcoded purchases
  sample.
- **All callers of the changed signature.** `_attach_status_grounding` gained a *required* keyword
  (no default) → a missed caller would be a `TypeError`. Both callers (818 enable, 1275 pause) pass it;
  no third caller exists. `_attach_budget_grounding`'s `goal` param pre-existed; all four callers
  (1442/1472/1493/1519) already supply it.
- **Sample/metric agreement per call site** (the reviewer-focus item): enable installs-vs-installs ✓;
  `roas_below` pause purchases-vs-`blended_roas` ✓ (correctly does NOT switch to installs even for an
  install-goal account — the pause was *selected* by ROAS); budget installs-vs-installs ✓.
- **`fetch_entity_metrics` row shape** — verified rows genuinely carry `app_installs` + `purchases`
  but **no** separate subscription/`results` count, so the documented collapse of the action plan's
  subscriptions-first ladder to `app_installs` here is forced, not a shortcut.
- **Apply-time gate does not re-introduce the bug.** `apply_ops_plan` gates via
  `op_grounding_gap(op["confidence"], op["evidence"])` — it trusts the *cited* evidence and never
  recomputes the sample from purchases, so the goal-aware install sample carries through to the gate.
- **`build_pause_plan` goal-blindness** — confirmed it takes no `goal`/`policy` param and never
  consults the goal; the implementer's decision to skip a `build_pause_plan(..., install-goal)`
  structural-pause test is correct (that path routes through the untouched `metrics_row is None,
  cold_cites_zero=False` branch). Coverage via the existing structural-pause test + the new
  `test_attach_status_grounding_none_row_ignores_sample_conversions` unit guard is sufficient.
- **No-goal-but-installs-present asymmetry** (metric `cost_per_app_install`, sample stays purchases) —
  reviewed and **agree it is intentional, not a defect**: it preserves byte-for-byte parity with
  `actions._select_sample_conversions` (which also keys only on the explicit install-goal string), and
  defaulting an unknown-goal account to the stricter purchases sample is the conservative choice.
  Pinned by `test_enable_ads_no_goal_installs_present_keeps_sample_on_purchases`.
- **Docs / stale claims.** confidence.py renders the sample as "conversions" throughout (prereq's
  work), so operator-facing text is correct despite the un-renamed key. Grepped the repo for stale
  "install ops cap at low"-style claims — none survive (the `_bud_install_insights` docstring claim was
  updated in this diff; remaining "cap at low" hits are about the unrelated `external` evidence tier).
  The `Evidence` dataclass docstring is generic and not made wrong by the key keeping its name.

### Test coverage assessment

8 new + 3 updated + 1 docstring-only — covers the core fix on both surfaces (enable + budget),
zero-purchases-real-installs floor clearing, the ROAS-goal decoy parity guard on both surfaces, the
cold-ad abstain→gate-blocks boundary, the unit-level proof that the None branches ignore
`sample_conversions`, the `roas_below`-stays-on-purchases guard, the no-goal asymmetry pin, and the
thin-present-row abstain→block case. Edge/error/regression/interaction paths are all represented.
Campaign/CBO-level install-goal sampling has no dedicated test, but the helper is level-agnostic
(operates on the row) and the adset-level budget test exercises the same code path — acceptable.

## Deferred / out of scope (unchanged from handoff, confirmed appropriate)

- Renaming the `sample_purchases` key → `confidence-sample-conversions-rename`.
- Threading a real subscription/`results` count into `fetch_entity_metrics` rows so the install-goal
  selector could prefer subscriptions over installs (matching the action plan's full ladder) — a
  larger, separate change.
