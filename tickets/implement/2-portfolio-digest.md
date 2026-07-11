description: Add a single "give me my portfolio overview" tool that returns one ranked digest — totals, each account's goal verdict, what changed and needs attention, and budget pacing — in one call, instead of making four separate calls and stitching them together by hand.
prereq: portfolio-digest-perf-seam
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What this delivers

A one-call daily-driver `portfolio_digest` over a scope + window that returns a single ranked digest by
**composing existing tools** (never reimplementing their logic). It fetches `cross_account_performance`
**once** for the window and threads that shared result into `grade_accounts_against_goals`,
`flag_accounts_needing_attention`, and `pacing_report` via the injection seam landed in the prereq —
so the digest costs roughly the same as one flag call by default, not 3–4× the reads.

New function lives in `src/meta_ads_analysis/account_discovery.py` alongside the other cross-account
tools; the MCP wrapper is added to `build_discovery_tools` + `DISCOVERY_TOOL_DESCRIPTIONS` in
`src/meta_ads_analysis/mcp_server.py`.

## Interface

```python
def portfolio_digest(
    reader: "MetaReaderProvider",
    *,
    date_from: str,
    date_to: str,
    account_ids: list[str] | None = None,
    reporting_currency: str = "USD",
    include_flags: bool = True,      # "what changed" — costs +N (baseline) reads
    include_pacing: bool = False,    # heaviest: +3N budget reads; off by default
    include_ad_health: bool = False, # nested into flag; off by default
    fx_table: FxTable | None = None, # internal/test seam, NOT exposed to the LLM
) -> dict[str, Any]:
```

MCP wrapper signature (exposed to the LLM — omit `fx_table`, matching the existing discovery wrappers):
`portfolio_digest(date_from, date_to, account_ids=None, reporting_currency="USD", include_flags=True, include_pacing=False, include_ad_health=False)`.

## Composition algorithm (one shared read)

1. **Shared perf — the single fan-out.** Load `fx_table` once (or accept the injected one). Call
   `cross_account_performance(reader, date_from=date_from, date_to=date_to, account_ids=account_ids,
   reporting_currency=reporting_currency, fx_table=table)`. This resolves scope once; its
   `perf["accounts"]` define the working scope for every downstream section. A whole-call `ValueError`
   here (bad `reporting_currency`) propagates — nothing is normalizable, so the whole digest fails
   (inherits the prereq contract). This is the ONLY account-level insight fan-out in the default path.

2. **`totals`** — from `perf["normalized_total"]` (portfolio figures in `reporting_currency`) plus
   `perf["totals_by_currency"]` (per-currency native subtotals, since money never sums across
   currencies). Zero extra reads.

3. **`top` / `bottom`** — derive directly from `perf["accounts"]` by sorting on `spend_normalized`
   (fallback native `spend` for a no-FX account), desc for `top`, asc for `bottom`, capped at a small N
   (e.g. 5). Zero extra reads. Do NOT call `rank_accounts` (it re-fetches); a plain sort over the perf
   rows we already hold is the composition-friendly choice. Each entry carries id/name/currency/spend
   (+ normalized) so it is independently readable.

4. **`goal_summary`** — `grade_accounts_against_goals(reader, date_from=date_from, date_to=date_to,
   precomputed_perf=perf)`. Returns `{counts, pause_candidates, accounts}`. Emit `counts` +
   `pause_candidates` (+ optionally a compact per-account verdict list). Zero extra reads.
   **Document the intentional scope difference:** because the digest threads its *full* resolved scope
   into grade, `goal_summary.counts` will include a `no_goal_configured` tally for every in-scope
   account absent from `config/meta_ads_accounts.json` — this is the portfolio-wide view, NOT a bug,
   and differs from standalone `grade_accounts_against_goals` (whose default scope is configured
   accounts only).

5. **`attention`** (only when `include_flags`) — `flag_accounts_needing_attention(reader,
   current_from=date_from, current_to=date_to, account_ids=<perf scope ids>,
   reporting_currency=reporting_currency, fx_table=table, precomputed_current_perf=perf,
   include_pacing=False, include_ad_health=include_ad_health)`. Baseline auto-derives via
   `prior_window`. Cost: +N insight reads (baseline fan-out) — plus `len(flagged)` ad reads when
   `include_ad_health`. Emit `{flagged, informational, clean_count}` (or a trimmed view). When
   `include_flags=False`, set `attention` to `None` (or omit) and note it in the payload.
   **Pass the resolved scope explicitly** (`account_ids=[r["ad_account_id"] for r in perf["accounts"]]`)
   so flag's baseline fan-out covers exactly the shared-perf scope — never re-discovers the whole reach.

6. **`pacing`** (only when `include_pacing`) — `pacing_report(reader, date_from=date_from,
   date_to=date_to, account_ids=<perf scope ids>, as_of=date_to, reporting_currency=reporting_currency,
   fx_table=table, precomputed_perf=perf)`. `as_of=date_to` makes the period complete
   (`elapsed_fraction == 1`) so the injected perf's window matches pacing's spend-to-date window (this
   is what lets the seam fire) and `variance_pct` is realized actual-vs-budget for the window — the
   same realized-variance semantics `flag`'s `include_pacing` already uses. Emit the rollup
   (`status_counts`, `worst_over_pacers`, `worst_under_pacers`). Cost: +3N budget reads. When off, set
   `pacing` to `None`. **Document** (mirroring flag's docstring) that true forward month-projection is
   `pacing_report` called directly with a mid-period `as_of`.

7. **`needs_you`** — a synthesized, worst-first shortlist merging (a) `goal_summary.pause_candidates`
   and (b) the high-severity entries from `attention.flagged` (severity == `high`). Dedupe by
   `ad_account_id` (an account that is both a pause candidate and high-severity flagged appears once,
   carrying both reasons). Each entry: `{ad_account_id, account_id, name, reasons: [...], sources:
   ["goal"|"attention"]}`. Zero extra reads. When `include_flags=False`, `needs_you` is built from
   pause candidates alone.

8. **`errors`** — union of each sub-tool's `errors`, each tagged with its `section`
   (`"performance"|"goal"|"attention"|"pacing"`). Per-account failures are already isolated inside each
   tool; carry them through.

## Output shape

```
{
  "date_from", "date_to", "reporting_currency", "fx_as_of", "fx_note",
  "scope": {"account_count", "reachable_count"},   # from perf
  "totals": {"normalized_total": {...}, "totals_by_currency": {...}},
  "top":  [ {ad_account_id, name, currency, spend, spend_normalized}, ... ],
  "bottom": [ ... ],
  "goal_summary": {"counts": {...}, "pause_candidates": [...]},
  "attention": {"flagged": [...], "informational": [...], "clean_count": N} | null,
  "pacing": {"status_counts": {...}, "worst_over_pacers": [...], "worst_under_pacers": [...]} | null,
  "needs_you": [ {ad_account_id, account_id, name, reasons, sources}, ... ],
  "errors": [ {section, ad_account_id, error}, ... ],
  "note": <present only when perf carried a note, e.g. "no accounts reachable">
}
```
Each section is clearly labeled and independently readable (the ticket's core UX requirement).

## Edge cases & interactions
- **Large / full-reach scope (`account_ids=None`)** → discovery fans over the whole reach; per the
  memory note the token reaches ~792 accounts and a full-fleet fan-out **times out**. Document a
  recommended ceiling in the docstring + MCP description and steer callers to pass `account_ids`. Keep
  the read-heaviest opt-ins (`include_pacing`, `include_ad_health`) **off by default** so the default
  path is ~`1 + 2N` reads (perf + baseline), never the `1 + 5N` full menu.
- **`no_goal_configured` accounts** → surfaced in `goal_summary.counts`, never errored (see §4 note).
- **Partial sub-tool failure isolates; digest still returns succeeding sections.** Wrap the grade /
  flag / pacing calls each in their own `try/except`: on an unexpected whole-call exception, set that
  section to `None`, append a `{section, error}` entry to `errors`, and STILL return the other
  sections. (Per-account failures never reach here — they are already isolated inside each tool. The
  shared perf succeeding means these calls generally cannot whole-call-fail; the guard is defensive.)
- **Shared-perf whole-call failure** (bad `reporting_currency`, or discovery `MetaApiError`) → the
  whole digest fails, matching every prereq tool's contract (nothing is computable without perf).
- **Currency** normalized to `reporting_currency` via static FX ([[currency-precision-low-priority]]);
  no-FX accounts inherit perf's native-figures-plus-`errors` treatment. Money is never summed across
  currencies (`totals_by_currency` stays per-currency).
- **`include_flags=False`** → `attention: null`; `needs_you` from pause candidates only.
- **`include_pacing=False`** (default) → `pacing: null`.
- **Empty / no-reachable scope** → perf returns `note="no accounts reachable"`; digest returns empty
  sections + the note; never raises.
- **`needs_you` dedupe + ordering** → worst-first; a dual-source account appears once with merged
  reasons. Deterministic tiebreak by `ad_account_id`.
- **Determinism** → all sections derive from the deterministic fan-outs; identical inputs → identical
  output (assert on ordering in tests).

## Tests (tests/test_meta_ads_analysis.py)
Use `FakeMetaReader` (records `.calls`) and the multi-account patterns near the existing
grade/flag/pacing suites.

- **Read-count is the headline test.** Default digest (`include_flags=True`, pacing off) over an
  explicit N-account scope issues exactly the perf fan-out (`N` current `fetch_insights`) + the
  baseline fan-out (`N` more), and **zero** extra `fetch_insights` from grade (proves the seam fired).
  Assert grade contributed no additional insight reads.
- `include_pacing=True` adds the `3N` budget reads (`list_campaigns`/`list_adsets`/`get_account`) and
  **no** additional perf `fetch_insights` (pacing consumed the shared perf).
- `include_flags=False` → `attention is None`, `needs_you` built from pause candidates only, and the
  baseline fan-out is NOT issued (only `N` perf reads).
- **Section correctness:** a fixture with (a) one on_goal, one pause_candidate, one no_goal_configured
  account, (b) one account with a spend spike (flagged high), (c) one clean account → assert
  `goal_summary.counts`, `attention.flagged`, and that `needs_you` merges the pause_candidate + the
  high-severity flagged account, deduped, worst-first.
- **Partial failure:** monkeypatch/inject so `grade` raises → digest returns `goal_summary: None`, an
  `errors` entry tagged `section="goal"`, and the other sections still populated.
- **Currency:** `reporting_currency="EUR"` normalizes totals to EUR and threads EUR consistently (no
  seam `ValueError`); a no-FX account appears in `errors` and keeps native figures.
- **MCP wiring:** `portfolio_digest` is in `build_discovery_tools(reader)` and
  `DISCOVERY_TOOL_DESCRIPTIONS`; a `--mock` smoke test returns a well-formed payload over the single
  seeded account with zero live calls (mirror `test_build_discovery_tools_flag_accounts_attention_mock_smoke`
  at ~line 11741).
- **Update the tool-count parity test** `test_build_discovery_tools_exposes_cross_account_summary`
  (~line 9677): it asserts "All nine discovery tools are exposed" with an explicit list — bump to ten,
  add `"portfolio_digest"` to the list, and add the `DISCOVERY_TOOL_DESCRIPTIONS` membership assertion.

## TODO

### Phase 1 — core composition
- Implement `portfolio_digest` in `account_discovery.py`: shared perf → totals/top/bottom → grade
  (threaded) → flag (threaded, gated) → pacing (threaded, gated) → `needs_you` synthesis → tagged
  `errors`. Wrap each sub-tool call in its own `try/except` for section-level isolation.

### Phase 2 — MCP wiring
- Add the `portfolio_digest` wrapper to `build_discovery_tools` (omit `fx_table`, mirror the other
  wrappers) and a rich `DISCOVERY_TOOL_DESCRIPTIONS["portfolio_digest"]` entry (state: one-call
  overview; the four sections; defaults — flags on, pacing/ad-health off; the read-cost + scope-ceiling
  guidance; that money is per-currency + normalized).

### Phase 3 — tests + validation
- Add the tests above; update the parity test.
- `pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/digest.log`; ruff + type checks per AGENTS.md.
