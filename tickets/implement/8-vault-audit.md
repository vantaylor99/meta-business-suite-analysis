description: Add a command that re-checks the knowledge vault against fresh live data — when a stored number (like "Engaged Audience holds 3.7 ROAS") no longer matches reality, it surfaces the conflict loudly, lowers that fact's confidence, and logs the disagreement, but never silently overwrites or deletes it. A human still decides what to remove.
prereq: vault-provenance-format
files: src/meta_ads_analysis/knowledge_provenance.py, src/meta_ads_analysis/cli.py, src/meta_ads_analysis/config.py, pyproject.toml, knowledge/README.md, tests/test_meta_ads_analysis.py
difficulty: hard
----
## Why

`vault-provenance-format` made each data-backed learning carry an auditable `metric:` assertion and
a `verify: account_metrics …` command. This ticket closes the loop: a **`audit-vault`** command
re-runs those queries against fresh live metrics, diffs the result against the stored value, and —
when reality has drifted — **surfaces the contradiction loudly and lowers the fact's confidence band
with a dated `➖` evidence line**, never silently keeping the stale belief and never auto-deleting it
(a human decides deletion). It also doubles as the *re-verification* mechanism for the staleness
flags `lint-vault` raises: a fact the audit re-runs and confirms gets its `Verified:` date refreshed.

This is the contradiction-surfacing half of the parent `knowledge-base-provenance` plan.

## Shared vocabulary

Reuse, do not reinvent: `confidence.Band` (🟢/🟡/🔴/⚪) and its ordering for the −1-level decrement;
`confidence.EvidenceTier`; `confidence.data_strength` to decide whether the **fresh** sample is even
strong enough to count as a contradiction; the `LearningEntry`/`EvidenceLine` parser from
`knowledge_provenance.py`. Fresh metrics come **in-process** from `fetch_entity_metrics` /
`fetch_breakdown_metrics` (`cli.py:861`) — do NOT shell out and scrape stdout.

## `audit-vault --account <slug>` behavior

Read-only against Meta; **report-only by default**, mutates local markdown only with `--apply`.

1. **Select auditable claims.** Parse `learnings.md`; keep evidence lines that carry both a
   `metric:` assertion and a `verify:` query scoped to `--account <slug>`. Skip `evergreen` entries
   and any line with no `metric:` — only fast-rotting, data-backed account facts are audited.
2. **Re-pull fresh.** For each claim, re-run its metric over a **fresh trailing window of the same
   length** as the stored window, ending `--as-of` (default today) — "current data," not the
   original window. Default to a trailing 30-day window if the stored window length can't be
   determined. Resolve the named metric (`blended_roas`, `roas`, etc.) from the returned rows for the
   claim's `entity_id`/level.
3. **Decide drift.** A claim is **contradicted** when the fresh value clears the data floor (via
   `confidence.data_strength` — see below) AND either:
   - relative change `|fresh − stored| / stored ≥ KNOWLEDGE_DRIFT_PCT` (0.25), OR
   - the fresh value **crosses a policy threshold** the stored value sat on the other side of —
     `target_roas` (3.0) or `pause_roas_floor` (1.5) from the account config (e.g. stored 3.74 above
     target, fresh 2.10 below it).
   Otherwise it's **confirmed** (fresh ≈ stored).
4. **Report (always).** Print every audited claim: stored vs fresh vs window vs verdict, with
   contradictions called out **loudly** (⚠️). Summize counts (confirmed / contradicted / could-not-audit).
5. **`--apply` (writes local markdown only):**
   - **Contradicted:** append a dated `➖` line to that entry's evidence log —
     `` - ➖ <as-of> — vault audit: <metric> now <fresh> vs stored <stored> over <window> `verify: account_metrics …` _(src: direct_observation · acct: <slug> · metric: <name>=<fresh>)_ `` — and **lower the band one level** (🟢→🟡→🔴) per the README rubric. If the fresh value *directly refutes* the claim (crosses to the opposite side of the threshold the claim asserts), drop to 🔴 Low and append `(contested)` to the claim per the rubric. **Never edit the claim text; never delete the entry.** Refresh `Verified:` to `<as-of>` (it WAS just checked).
   - **Confirmed:** refresh `Verified:` to `<as-of>` (this is how a `lint-vault ⏳ re-verify` flag
     clears); optionally append a `➕` confirming line. Do **not** raise the band automatically
     (re-confirming the same window is not independent corroboration).
   - **Surgical edits only:** locate the entry by `LearningEntry.lineno`, change the single band
     emoji + `Verified:` line and insert one bullet. Do **not** rewrite the whole file (a human or
     another tess ticket may have in-flight edits).
6. Register console script + `python -m meta_ads_analysis audit-vault` subcommand like the others.

## Config

Add `KNOWLEDGE_DRIFT_PCT = 0.25` to `config.py`. Reuse the per-account `target_roas` /
`pause_roas_floor` (already in `config/meta_ads_accounts.json` / `config.py`) for boundary-cross
detection — do not hardcode 3.0/1.5.

## TODO

- [ ] Add `KNOWLEDGE_DRIFT_PCT` to `config.py`.
- [ ] Implement the audit engine in `knowledge_provenance.py` (pure diff/verdict logic separated
      from the Meta pull so it's unit-testable with a fake metrics provider): a function that takes
      (stored claim, fresh value, fresh sample, account thresholds) → verdict
      {confirmed | contradicted | refuted | insufficient_fresh_data | could_not_audit}.
- [ ] Implement the markdown mutation (append `➖`/`➕`, lower band, refresh `Verified:`) as a
      surgical, idempotent edit keyed off `lineno`.
- [ ] Add `audit_vault_main()` to `cli.py` (the only place that touches Meta); register entry points.
- [ ] Add `audit-vault` to the README workflow + note it as the re-verification mechanism.
- [ ] Unit tests (below) using a fake metrics provider — **no live Meta in tests**.
- [ ] `python -m pytest tests/ -q 2>&1 | tee /tmp/vault_audit.log` green.

## Key tests (TDD — inject a fake metrics function; never hit Meta)

- Stored 3.74, fresh 3.70 → **confirmed**; `--apply` refreshes `Verified:` only, band unchanged.
- Stored 3.74, fresh 2.10 (>25% drop AND crosses target 3.0) → **contradicted/refuted**; `--apply`
  lowers 🟢→🟡 (or to 🔴 + `(contested)` on refute), appends a dated `➖` with the fresh metric and
  `verify:` command, updates `Verified:`, and **does not** alter the claim text or delete the entry.
- Fresh sample below the floor (e.g. 2 purchases / $30 over a noisy window) → **insufficient fresh
  data**; verdict is abstain, **no** band change, no `➖` (a noisy window must not refute a real fact).
- Stored entity_id missing from the fresh rows, or the named metric absent → **could_not_audit**;
  reported, never silently counted as confirmed (AGENTS.md "don't collapse missing into zeros").
- **Idempotency:** running `--apply` twice on the same `--as-of` appends only ONE `➖` for the same
  drift (detect an existing same-date audit line before inserting).
- Report-only (no `--apply`) makes **zero** file changes (assert file bytes unchanged).
- `evergreen` entries and lines with no `metric:` are never selected for audit.
- The band-decrement step uses `confidence.Band` ordering (not a local emoji list) so it stays one
  vocabulary with the live engine.

## Edge cases & interactions

- **Contradiction lowers + logs; never deletes.** Hard invariant from the parent ticket: a human
  decides deletion. The audit only appends evidence and decrements the band.
- **A contradiction must itself clear the data floor.** Use `confidence.data_strength` on the fresh
  sample; a below-floor fresh pull is `insufficient_fresh_data`, not a refutation — protects against
  killing a true fact on a quiet week. Second-decimal ROAS noise is absorbed by the 25% threshold.
- **Surgical, idempotent, concurrency-safe edits.** Edit only the matched entry's band/`Verified:`
  line + insert one bullet, keyed off `lineno`; re-parse-and-locate rather than byte-offset
  rewriting, so concurrent human/tess edits elsewhere in the file aren't clobbered. Do not run any
  `git restore`/`reset`/`stash`.
- **Window semantics.** Audit re-pulls a *fresh trailing* window of the stored length ending
  `--as-of` (current reality), not the historical window — document this so a reviewer doesn't read
  a date mismatch as a bug.
- **Derived ROAS.** ROAS here is value/spend (per learnings); if value is missing for the fresh
  window, treat as could_not_audit, not 0 ROAS.
- **Read-only w.r.t. Meta writes.** The audit only *reads* metrics and *writes local markdown*; it
  must never propose or execute a Meta account change.
- **One vocabulary.** Bands, emoji, and tiers must match `confidence.py` + `knowledge/README.md` +
  `knowledge_provenance.py`; no second scale, no model-typed band.
