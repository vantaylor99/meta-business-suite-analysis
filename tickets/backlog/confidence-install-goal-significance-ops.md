description: The enable/pause grounding path has the same blind spot as the action plan: for app-install accounts it only counts purchases (which they rarely have), so turning install-account ads on or off keeps reading low confidence even when lots of installs back the decision.
prereq: confidence-install-goal-significance
files: src/meta_ads_analysis/control.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Problem

`control._attach_status_grounding` (the grounding for `build_enable_ads_plan` / `build_pause_plan`
`set_status` ops) has the **identical** defect that `confidence-install-goal-significance` fixes in
the action plan: `_status_metric` selects the cost-per-install metric for install-goal accounts, but
the sample is always sourced from `metrics_row["purchases"]`:

```
sample_purchases=_num(metrics_row.get("purchases")),   # control.py ~699 and ~1350
```

For an install-goal account (`maximize_in_app_subscriptions`) `purchases` is typically 0, so the
conversions floor is never cleared and enable/pause grounding is structurally stuck — the same
"cost/install metric, purchases sample" mismatch the action-plan fix removes. The docs explicitly tie
the two paths together ("the same selection `actions._select_action_metric` uses"), so they should
agree on the conversion signal too.

This is filed as **backlog** (a parallel correctness gap in a different module with a different data
shape) rather than folded into the action-plan ticket, matching the codebase convention of duplicating
the small goal-selection helper per module (`actions._select_action_metric` vs
`control._status_metric` were duplicated, not shared).

## Data-shape note (important — differs from the action plan)

The action-plan path reads ad-summary fields `total_results` / `total_app_installs` /
`total_purchase_count`. The set_status path reads a `metrics_row` built by control.py
(see ~`control.py:976-987`), which carries `purchases` and `app_installs` but **no separate
`results`/subscription count** in that row shape. So the install-goal fallback here is necessarily
"`app_installs` when `purchases` is 0," not the subscriptions-first/installs-fallback ladder the
action plan uses — unless this ticket also threads a subscription/result count into the metrics row
(decide during implement; the simpler, defensible default is app-installs fallback).

## Use case / expected behavior

- Enabling or pausing an install-goal ad backed by genuine install volume should be able to read
  `medium`/`high` on its real signal, not be capped at `low` because purchases are 0.
- The cold-enable / structural-pause asymmetry (zero-sample vs no-sample) and the apply-time grounding
  guard must be preserved exactly — this only changes *which conversion count* fills the cited sample,
  never whether a sample is cited.
- Operator-facing wording stays consistent with the action-plan fix ("conversions", not "purchases").

## Notes for whoever plans/implements this

- Reuse the wording/`_fmt_conversions` changes already landed in confidence.py by the prereq.
- Keep the serialized `sample_purchases` JSON key (the rename is tracked separately in
  `confidence-sample-conversions-rename`).
- Cover the cold-enable (zero sample → abstain → gate blocks) and structural-pause (no sample → gate
  allows) boundaries for an install-goal account in tests.
