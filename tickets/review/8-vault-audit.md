description: A new command re-checks the knowledge vault's stored numbers against fresh live data and, when a number has drifted from reality, loudly flags the conflict and lowers that fact's confidence — but never silently overwrites or deletes it. A human still decides what to remove.
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/__main__.py, src/meta_ads_analysis/config.py, pyproject.toml, knowledge/README.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## What shipped

`audit-vault` closes the loop opened by `vault-provenance-format`: it re-runs each data-backed
`metric:` claim in `knowledge/learnings.md` against **fresh** live metrics, diffs fresh vs stored,
and — when reality has drifted — surfaces the contradiction loudly, appends a dated `➖` evidence
line, and lowers the fact's confidence band one level. It **never edits claim text and never deletes
an entry** (a human decides deletion), and it is **read-only against Meta** + **report-only unless
`--apply`**.

The prior (interrupted) run had already committed the pure engine in `knowledge_provenance.py`
(`select_auditable` → `classify_drift`/`audit_claim` → `plan_edits` → `apply_entry_edits` →
`render_audit_report`, plus `FreshSample`/`AuditOutcome`/`EntryEdit`) and `KNOWLEDGE_DRIFT_PCT` in
`config.py`. **This run completed the remaining half:**

- **`cli.py`** — the only Meta-touching code:
  - `resolve_fresh_metric(rows, level, breakdowns, metric_name)` — pulls the claim's value out of
    fresh rows. Account-aggregate (no breakdown, `--level account`) is resolved deterministically;
    breakdown/entity-scoped metrics (`ig_roas`, `engaged_adset_roas`) are matched by **token overlap**
    between the metric name and each row's segment value / entity name, with a small alias map
    (`ig→instagram`, `fb→facebook`, `an→audience_network`). Zero or multiple matches → unresolved →
    `could_not_audit` (never a guessed value).
  - `run_vault_audit(...)` — pure orchestration given an injected `fetch_metrics`: select → re-pull a
    **fresh trailing window of the stored length ending `--as-of`** → classify → render / (optionally)
    plan+apply edits. Returns `(report, new_text_or_None, counts)`.
  - `audit_vault_main()` — builds the real client, reads/writes `learnings.md`, prints the report.
  - Small cosmetic fix in `knowledge_provenance.classify_drift`: the relative-drift factor now prints
    `.1%` (was `.0%`) so a borderline 24.7% no longer reads as the confusing "25% drift < 25%".
- **Wiring** — `audit_vault` console script in `pyproject.toml`; `audit-vault`/`audit` subcommand +
  usage strings in `__main__.py`.
- **README** — `audit-vault` is now documented as **the re-verification mechanism** (new
  "Re-verifying with `audit-vault`" subsection); all "forthcoming" framing removed.
- **Tests** — 19 new tests, all using a **fake metrics provider** (no live Meta).

## How to validate

```
.venv/bin/python -m pytest tests/ -q          # 178 passed (159 prior + 19 new)
PYTHONPATH=src .venv/bin/python -m meta_ads_analysis audit-vault -h
```

The 19 new tests live in `tests/test_meta_ads_analysis.py` under the "audit-vault — drift re-check"
banner. Coverage map to the ticket's "Key tests":

- **confirmed** (3.74 vs 3.70) → `--apply` refreshes `**Verified:**` only, band unchanged, no `➖`.
- **refuted** (3.74 vs 2.10, >25% AND crosses `target_roas` 3.0) → `--apply` sets `🔴 Low (contested)`,
  appends a dated `➖` with the fresh metric + a fresh `verify:` command, refreshes `Verified:`, leaves
  the claim text and entry intact. Also covers **contradicted** (10.0→6.0, >25% but no threshold
  cross → one level down, no `(contested)`).
- **insufficient_fresh_data** (2 purchases / $30) → abstain: no band change, no `➖`, `Verified:`
  unmoved (asserts file unchanged).
- **could_not_audit** (entity vanished / fresh rows empty; and value-missing-not-zero in
  `resolve_fresh_metric`) → reported, never counted as confirmed, no edits.
- **idempotency** — `--apply` twice on the same `--as-of` yields byte-identical text and exactly one
  `➖`.
- **report-only** — `new_text is None`; CLI test asserts the file is byte-for-byte unchanged.
- **selection** — `evergreen` entries, no-`metric:` lines, and other-account lines are never selected.
- **band decrement** uses `confidence.Band` ordering (pinned via `BAND_PRESENTATION`), not a local
  emoji list.
- **resolution** — account aggregate; breakdown segment match (`ig_roas`→instagram); ambiguous→None.

**Manual end-to-end (run during implement, not in the suite):** `run_vault_audit` against the *real*
`knowledge/learnings.md` with a fake provider selected the 3 real metric claims, resolved the
breakdown (`ig_roas`→instagram) and adset (`engaged_adset_roas`→"Engaged - 365d") cases, and on
`--apply` produced a surgically-correct, **idempotent** mutation whose appended `➖` lines **still
lint clean** (they carry `src:`+`metric:`+`verify:`). The real file was **not** modified (in-memory
only). A reviewer can re-run the snippet from the implement log or adapt the CLI test.

## Known gaps / things to scrutinize (this is a starting point, not a finish line)

1. **Metric resolution is a name-matching heuristic for non-account-level claims.** It abstains
   (`could_not_audit`) rather than guess when a metric name's tokens don't uniquely match one fresh
   row (e.g. an abbreviation not in the alias map, or a renamed ad set). Verify the failure
   *direction* is right (abstain, never a fabricated confirm). A cleaner long-term fix — out of scope
   here — is to capture an explicit segment/entity selector in the provenance tag so resolution is
   exact instead of name-matched; worth a backlog ticket if the heuristic proves brittle.
2. **No live Meta anywhere in tests.** The real Graph pull (`client_from_env` →
   `fetch_entity_metrics`/`fetch_breakdown_metrics`) is only reached via monkeypatched CLI tests. The
   fetch functions themselves are the same ones the existing `metrics` command uses (covered
   elsewhere), but the audit's *specific* wiring to them has not run against the live API.
3. **`could_not_audit` does not refresh `Verified:`** (by design — nothing was actually confirmed).
   Consequence: a `lint-vault ⏳ re-verify` flag on a claim whose segment can't be auto-resolved won't
   clear via `audit-vault`; it needs manual re-verification. Confirm this is acceptable.
4. **Window default.** A `verify:` command lacking `--date-from`/`--date-to` falls back to a 30-day
   trailing window. All current stored claims carry explicit windows, so this is only a fallback.
5. **File-level concurrency.** `--apply` re-parses and edits bottom-up, only touching matched entries
   and preserving bytes when there are no edits — but it rewrites the whole file (last-writer-wins).
   Concurrent edits to *other* entries between read and write would be lost. Documented; acceptable
   for a human-run command, but flag if you disagree.
6. **`target_roas` source.** `audit_vault_main` reads `target_roas` (falling back to
   `scale_roas_floor`) and `pause_roas_floor` from the account `action_policy`. `pollen_sense` has no
   `target_roas`/`pause_roas_floor` (its policy is subscription-based), so for that account threshold
   crossings can't fire and drift relies on the 25% magnitude test alone — confirm that's intended.

## Invariants the reviewer should treat as load-bearing

- Contradiction **lowers + logs, never deletes**; claim text is never rewritten.
- A contradiction must clear the `confidence.data_strength` floor — a noisy fresh week abstains.
- Bands/emoji/tiers are ONE vocabulary with `confidence.py` + `knowledge/README.md` (pinned by tests).
- The audit only *reads* Meta metrics and *writes local markdown* — it never proposes/executes a Meta
  account change.
