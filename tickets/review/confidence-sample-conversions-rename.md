description: A data field was renamed from "sample_purchases" to "sample_conversions" everywhere because it now holds whatever conversion an account optimizes for, not just purchases; older saved plans using the old name still load. Verify the rename is complete and the back-compat read path is sound.
prereq:
files: src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/experiment.py, src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/early_triage.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What was implemented

Renamed the evidence field / `data_strength` keyword / serialized JSON key
`sample_purchases` → `sample_conversions` across the whole repo. The field holds the
account's **conversion** count (purchases for ROAS accounts; in-app subscriptions / app
installs for install accounts), so `purchases` lied for ~half the managed accounts. This is
a **pure rename**: no band, factor, or gate decision changes for any goal — proven by the
existing assertions passing unchanged except for the key-string spelling.

**Back-compat strategy used (Option b — write new, read both):**
- `evidence_to_dict` (and `_empty_evidence_dict` in `write_grounding.py`) write **only**
  `"sample_conversions"`.
- A single shared helper `sample_conversions_from_dict(ev)` in `confidence.py` reads the
  serialized dict, preferring `sample_conversions` and falling back to legacy
  `sample_purchases`: `ev.get("sample_conversions", ev.get("sample_purchases"))`.
- All **4** serialized-dict read sites route through that helper:
  `confidence.py` `evidence_from_dict`, `write_grounding.py` abstain guard (`op_grounding_gap`),
  `actions.py:615` insufficient-data rationale, `review.py:234` sample-floor check.
- `schema_version` is **untouched** (stays at 1 — no migration). Confirmed: `git diff` has
  no `schema_version` lines.

## What to verify (use cases)

1. **No string left behind.** `grep -rn "sample_purchases" src/ tests/ docs/` must return ONLY:
   - `confidence.py:433` + `:436` — the helper docstring + the fallback lookup in the helper body.
   - `docs/META_ACTION_WORKFLOW.md:108` — the doc note that legacy is accepted on read.
   - `tests/test_meta_ads_analysis.py` — the dedicated back-compat test
     (`test_evidence_from_dict_reads_legacy_sample_purchases_key`): its name, docstring, the
     fixture key `"sample_purchases": 120.0`, and the `!= "sample_purchases"` dict filter.
   Note: unlike the original plan's wording ("4 read sites + helper body"), the read sites no
   longer contain the literal — they call `sample_conversions_from_dict(...)`. The literal lives
   only in the centralized helper, which is what the plan's "route all four through it"
   directive intended. This is the cleaner, correct outcome, not a deviation to flag.

2. **Back-compat read.** A legacy `action_plan.json` (key `sample_purchases`) must still load.
   The new test pins: `evidence_from_dict({legacy})` picks up the value; the `op_grounding_gap`
   thin-data block still fires for a legacy cited sample under an `abstain` band; a structural
   abstain (neither key) is still allowed through.

3. **Edge cases (covered by the new test):**
   - Mixed-key dict → `sample_conversions` wins over `sample_purchases`.
   - Explicit `sample_conversions: null` is honored as `None` and does NOT fall through to the
     legacy key (`.get(new, default)` semantics, not `or`-chaining — `0.0`/`None` are not "missing").
   - Zero vs missing not collapsed: `0.0` (cited-zero cold branch) and `None` (structural abstain)
     stay distinct through the grounding guard's `is not None` check.

4. **Right-hand sides preserved.** The kwarg rename touched only the **left** side. Metric reads
   like `m.get("purchases")` (monitor.py 368/833), `fresh.purchases`, `control["purchases"]`,
   `own.results`, `triaged_sums.results` are unchanged. `experiment.py` local var
   `sample_purchases` → `sample_conversions` (purely local, both def and use).

## Tests / validation run

- Full suite: `.venv/bin/python -m pytest -q` → **373 passed in ~0.75s**.
- New test: `test_evidence_from_dict_reads_legacy_sample_purchases_key` passes (also
  re-confirmed alongside the round-trip test).
- AST parse of all changed `src/` files + the test file: clean.
- `git diff --stat`: exactly the 13 ticketed files, +139/-92.

## Known gaps / honest caveats (treat tests as a floor)

- **No end-to-end legacy-file load.** The back-compat path is pinned at the unit level
  (`evidence_from_dict` + `op_grounding_gap` directly). There is **no test that writes a real
  legacy `action_plan.json` to disk with the old key and runs it through a full CLI command**
  (`apply_ops`, `apply_rotation`, `operator_brief`, `review`). The two other read sites
  (`actions.py:615`, `review.py:234`) use the same helper so they are correct by construction,
  but are **not independently exercised with a legacy-key fixture**. A reviewer wanting belt-and-
  suspenders could add a CLI-level legacy-plan fixture test.
- **No static type/lint gate exists in this repo.** `pyproject.toml` dev deps are pytest only
  (no mypy/ruff/pyright config). The rename was verified by AST parse + the test suite, not by a
  type checker. If the project later adds mypy, the renamed dataclass field / kwarg should be
  re-checked there.
- **Doc:** `docs/META_ACTION_WORKFLOW.md:108` was updated to state the key is now
  `sample_conversions` with `sample_purchases` accepted as a legacy alias on read.

## Provenance

Spun out of `confidence-install-goal-significance`. The operator-facing wording change
("purchases" → "conversions" in strings) already landed there; this finished the structural
rename of the symbol and serialized key.
