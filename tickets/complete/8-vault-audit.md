description: A new command re-checks the knowledge vault's stored numbers against fresh live data and, when a number has drifted from reality, loudly flags the conflict and lowers that fact's confidence — but never silently overwrites or deletes it. A human still decides what to remove. Reviewed and shipped.
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/__main__.py, src/meta_ads_analysis/config.py, pyproject.toml, knowledge/README.md, tests/test_meta_ads_analysis.py
----
## What shipped

`audit-vault` closes the loop opened by `vault-provenance-format`: it re-runs each data-backed
`metric:` claim in `knowledge/learnings.md` against **fresh** live metrics, diffs fresh vs stored,
and — when reality has drifted — surfaces the contradiction loudly (⚠️), appends a dated `➖` evidence
line, and lowers the fact's confidence band one level. It **never edits claim text and never deletes
an entry** (a human decides deletion), is **read-only against Meta**, and is **report-only unless
`--apply`**.

- **`knowledge_provenance.py`** (pure half): `select_auditable` → `classify_drift` / `audit_claim` →
  `plan_edits` → `apply_entry_edits` → `render_audit_report`, with `FreshSample` / `AuditOutcome` /
  `EntryEdit`. Drift verdicts decrement `confidence.Band` (not a local emoji ladder); the
  significance floor that protects a true fact from a noisy week is `confidence.data_strength`. The
  relative-drift factor prints `.1%`.
- **`cli.py`** (the only Meta-touching half): `resolve_fresh_metric` pulls the claim's value out of
  fresh rows — account-aggregate deterministically; breakdown/entity-scoped metrics by token overlap
  with a small alias map (`ig→instagram`, …), abstaining (`could_not_audit`) on zero/multiple matches
  rather than guessing. `run_vault_audit` is pure given an injected `fetch_metrics`; `audit_vault_main`
  builds the real client, reads/writes `learnings.md`, prints the report.
- **Wiring**: `audit_vault` console script (`pyproject.toml`); `audit-vault`/`audit` subcommand +
  usage strings (`__main__.py`). `KNOWLEDGE_DRIFT_PCT` (25%) lives in `config.py`.
- **README**: new "Re-verifying with `audit-vault`" subsection; "forthcoming" framing removed.

## How to validate

```
.venv/bin/python -m pytest tests/ -q          # 181 passed
PYTHONPATH=src .venv/bin/python -m meta_ads_analysis audit-vault -h
```

## Review findings

**Verdict: shipped.** The implementation is correct, the invariants hold, and the suite is green
(181 passed; 178 from implement + 3 added in this pass). One real coverage limitation was found and
filed to backlog (not a correctness bug). No claim text is ever rewritten and no entry is ever
deleted — verified by reading the mutation path end-to-end.

### What was checked

- **Read the implement diff first** (commit `541b9f3`) across all six touched files, then the pure
  engine in `knowledge_provenance.py` (`classify_drift`/`audit_claim`/`plan_edits`/`apply_entry_edits`)
  and the parser it relies on (`parse_learnings` / `_parse_evidence` / the regex set).
- **Wiring**: confirmed `fetch_entity_metrics`/`fetch_breakdown_metrics`/`resolve_ad_account_id`/
  `resolve_account` are imported and their signatures + row shapes (`segment` dict at breakdown level,
  `name` at entity level) match what `resolve_fresh_metric` consumes. `resolve_account(...).action_policy`
  is the same pattern used elsewhere in `cli.py`.
- **Window math**: `_window_length_days` (inclusive span) → `resolve_date_window(as_of, lookback_days)`
  re-pulls a fresh trailing window of the *stored length ending `--as-of`* — verified against the real
  30/31/121-day stored windows.
- **End-to-end against the real `knowledge/learnings.md`** (in-memory, fake provider, file untouched):
  selected the 3 real metric claims, resolved the breakdown (`ig_roas`→instagram) and adset
  (`engaged_adset_roas`→"Engaged - 365d") cases, produced surgically-correct mutations, and the
  appended `➖` lines **lint clean** (0 lint errors). Confirmed **idempotency on a re-run** including
  the real divine_designs Instagram entry that carries **two `ig_roas` claims in one entry** — both
  `➖` lines are written once and a second `--apply` is byte-identical.
- **Edge/error paths** traced: idempotency `already`-key dedup, band floor at 🔴, `(contested)` double-
  apply guard, abstain verdicts never moving the band or `Verified:`, value-missing-not-zero,
  ambiguous-segment abstain.
- **Docs**: `knowledge/README.md` re-verification subsection read line-by-line against the code — band
  ladder, `data_strength` floor, confirmed-refreshes-`Verified:`-only, refute→🔴 `(contested)`,
  read-only/report-only — all accurate. (AGENTS.md mentions neither `lint-vault` nor `audit-vault`;
  that predates this ticket and was out of scope — noted, not changed.)
- **Lint/tests**: no `ruff`/`mypy`/`black` configured in this repo (the only "lint" is the in-tree
  `lint-vault` content checker, which passes on the audited output). `pytest` is the gate: green.

### Minor — fixed inline (added 3 tests; no production code changed)

The implementer's tests were a solid starting point but missed three cases now covered in
`tests/test_meta_ads_analysis.py`:

- `test_audit_contradicted_lowers_band_one_level_in_text` — the **contradicted** (magnitude, no
  threshold cross) path's *text* mutation (🟢 High → 🟡 Medium, no `(contested)`) — previously only the
  `classify_drift` verdict was asserted, never the applied band move.
- `test_resolve_fresh_metric_matches_entity_name_at_adset_level` — entity-**name** resolution at
  `--level adset` (`engaged_adset_roas`→"Engaged - 365d") — previously only account-aggregate and
  breakdown-segment resolution were tested.
- `test_audit_logs_each_drifted_metric_in_a_multi_metric_entry_and_is_idempotent` — **two drifted
  metrics in one entry** (mirrors the real Instagram entry), asserting both `➖` are logged and a
  re-run is byte-identical. This is the highest-value addition: the real vault has this shape and it
  exercises the `plan_edits` group-by-entry + idempotency interaction the original tests never did.

### Major — filed to backlog (`vault-audit-segment-selector`)

`resolve_fresh_metric`'s name-matching heuristic **cannot resolve a multi-dimension breakdown claim**:
the real stored `ig_roas=3.63` (`--breakdown publisher_platform,platform_position`, `learnings.md:193`)
matches *many* `instagram × position` rows → ambiguous → `could_not_audit`. So that claim is
permanently un-auditable and its `⏳ re-verify` flag can't clear automatically. This is a **coverage
limitation, not a correctness bug** — abstaining is the safe direction (it never fabricates a confirm
or a false refutation), which is why it's backlog rather than a blocker. The fix is an explicit
segment/entity selector in the provenance tag so resolution is exact; spec'd in the backlog ticket.

### Considered and accepted (no action — documented design judgments)

- **Sub-entity metrics judged against account-level thresholds.** `engaged_adset_roas` / `ig_roas`
  cross-check against the account `target_roas`/`pause_roas_floor`, framed as a "decision flip". For a
  single ad set / platform segment this is a slightly loose use of "flip", but it only *lowers
  confidence + logs* (never deletes), and the 25% magnitude test backstops it. Acceptable.
- **`pollen_sense` has no `target_roas`/`pause_roas_floor`** (subscription policy) → threshold crosses
  can't fire there; drift relies on the 25% magnitude test alone. Intended.
- **One failed live pull aborts the whole audit** (no per-claim try/except in `run_vault_audit`).
  Acceptable for a human-run command that should fail loudly on an API error; a per-claim
  `could_not_audit` fallback would be a nicety, not a requirement.
- **`--apply` last-writer-wins** on the whole file (re-parses + edits bottom-up, byte-preserving when
  no edits). Concurrent edits to *other* entries between read and write would be lost — acceptable for
  a human-run command.
- **Repeated audits on new `--as-of` dates progressively lower a persistently-drifted claim** (one
  level per run, to the 🔴 floor) and accumulate one `➖` per date. By design (sustained drift = lower
  confidence + an audit trail).

### Invariants confirmed load-bearing and intact

- Contradiction **lowers + logs, never deletes**; claim text is never rewritten.
- A contradiction must clear the `data_strength` floor — a noisy fresh week abstains.
- Bands/emoji/tiers are ONE vocabulary with `confidence.py` + `knowledge/README.md` (test-pinned).
- The audit only *reads* Meta metrics and *writes local markdown* — never a Meta account change.
