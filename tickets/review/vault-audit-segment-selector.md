description: A stored vault number that was sliced by two dimensions at once (e.g. "Instagram, across all placements") couldn't be re-checked automatically; it can now, by letting the fact name its exact slice with a new `select:` field.
prereq:
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, knowledge/learnings.md, knowledge/README.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

`audit-vault` re-checks a stored `metric:` claim by matching the metric *name* (token overlap) to a
single fresh breakdown row. A claim sliced by ≥2 breakdowns (e.g. `ig_roas=3.63` under
`publisher_platform,platform_position`) matched **many** IG cells → ambiguous → `could_not_audit`,
so its `lint-vault ⏳ re-verify` flag could never clear.

Fix: an optional **`select:`** field on the provenance tag names the exact slice, and
`resolve_fresh_metric` resolves against it (full-value, case-insensitive) **before** the token
heuristic. The token heuristic and every no-selector path are untouched.

### Changes

**`knowledge_provenance.py`**
- `_SELECT_RE = re.compile(r"\bselect:\s*(?P<select>[A-Za-z0-9_=,.]+)")` (value chars stop at
  whitespace / the `·` field separator, so the commas *inside* the selector survive).
- `EvidenceLine.metric_selector: dict[str, str] | None = None` (default on the dataclass; the one
  construction site in `_parse_evidence` now passes it). Parsing: comma-split → `k=v` dict;
  malformed (no `=`) / empty → `None` → token-heuristic fallback.
- `_lint_evidence`: a **warn** (`select_recommended`, never an error) when a metric line is sliced by
  ≥2 breakdowns and has no `select:`. Skips `is_audit_line` (our own ➖ bullets).

**`cli.py`**
- `_row_matches_selector(row, selector)` — True iff every selector key/value is in the row's
  `segment` dict, **full-value case-insensitive** (a missing key or a no-`segment` row → no match).
- `resolve_fresh_metric(..., selector=None)` — selector branch runs **first**: 0 matches →
  `(None,None,None)` (abstain); 1 match → `_row_value` (keeps the roas-or-derived path); several →
  `_aggregate_value` (the author's intentional coarser-slice blend). Docstring updated.
- `run_vault_audit` threads `selector=ev.metric_selector`.

**Data / docs**
- `knowledge/learnings.md:194` — the real `ig_roas=3.63` tag gained
  `· select: publisher_platform=instagram` (the platform-level blend across positions — **NOT** a
  `platform_position` cell; 3.63 is the IG blend, the per-cell numbers are Stories 4.33 / Reels 3.30
  / feed 3.25).
- `knowledge/README.md` — documents the optional `select:` field.

## Validate

- Full suite green: `.venv/bin/python -m pytest tests/test_meta_ads_analysis.py -q` → **343 passed**
  (13 new tests; one assertion added to `test_parse_learnings_extracts_structured_fields`).
- Real corpus clean: `.venv/bin/python -m meta_ads_analysis lint-vault --today 2026-06-26` →
  `13 entries · 0 error(s) · 0 warning(s)` (the migrated line no longer trips `select_recommended`;
  no other real line has ≥2 breakdowns without a selector).
- **Use `.venv/bin/python`** — bare `python`/`python3` lacks `duckdb` and fails at collection.

### Key test cases (all in tests/test_meta_ads_analysis.py)
- Parse: single-key / multi-key `select:` → dict; malformed (no `=`) → `None`; absent → `None`;
  a selector on one sibling bullet does not bleed onto the other.
- Resolve: subset blend (`publisher_platform=instagram` over two-dim rows → blended 3.63);
  single-cell pin (two-key selector → `_row_value`); zero-match → abstain; case-insensitive
  full-value (`Instagram` resolves, substring `insta` does **not**); missing key → no match;
  `selector=None` → token heuristic unchanged; account-level rows (no `segment`) → abstain.
- Lint: ≥2-breakdown metric w/o selector → exactly one `select_recommended` **warn**, zero errors;
  with selector OR single breakdown → no warn.
- End-to-end (`run_vault_audit`, fake provider): two-dim claim with selector resolves to the IG
  blend, drifts → contradicted, band 🟢→🟡, dated ➖ logged, **idempotent** on a second `--apply`
  (the ➖ bullet carries no `select:` and is `is_audit_line`-skipped); vanished IG segment → zero
  matches → `could_not_audit`, file byte-identical (safe-direction invariant holds).

## Known gaps / reviewer notes (treat tests as a floor)
- **Live re-verify is out-of-band.** All audit tests use fake rows. Whether the real IG blend
  actually lands near 3.63 against live Meta needs a real `audit-vault --account divine_designs` run;
  not exercised here.
- **`ig_roas=2.79` (learnings.md:185–186) left as-is** on purpose — it is single-breakdown
  (`publisher_platform`), the token heuristic resolves it, and leaving it proves backward-compat.
  It could optionally gain `select: publisher_platform=instagram` for consistency; not required.
- **Selector values can't contain whitespace** — `_SELECT_RE` stops at the first space (by design,
  so it doesn't swallow the rest of the tag). Fine for Meta breakdown values (`instagram`, `feed`,
  `stories`), but a hypothetical multi-word segment value couldn't be expressed. Worth a glance.
- **Several-match = blend, by design** — with an explicit selector, multiple matches is the author's
  coarser slice, not ambiguity; only the *no-selector* path keeps "several → abstain". Confirm this
  framing reads correctly and that the blend (`_aggregate_value`) is the right reducer (it
  re-derives ROAS from summed `purchase_value`/`spend`, abstaining if value is missing or spend 0).
