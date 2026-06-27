description: A data field is named "sample_purchases" but now holds whatever conversion an account optimizes for (purchases, subscriptions, or app installs), so the name is misleading for about half of accounts. Rename it to "sample_conversions" everywhere while still loading older saved plans that use the old name.
prereq: confidence-install-goal-significance-ops, enable-wrong-direction-install-goal, goal-aware-grounding-other-producers
files: src/meta_ads_analysis/confidence.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/write_grounding.py, src/meta_ads_analysis/review.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/experiment.py, src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/early_triage.py, docs/META_ACTION_WORKFLOW.md, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Goal

Rename `Evidence.sample_purchases`, the `data_strength(sample_purchases=…)` keyword
parameter, and the serialized JSON key `"sample_purchases"` to `sample_conversions`
across the whole repo. The field now holds the account's **conversion** count (purchases
for ROAS accounts; in-app subscriptions / app installs for install accounts), so the
`purchases` name lies for ~half the managed accounts.

This is a pure rename: **no band, factor, or gate decision may change for any goal.**

## Why this is its own ticket

`sample_purchases` appears at **91 sites across 12 Python modules + 1 doc**, and it is a
**serialized JSON key** persisted in `action_plan.json` files that older runs already wrote
to disk. A naive rename would make previously-written plans fail to deserialize. The
back-compat strategy below is the whole reason this was deferred out of
`confidence-install-goal-significance`.

## Back-compat strategy (decided — do NOT re-open)

**Option (b): write only the new key, read both keys.**

- `evidence_to_dict` writes **only** `"sample_conversions"`.
- Every site that reads the serialized JSON key must prefer `sample_conversions` and fall
  back to the legacy `sample_purchases` when the new key is absent, e.g.
  `ev.get("sample_conversions", ev.get("sample_purchases"))`. (`.get(new, default)` returns
  the legacy lookup only when the new key is missing; an explicit `None` under the new key
  is preserved, which is correct.)
- This is lower-risk than bumping `schema_version` + migrating, and the action-plan
  `schema_version` stays at `1` (no migration). Do **not** bump it.

There are exactly **4 serialized-dict read sites** in `src/` (confirmed by
`grep '\.get("sample_purchases")\|\["sample_purchases"\]'`). To avoid drift, add one shared
helper in `confidence.py` and route all four through it:

```python
def sample_conversions_from_dict(ev: dict[str, Any]) -> float | None:
    """Read the conversion-count sample from a serialized evidence dict, preferring the
    current ``sample_conversions`` key and falling back to the legacy ``sample_purchases``
    key so older action_plan.json files still load."""
    return ev.get("sample_conversions", ev.get("sample_purchases"))
```

The four read sites:
- `confidence.py:438` — `evidence_from_dict` (`data.get("sample_purchases")`)
- `write_grounding.py:139` — `ev.get("sample_purchases") is not None or …`
- `actions.py:615` — `evidence_block.get("sample_purchases")`
- `review.py:234` — `_num(evidence.get("sample_purchases"))`

`write_grounding.py:139` only needs the `is not None` truthiness, so
`sample_conversions_from_dict(ev) is not None or ev.get("sample_spend") is not None` is the
right rewrite there.

## What changes where (the other 87 sites are mechanical)

All non-read sites are one of:

1. **Field definition** — `confidence.py:112` `Evidence.sample_purchases` → `sample_conversions`.
2. **`data_strength` parameter + its internal uses** — `confidence.py:159-209` (signature line
   161 and the local uses at 176, 184, 199, 206). Internal references can keep the local
   alias `purchases = …` if you like, but the **keyword name** must become `sample_conversions`.
   The `render_evidence_line` read at `confidence.py:471` is `evidence.sample_purchases` (a
   field access, not a dict) → rename to the field's new name.
3. **`evidence_to_dict` write key** — `confidence.py:407` → `"sample_conversions"`.
4. **Python keyword arguments at construction** — every `Evidence(... sample_purchases=…)`
   and `data_strength(sample_purchases=…)` call. These are Python kwargs and MUST match the
   renamed field/param. Sites:
   - `actions.py:557`
   - `monitor.py:116, 368, 550, 833` (note `368`/`833` are `sample_purchases=m.get("purchases")`
     — the **left** side is the kwarg to rename; the `.get("purchases")` on the right reads a
     metrics dict and stays as-is)
   - `control.py:702, 715, 728, 1387, 1394`
   - `authoring.py:311, 359, 365, 399`
   - `rotation.py:171, 193, 206, 618`
   - `experiment.py:178` (and the local var `sample_purchases` at `experiment.py:173` — rename
     it to `sample_conversions` for readability; it is purely local)
   - `knowledge_provenance.py:670`
   - `early_triage.py:381` (⚠️ **not listed in the original plan's `files:`** — it has
     `sample_purchases=triaged_sums.results`; include it)
5. **Doc** — `docs/META_ACTION_WORKFLOW.md:108` currently says the serialized key "remains
   `sample_purchases` … renaming it to `sample_conversions` is tracked separately." Update it
   to state the key is now `sample_conversions`, with `sample_purchases` accepted as a legacy
   alias on read.

## Tests

`tests/test_meta_ads_analysis.py` has ~50 sites. Categorize and update:

- **Construction kwargs** (`Evidence(sample_purchases=…)`, e.g. lines 1218, 1423, 1507) →
  `sample_conversions=…`.
- **Assertions on serialized output** (`op["evidence"]["sample_purchases"]`, the bulk of the
  hits — 2865, 3008, 3034, 3052, …, 5304) → `["sample_conversions"]`, because
  `evidence_to_dict` now writes the new key. Keep the inline comments (they explain the
  install-goal grounding behavior) — only the key string changes.
- **Input fixture dicts** that build a plan to feed `review_rotation_plan` /
  `evidence_from_dict` (lines 2904, 2929, 3465, 4513, 4560) — convert these to
  `"sample_conversions"` so they exercise the current shape, **except** the dedicated
  back-compat test below.
- Line `3217` mutates a fixture in place (`plan["rotations"][1]["evidence"]["sample_purchases"] = 9.0`)
  — update the key to match whatever that fixture now uses.

**New back-compat test (the acceptance pin):** add a test that constructs a serialized
evidence dict using the **legacy** `"sample_purchases"` key, runs it through `evidence_from_dict`
(and through one downstream renderer/guard that reads the dict — e.g. `review_rotation_plan`
or the `write_grounding` abstain guard), and asserts the legacy value is still picked up
(non-None, equal to the fixture value). This is the regression guard proving old
`action_plan.json` files still load. Name it e.g.
`test_evidence_from_dict_reads_legacy_sample_purchases_key`.

## Edge cases & interactions

- **Mixed-key dict** — if both `sample_conversions` and `sample_purchases` are present,
  `sample_conversions` wins. (Won't happen from our writers, but the read helper must be
  deterministic.) An explicit `sample_conversions: null` must be honored as `None`, NOT fall
  through to the legacy key — `.get(new, default)` already does this; do not use
  `or`-chaining, which would wrongly treat `0.0`/`None`/`0` as missing.
- **Zero vs missing** — `sample_purchases`/`sample_conversions` is legitimately `0.0`
  (cited-zero cold branch) and legitimately `None` (structural abstain, no sample cited).
  The grounding guard distinguishes these (`is not None`). Do not collapse `0.0` and `None`.
- **`write_grounding` abstain guard** — the structural-abstain allowance (band `abstain` +
  no sample cited) must still pass, and the thin-data block (band `abstain` + sample cited)
  must still fire, for BOTH key spellings. Cover with the back-compat test touching this guard.
- **No string left behind** — after the change, `grep -rn "sample_purchases" src/ tests/`
  should return ONLY: the read helper's fallback lookups (4 read sites + the helper body) and
  the dedicated legacy-key back-compat test fixture. Nothing else.
- **`schema_version` untouched** — confirm no action-plan `schema_version` is bumped by this
  change; the rename is value-shape compatible on read.
- **Helper placement / imports** — `actions.py`, `review.py`, `write_grounding.py` must import
  `sample_conversions_from_dict` from `confidence`. Confirm there is no import cycle
  (`confidence` is already imported by these modules for `Evidence`/`assess`, so this is safe).

## TODO

### Phase 1 — core (confidence.py)
- Rename `Evidence.sample_purchases` → `sample_conversions` (field def + `render_evidence_line` access).
- Rename `data_strength` keyword param + internal uses to `sample_conversions`.
- `evidence_to_dict`: write only `"sample_conversions"`.
- Add `sample_conversions_from_dict(ev)` helper (prefer new key, fall back to legacy).
- `evidence_from_dict`: read via the new helper.

### Phase 2 — read sites
- `write_grounding.py:139`, `actions.py:615`, `review.py:234`: read via `sample_conversions_from_dict`.

### Phase 3 — kwarg callers (mechanical rename only)
- `actions.py`, `monitor.py`, `control.py`, `authoring.py`, `rotation.py`, `experiment.py`
  (incl. local var), `knowledge_provenance.py`, `early_triage.py`.

### Phase 4 — docs
- Update `docs/META_ACTION_WORKFLOW.md:108`.

### Phase 5 — tests
- Rename construction kwargs and serialized-key assertions.
- Convert input fixtures to the new key (except the back-compat fixture).
- Add `test_evidence_from_dict_reads_legacy_sample_purchases_key` exercising
  `evidence_from_dict` + at least one dict-read guard.

### Phase 6 — verify
- `grep -rn "sample_purchases" src/ tests/` returns only the expected back-compat sites.
- Run full test suite + type-check/lint (see AGENTS.md). Stream output with `tee`.
- Confirm no band/factor/gate output changed (the existing assertions, with only the key
  string updated, are the proof — they assert the same values).

## Provenance

Spun out of `confidence-install-goal-significance`. The cosmetic operator-facing wording
("purchases" → "conversions" in strings) already landed there; this finishes the structural
rename of the symbol and serialized key.
