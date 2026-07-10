description: A new tool tells a manager whether each ad account is on track to spend its monthly budget — which are overspending, which are underspending, and the projected end-of-period total — across every account they oversee. Built, reviewed, and shipped.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
----

## What shipped

A sixth discovery tool, **`pacing_report`**, that answers "given spend-to-date and each account's
configured (ACTIVE daily) budget, will it land over / under / on target for the reporting period?"
across the whole token reach (or an explicit `account_ids` list). It is a **two-source join** — a
`cross_account_performance` read for spend-to-date + FX + scope, plus a second per-account fan-out for
budget config (`list_campaigns` + `list_adsets` budget-only fields + `get_account` for
spend_cap/amount_spent), joined by `ad_account_id`, projected linearly, and classified. Exposed on the
MCP server (auto-registered via the discovery loop) with `date_from/date_to/account_ids/as_of/
reporting_currency`; `fx_table` stays a test-only seam.

See the implement commit `32a31c5` for the full design rationale (docstrings + module header).

## Review findings

**Verdict: implementation is sound and faithful to the locked plan.** Reviewed the implement diff
with fresh eyes against the plan-stage spec, exercised the code, ran the suite. Two small inline
improvements applied; no major findings; two follow-ups were already correctly filed by the
implementer. Nothing sent backward.

### What was checked

- **Two-source join correctness (SPP / DRY).** `pacing_report` correctly rides
  `cross_account_performance` for scope resolution, FX, native+normalized spend, `account_status_label`,
  and per-account error isolation, then fans out budget config only over accounts that read OK in step
  1. Verified against the actual `cross_account_performance` / `fan_out_accounts` source
  (`account_discovery.py:519`, `:257`): `perf["accounts"]` is scope-ordered and deterministic
  regardless of worker finish order; `fan_out_accounts` returns input-order tuples; the main-thread
  assembly iterates `perf["accounts"]` and looks budget up by id → **deterministic output** (the
  reordering test confirms byte-identical `json.dumps`).
- **CBO double-count guard.** `summarize_account_budget` precedence (campaign daily → campaign
  lifetime → else per-ACTIVE-adset) matches the existing `control.classify_adset_budget` shape; a CBO
  campaign's decoy adset budget is never added. Confirmed by the precedence test *and* the end-to-end
  test (adset `daily="99999999"` ignored, account reads $300/day).
- **Status classification order.** `not_started` → `account_inactive` → `no_budget_set` →
  `budget_not_projectable` → over/under/on_track, checked in that order; boundaries at ±tolerance are
  strict (`>`/`<`), so exactly ±0.05 is `on_track`. Cap-only and lifetime-only accounts correctly land
  `budget_not_projectable`; a paused/closed account is `account_inactive` even with a real budget.
- **Currency discipline / FX-invariance.** `variance_pct` is a same-currency ratio, computed once from
  native and identical to the normalized value; a no-FX account keeps native figures, gets a real
  native verdict + shortlist slot, is excluded from normalized totals, and its FX gap surfaces once in
  `errors` (inherited from step 1). Confirmed by the no-FX test.
- **Error paths.** Step-1 insight failure → account absent + verbatim error + **no budget read
  attempted** (asserted via a call-tracking stub). Step-2 budget failure → `budget_unread` status +
  a single `{"stage":"budget",…}` error, never double-reported, excluded from over/under + shortlists.
- **Edge / regression cases.** Verified independently: an ACTIVE account with a budget but **zero
  spend-to-date** projects to 0 and classifies `under` (variance −1.0), no divide-by-zero. Not-started
  read window never inverts (`read_to` clamps up to `date_from`). `from > to` raises before any read.
  Invalid `reporting_currency` raises (inherited).
- **Docs.** README pacing paragraph and `docs/META_API_SETUP.md` both updated and accurate; the
  setup doc's discovery-tool count reads "six … `pacing_report`". No stale "five discovery tools"
  references remain anywhere (`grep` clean). MCP `DISCOVERY_TOOL_DESCRIPTIONS` entry present and the
  server registers it via the existing loop (`mcp_server.py:1096`).
- **Lint/tests.** Project ships no configured linter (no ruff/mypy in the venv, none in
  `pyproject.toml`); `py_compile` clean. `pytest tests/ -q` → **572 passed** (571 pre-review + 1 new
  clock-branch test).

### Minor — fixed in this pass

- **Untested clock branch (coverage gap).** `as_of=None → today (UTC)` was the one branch no test
  exercised (the implementer flagged it). Added `test_pacing_report_as_of_none_defaults_to_utc_today`,
  which freezes `_account_discovery.datetime` and asserts the omitted `as_of` defaults to the frozen
  UTC "today" and drives the same 14/31 window → `on_track`.
- **Rollup total type consistency.** `total_period_budget_normalized` / `total_projected_normalized`
  used bare `sum(gen)`, yielding an `int` `0` when no projectable FX account contributed but a `float`
  otherwise. Seeded both with `0.0` so the emitted totals are always `float`. (Output already
  deterministic per input; this only removes an int-vs-float inconsistency across scopes.)

### Reviewed and accepted as-is (spec-faithful, no change)

- **Shortlists include all projectable accounts (over/under/on_track), not just over/under.** The
  implementer flagged this for a fresh look. Confirmed it matches the locked plan verbatim
  ("worst_over_pacers = projectable accounts sorted by variance_pct desc") and the sample output in
  the implement ticket. A manager reading `worst_over_pacers` on a small scope (<10 projectable
  accounts) will see on_track/under accounts padding the tail — a defensible design (it always shows
  the *relative* worst), but a candidate UX refinement if a manager finds it confusing. Left unchanged
  because it is a deliberate, locked design decision; changing shortlist semantics is a product call
  for a human, not a review-stage edit. **Not filed as a ticket** — the behavior is correct per spec
  and the concern is purely presentational; note it here rather than manufacture backlog.
- **`budget_unread` None-fills every budget-derived field**, `overall_variance_pct` is `None` (not a
  fabricated `0.0`) when nothing projectable contributed, `excluded_from_rollup` is an int, and
  `status_counts` always carries all 8 enum keys. All consistent, deterministic, and reasonable
  signals; confirmed against the tests.

### Nit — noted, not changed

- **`PACING_ACCOUNT_FIELDS` fetches `"currency"` but the value is unused** — pacing takes currency from
  the step-1 perf row; `get_account`'s currency is dropped. Harmless (a defensive/self-documenting
  field on a read that already fetches `spend_cap`/`amount_spent`); left as-is.

### Empty categories

- **No correctness bugs found.** The join, projection, classification, currency handling, determinism,
  and error isolation all hold under the checks above.
- **No security / resource-cleanup findings.** No new I/O beyond the inherited reader; the fan-out uses
  the shared bounded-concurrency `ThreadPoolExecutor` (context-managed) with no new state.

## Follow-ups (already filed by implementer, verified appropriate)

- `tickets/backlog/pacing-currency-aware-minor-units.md` — `_minor_to_major` hardcodes ÷100, off by
  100×/10× for zero-/three-decimal currencies (JPY/KRW/BHD/…). Documented limitation.
- `tickets/backlog/pacing-prorate-lifetime-budgets.md` — lifetime-only accounts are
  `budget_not_projectable`; prorating against schedule overlap is future work.

Read cost `~1 + 4N` is accepted per spec (same posture as the attention tool's 2× note).

## Validation

- `.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py` — OK.
- `.venv/bin/python -m pytest tests/ -q` — **572 passed**.
