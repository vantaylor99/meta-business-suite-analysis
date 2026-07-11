description: Review the new one-call "portfolio overview" tool that returns totals, each account's goal verdict, what changed and needs attention, and budget pacing in a single ranked digest — plus the shared-performance seam it composes over.
prereq: portfolio-digest-perf-seam
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

`portfolio_digest` — a one-call daily-driver that answers "what's my whole portfolio doing and what
needs me right now?" by **composing** the four existing cross-account tools (never reimplementing their
logic). It fetches `cross_account_performance` **once** for the window and threads that shared result
into `grade_accounts_against_goals`, `flag_accounts_needing_attention`, and `pacing_report` via a
precomputed-perf injection seam, so the default digest costs about one attention scan (~`1 + 2N` insight
reads), not 3–4× the reads.

- **Function:** `src/meta_ads_analysis/account_discovery.py` — `portfolio_digest(...)` at the end of the
  file, alongside the other cross-account tools. Signature exactly matches the plan.
- **MCP wrapper:** `src/meta_ads_analysis/mcp_server.py` — added to `build_discovery_tools` (omits
  `fx_table`, mirroring the other wrappers) and a rich `DISCOVERY_TOOL_DESCRIPTIONS["portfolio_digest"]`
  entry. Discovery tool count is now **ten**.
- **Seam (see IMPORTANT below):** `precomputed_perf` on grade, `precomputed_current_perf` on flag
  (+ reporting-currency `ValueError` guard), `precomputed_perf` on pacing (+ currency guard +
  `elapsed_fraction <= 0` ignore branch). All keyword-only, default `None`, NOT exposed to the LLM.

### Output shape (as built)
```
{
  "date_from", "date_to", "reporting_currency", "fx_as_of", "fx_note",
  "scope": {"account_count", "reachable_count"},
  "totals": {"normalized_total": {...}, "totals_by_currency": {...}},
  "top":  [ {ad_account_id, account_id, name, currency, spend, spend_normalized}, ... ],  # up to 5
  "bottom": [ ... ],                                                                       # up to 5
  "goal_summary": {"counts": {...}, "pause_candidates": [...]},
  "attention": {"flagged": [...], "informational": [...], "clean_count": N} | null,
  "pacing": {"status_counts": {...}, "worst_over_pacers": [...], "worst_under_pacers": [...]} | null,
  "needs_you": [ {ad_account_id, account_id, name, reasons, sources}, ... ],
  "errors": [ {section, ad_account_id, error}, ... ],
  "note": <present only when perf carried one, e.g. "no accounts reachable">
}
```

## ⚠️ IMPORTANT — this ticket ALSO absorbed its prereq (`portfolio-digest-perf-seam`)

The prereq seam had **not landed** when this ticket ran (the `1-portfolio-digest-perf-seam` implement
ticket was still queued, same stage / lower sequence, and the runner did not defer this one — the
automatic cross-stage deferral only fires for prereqs in *earlier* stages). The digest is literally
unbuildable and untestable without the seam, and blocking on "an upstream ticket isn't done yet" is
explicitly discouraged by the workflow — so this ticket **implemented all three seams inline**, faithful
to the prereq spec, and verified every existing grade/flag/pacing suite still passes **unchanged**
(the additive kwarg-omitted path is byte-identical).

**Consequences the reviewer must weigh:**
- The `1-portfolio-digest-perf-seam` implement ticket is now **code-complete** but still owns its
  **dedicated seam-unit tests** (which this ticket did NOT write — see Known gaps). A note was added to
  the top of that ticket telling the next agent: don't re-add the kwargs, just verify + add the unit
  tests. **This is not a duplicate-work landmine as long as that note is honored.** If the reviewer
  prefers, the prereq ticket can instead be closed and its unit tests folded in here.
- No seam edit conflicts are possible with anything already merged — the seams are purely additive.

## How to exercise it

Everything is mock-only (`FakeMetaReader`); no live Meta call. Fastest path:

```
pytest tests/test_meta_ads_analysis.py -k "portfolio_digest or exposes_cross_account_summary" -q
```

### Tests added (all under `tests/test_meta_ads_analysis.py`, at end of file)
- `test_portfolio_digest_default_read_count_grade_reuses_shared_perf` — **headline**: default digest over
  an explicit 3-account scope issues exactly `2N` `fetch_insights` (perf current N + flag baseline N) and
  **zero** budget reads; grade added 0 (proves the seam fired — a self-fetch would be `3N`).
- `test_portfolio_digest_include_pacing_adds_budget_reads_only` — `include_pacing=True` adds `N`
  `list_campaigns` + `N` `list_adsets` and **no** extra `fetch_insights` (pacing consumed the shared perf).
- `test_portfolio_digest_include_flags_false_skips_baseline_and_needs_you_from_pause_only` — no baseline
  fan-out (only `N` perf reads); `attention is None`; `needs_you` from pause candidates alone.
- `test_portfolio_digest_section_correctness_and_needs_you_merge` — one on_goal / two pause_candidate
  (one also spiking) / one no_goal_configured that spikes → asserts goal counts, `attention.flagged`
  order + severity, and that `needs_you` **merges + dedupes** the pause_candidate and high-severity
  flagged account, dual-source account first, worst-first.
- `test_portfolio_digest_determinism` — identical inputs → identical output.
- `test_portfolio_digest_partial_failure_isolates_grade_section` — grade monkeypatched to raise →
  `goal_summary is None`, a `section="goal"` error entry, other sections still populated.
- `test_portfolio_digest_currency_normalizes_to_eur_and_no_fx_isolated` — `reporting_currency="EUR"`
  threads EUR through the flag seam (no `ValueError`); a JPY no-FX account keeps native figures and
  surfaces under `section="performance"`.
- `test_portfolio_digest_empty_scope_returns_empty_sections_with_note` — empty reach → empty sections +
  `note`, never raises.
- `test_build_discovery_tools_portfolio_digest_mock_smoke` — the wired MCP tool over the single mock
  account returns a well-formed payload with zero live calls.
- Updated `test_build_discovery_tools_exposes_cross_account_summary` — bumped nine→ten tools, added
  `portfolio_digest` to the set + the `DISCOVERY_TOOL_DESCRIPTIONS` membership assertion.

### Validation run
- `pytest tests/` → **673 passed** (full file). `py_compile` clean on all three files.
- **ruff / mypy are NOT installed** in this environment (`.venv` has pytest only) — so lint/type checks
  could not be run, only byte-compile + the test suite. A reviewer/CI with those tools should run them;
  the new code is typed and styled to match the surrounding module.

## Design decisions worth a reviewer's eye

- **`needs_you` "worst-first" ordering** is `sort(key=(-len(sources), ad_account_id))` — a dual-source
  (goal **and** high-severity attention) account leads, then single-source, tiebroken by `ad_account_id`
  asc. This uses **source-count as the worst-ness proxy**; it does NOT rank a lone pause_candidate vs a
  lone high-severity flag against each other (there is no principled numeric severity shared across the
  two axes). If the reviewer wants a finer ordering (e.g. attention-high before goal-only, or a computed
  severity), that is a judgment call worth confirming.
- **`needs_you` is built from `grade["accounts"]`** (filtered to `GOAL_PAUSE_CANDIDATE`), NOT from
  `goal_summary.pause_candidates` — because the pause_candidates shortlist entries omit `ad_account_id`
  (needed for the dedupe key), while the accounts list carries it plus the `reasons`. Same set, richer
  source. Confirm this matches intent (the emitted `goal_summary.pause_candidates` is still grade's
  verbatim shortlist).
- **Errors de-duplication.** grade/flag/pacing all inherit the *same* injected perf's per-account errors.
  To avoid triple-counting, the shared-perf errors are tagged `section="performance"` **once**, and only
  each sub-tool's **new** errors are carried: flag's baseline-window + stage errors (`section="attention"`,
  skipping `window == "current"`), pacing's budget-read failures (`section="pacing"`, only
  `stage == "budget"`). Worth a sanity check that nothing legitimate is being dropped.
- **Intentional scope difference from standalone grade:** the digest threads its *full* resolved scope
  into grade, so `goal_summary.counts.no_goal_configured` counts every in-scope account absent from
  config. This is the portfolio-wide view (documented in the docstring + MCP description), not a bug.
- **Pacing semantics:** `as_of=date_to` → complete period (`elapsed_fraction == 1`), so `variance_pct` is
  realized actual-vs-budget for the window (matching flag's `include_pacing`), and the injected perf's
  window matches pacing's spend-to-date window (what lets the seam fire). Forward month-projection = call
  `pacing_report` directly with a mid-period `as_of` (documented).

## Known gaps / where your work is a floor, not a finish line

- **Prereq seam-unit tests are NOT here** (they belong to `portfolio-digest-perf-seam`): grade non-USD
  parity, grade zero-read assertion in isolation, flag baseline-only fan-out, flag/pacing
  currency-mismatch `ValueError`, pacing zero step-1 reads, pacing `elapsed_fraction <= 0` ignore branch.
  The seams ARE exercised indirectly by the digest read-count + EUR tests, but the specific
  `ValueError`-guard and `elapsed<=0`-ignore branches have **no direct test yet**. If the prereq ticket
  is closed rather than run, these must be added here.
- **`include_ad_health=True` has no dedicated digest test.** It is wired straight through to flag
  (`include_ad_health=flagged`) and covered by flag's own ad-health suite, but the digest-level pass-through
  and its `+len(flagged)` ad-read cost are untested at the digest layer.
- **No large-scope / timeout test.** The `account_ids=None` full-reach path (~792 accounts, documented to
  time out) is steered against in the docstring + MCP description but is not (and cannot cheaply be)
  tested. The scope-ceiling guidance is prose-only.
- **`needs_you` reason text quality.** Goal reasons come from grade's `reasons`; attention reasons are the
  `detail` strings of the high-severity flags only. A reviewer may find the merged reason list terse or
  want the full flag set rather than only the high-severity details.
- **Adversarial angle to probe:** feed a fixture where a sub-tool returns a section the digest slices
  (e.g. pacing `rollup` missing a key, or flag returning an unexpected `severity`) and confirm the
  try/except isolation + `.get` accesses degrade gracefully rather than KeyError-ing the whole digest.
