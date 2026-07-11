description: Shipped and reviewed the one-call "portfolio overview" tool that returns totals, each account's goal verdict, what changed and needs attention, and budget pacing in a single ranked digest — reusing one shared performance read across all sections.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
----
## What shipped

`portfolio_digest` — a one-call daily-driver answering "what's my whole portfolio doing and what needs
me right now?" by **composing** the four existing cross-account tools (never reimplementing their logic).
It fetches `cross_account_performance` **once** and threads that shared result into
`grade_accounts_against_goals`, `flag_accounts_needing_attention`, and `pacing_report` via their
precomputed-perf seams, so the default digest costs ~`1 + 2N` insight reads, not 3–4×.

- **Function:** `src/meta_ads_analysis/account_discovery.py` — `portfolio_digest(...)` at end of file.
- **MCP wrapper:** `src/meta_ads_analysis/mcp_server.py` — in `build_discovery_tools` (omits `fx_table`)
  + `DISCOVERY_TOOL_DESCRIPTIONS["portfolio_digest"]`. Discovery tool count is now **ten**.
- **Seam:** the precomputed-perf injection points on grade/flag/pacing landed inline via this ticket's
  implement commit (`7dc29ce`) and were independently completed + unit-tested by the prereq ticket
  `portfolio-digest-perf-seam` (`bcdb161` implement, `238a3e0` review — see `complete/1-portfolio-digest-perf-seam.md`).

Output shape and design decisions are documented in the function docstring and the MCP description; see
the implement commit `7dc29ce` for the full rationale.

## Review findings

**Method:** read the implement diff (`git show 7dc29ce`) with fresh eyes before the handoff summary,
scrutinized the composite for correctness / isolation / DRY / determinism / resource use, traced the
scope-resolution and currency-guard seams, ran the full suite, and checked every doc the change touches
(and the ones it *should* have — README, META_API_SETUP.md).

### Correctness (checked — no defects)
- **Empty-list scope safety.** `resolve_scope` (`account_discovery.py:212`) treats **only**
  `account_ids is None` as "whole reach"; an explicit `[]` resolves to an empty scope
  (`requested_all=False`). The digest derives `scope_ids` from the *successfully-read* `perf["accounts"]`
  and passes it to flag/pacing — so even when every explicit account fails its read (`scope_ids == []`),
  flag/pacing get `[]`, not `None`, and can **never** balloon into a whole-fleet fan-out. Verified against
  the resolver source, not just the empty-scope test.
- **Currency threading.** `reporting = perf["reporting_currency"]` (already upper-cased) is threaded into
  flag/pacing, so their defensive currency-mismatch guards never fire spuriously; grade reads only native
  metrics so it is FX-independent by construction. Confirmed by the EUR + no-FX test.
- **Failure isolation.** grade / flag / pacing sections are each `try/except`-wrapped: an unexpected
  whole-call failure (incl. a `KeyError` from a malformed sub-tool payload) sets that section to `None`,
  records a tagged `{section, error}` entry, and the digest still returns. The `needs_you` builder reads
  `grade["accounts"]` / `attention["flagged"]` only *after* those sections succeeded, and every field
  access there uses `.get(...)` with defaults — so a missing `verdict`/`flags`/`severity` degrades, never
  `KeyError`s. This closes the handoff's flagged "adversarial angle."
- **Error de-duplication.** The shared perf's per-account errors are tagged `section="performance"` once;
  flag contributes only non-`current`-window errors (`section="attention"`), pacing only `stage="budget"`
  errors (`section="pacing"`). No legitimate error is dropped and none is triple-counted. Confirmed.

### Seam / LLM-exposure safety (checked — holds, via the prereq)
The three precomputed-perf kwargs are keyword-only, default `None`, and appear **only** in
`account_discovery.py` (the MCP wrappers use explicit named params and omit them) — verified in the
prereq's completed review. The prereq also added **all** the seam-branch tests the digest handoff worried
were missing: flag/pacing currency-mismatch `ValueError`, the pacing `elapsed_fraction <= 0` ignore
branch (`test_pacing_precomputed_perf_ignored_when_not_started`), flag baseline-only fan-out, and the
error-window tagging invariant. The digest handoff's "known gaps — prereq seam-unit tests are NOT here"
was written **before** the prereq landed; it is now **stale** — those tests exist and pass.

### Tests (checked — meaningful; one gap closed inline)
The 9 implement-added digest tests are genuine (read-count tests assert exact `fetch_insights`/budget
call counts; the section-correctness test exercises every section + the `needs_you` merge/dedupe; partial
failure, determinism, currency, empty scope all covered).
- **Minor (fixed inline):** the digest's `include_ad_health=True` pass-through — the one wiring in the
  digest's own signature that had **no digest-level test** (the handoff flagged it) — is now covered by
  `test_portfolio_digest_include_ad_health_scans_flagged_only_and_feeds_needs_you`: asserts only the
  flagged account is ad-scanned (`+1 iter_paginated`, clean account never enumerated), a `DISAPPROVED` ad
  attaches a high-severity `ads_disapproved` flag and promotes the account, and its detail flows into
  `needs_you`.

### Docs (checked — were stale; fixed inline)
The change added a **tenth** discovery tool but left the docs describing nine. Fixed:
- `README.md` — added the `portfolio_digest` "tenth discovery tool" paragraph alongside the other nine.
- `docs/META_API_SETUP.md` — "the nine discovery tools" → "the ten discovery tools" (+ added
  `portfolio_digest` to the enumerated list) and added a full tool-description paragraph mirroring the
  other tools' depth (composition, read cost, sections, scope ceiling, pacing semantics, isolation).
- `AGENTS.md` and the other `docs/*.md` enumerate no discovery-tool list — nothing to update there
  (checked explicitly, not assumed).

### Judgment-call design points (checked — intentional, NOT bugs; no ticket warranted)
- **`needs_you` "worst-first" = source-count desc, `ad_account_id` asc.** Uses source-count as the
  worst-ness proxy; does not rank a lone pause-candidate vs a lone high-severity flag numerically. There is
  no principled shared severity across the two axes, so this is a defensible choice, documented in the
  docstring.
- **`top`/`bottom` overlap for a <5-account fleet** (each returns all accounts, one asc one desc). Harmless
  given the "up to `_DIGEST_TOP_N`" contract.
- **grade's `as_of` defaults to today** (not `date_to`) inside the digest — consistent with standalone
  `grade_accounts_against_goals` (as_of governs only the goal-config grace window, not the perf window).
- **Portfolio-wide `no_goal_configured` scope** (threads the full resolved scope into grade) — intentional
  and documented; differs from standalone grade's configured-only default by design.

### Lint / type checks (not runnable here — flagged, not a gap)
`ruff` / `mypy` are **not installed** (`.venv` has pytest only) and no lint config exists in
`pyproject.toml` / AGENTS.md — the same situation the prereq review flagged. `py_compile` is clean; the
new test code matches surrounding style. A CI/human with those tools should run them.

### Validation
- `.venv/bin/python -m pytest tests/` → **683 passed** (682 pre-existing + the 1 test added this pass).
  No regressions.
- `pytest -k "portfolio_digest"` → 10 passed; `pytest -k "precomputed or errors_tagged_current"` →
  9 passed (seam guards green).

## Disposition
No major findings; **no new fix/plan/backlog tickets spawned.** Two minor items fixed inline (stale docs;
the missing `include_ad_health` digest-level test). The digest is a faithful, deterministic, well-isolated
composite over the four cross-account tools, and the prereq seam it rides is independently verified and
LLM-safe.
