description: A field in the recommendation-evidence record is named "sample_purchases", but it now holds whatever conversion the account optimizes for (subscriptions or app installs, not just purchases). Rename it so the name stops implying purchases-only — a repo-wide change to keep for a deliberate, well-tested pass.
prereq: confidence-install-goal-significance-ops, enable-wrong-direction-install-goal, goal-aware-grounding-other-producers
files: src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/experiment.py, src/meta_ads_analysis/knowledge_provenance.py, tests/test_meta_ads_analysis.py
----

## Why

After `confidence-install-goal-significance`, `Evidence.sample_purchases` and the
`data_strength(sample_purchases=…)` parameter hold the account's **conversion** count (purchases for
ROAS accounts, in-app subscriptions or app installs for install accounts) — so the name
`sample_purchases` lies for roughly half the managed accounts. That ticket deliberately left the
structural rename out of scope because the symbol is cross-cutting and the cosmetic wording fix
("purchases" → "conversions" in operator-facing strings) already removed the *visible* lie. This
ticket finishes the job: rename the field, the parameter, and the serialized JSON key to
`sample_conversions`.

## Scope (large — that is why it is deferred)

`sample_purchases` appears in **10 source modules** and ~30 test sites, and — critically — it is a
**serialized JSON key** persisted in `action_plan.json` and read back by:

- `confidence.evidence_to_dict` / `evidence_from_dict`,
- the apply-time grounding guard `write_grounding.py` (`ev.get("sample_purchases")`),
- `review.py`, `monitor.py`, and the per-capability grounded producers (`control.py`, `authoring.py`,
  `rotation.py`, `experiment.py`, `knowledge_provenance.py`).

So a clean rename must either (a) bump the evidence `schema_version` and migrate, or (b) read **both**
keys on the way in (`sample_conversions` preferred, `sample_purchases` legacy fallback) while writing
only the new key — so previously written plans still deserialize. Option (b) is lower-risk and the
recommended default; confirm there is no place that pattern-matches the literal string
`"sample_purchases"` that would be missed.

## Acceptance criteria

- `Evidence.sample_conversions`, `data_strength(sample_conversions=…)`, and the serialized key are
  consistently renamed across all modules and tests.
- Old `action_plan.json` files (with the legacy `sample_purchases` key) still load and render — pin
  this with a deserialization test using a fixture that carries the legacy key.
- No behavior change: bands, factors, and gate decisions are identical for every goal before/after.
- Full test suite + type-check/lint pass.

## Provenance

Spun out of `confidence-install-goal-significance` (the install-goal significance fix), which made the
field hold conversions but kept the legacy name to avoid a risky drive-by rename in a single change.
