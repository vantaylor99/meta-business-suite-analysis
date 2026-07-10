description: A new tool tells a manager whether each ad account is on track to spend its monthly budget — which are overspending, which are underspending, and the projected end-of-period total — across every account they oversee. This ticket reviews that implementation.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: hard
----

## What was built

A sixth discovery tool, **`pacing_report`**, that answers "given how much each account has spent so far
this period and its configured (active daily) budget, will it land over, under, or on target?" across
the whole token reach (or an explicit `account_ids` list). Unlike `account_benchmark` /
`flag_accounts_needing_attention` (pure post-processors), pacing is a **two-source join** — budget
config is not in the insights row:

1. **Spend-to-date + FX + scope** — one `cross_account_performance` over `[date_from, effective_as_of]`
   (inherits scope resolution, native + normalized `spend`, `currency`, `account_status_label`, the
   shared `fx_table`, per-account error isolation).
2. **Budget config** — a second `fan_out_accounts` over the accounts that read OK in step 1, each
   reading `list_campaigns` + `list_adsets` (budget-only fields) + `get_account` (`spend_cap` /
   `amount_spent`), computing the CBO-deduplicated ACTIVE daily-budget sum.
3. **Join + project + classify** by `ad_account_id` → per-account entries + a `rollup`.

### Files touched

- `config.py` — `PACING_ON_TRACK_TOLERANCE_PCT = 0.05`, `PACING_SHORTLIST_LIMIT = 10` (alongside the
  `ATTENTION_*` block, commented; no magic numbers in the engine).
- `account_discovery.py` — pure helpers `pacing_period`, `project_spend`, `_minor_to_major`,
  `summarize_account_budget`, `classify_pacing`; the `pacing_report` orchestration + `_build_pacing_rollup`
  + `_pacing_shortlist_entry`; module field lists `PACING_CAMPAIGN_FIELDS` / `PACING_ADSET_FIELDS` /
  `PACING_ACCOUNT_FIELDS`; the status enum `_PACING_STATUSES` / `_PACING_PROJECTABLE`.
- `mcp_server.py` — `pacing_report` added to `DISCOVERY_TOOL_DESCRIPTIONS` and `build_discovery_tools`
  (exposes `date_from, date_to, account_ids, as_of, reporting_currency`; `fx_table` kept as a
  test-only seam, not exposed). Server auto-registers it via the existing discovery loop.
- `README.md` + `docs/META_API_SETUP.md` — pacing paragraph added; the discovery-tool count updated
  "five → six".
- `tests/test_meta_ads_analysis.py` — pure-helper + integration tests (see below).

## How to validate

- `.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py` — OK.
- `.venv/bin/python -m pytest tests/ -q` — **571 passed** (was 539 pre-ticket + 32 new pacing/wiring
  tests; the discovery-set test was updated five→six).

### Test coverage (the floor, not the ceiling)

Pure helpers: `pacing_period` (mid-period 14/31, day-1, not-started, completed, single-day, `from > to`
→ ValueError); `project_spend` (normal, zero-fraction → None, completed == actual); `_minor_to_major`
(cents, blank → None, "0"); `summarize_account_budget` (CBO daily / CBO lifetime / non-CBO / paused
campaign / paused adset / mixed); `classify_pacing` (over/under/on_track boundaries at ±0.05,
no_budget_set, budget_not_projectable, account_inactive, not_started short-circuit).

Integration (FakeMetaReader, MOCKS ONLY): end-to-end 6-account scope asserting every status +
`rollup.status_counts` + both shortlists' ordering; shortlist tiebreak by `ad_account_id`; no-FX
account (native-only, in shortlist via native variance, excluded from normalized totals, FX gap in
errors); budget-read failure → `budget_unread` (tagged once, excluded, not double-reported); insights
failure → absent + single verbatim step-1 error + **no budget read attempted**; determinism (reversed
worker finish order → byte-identical `json.dumps`); not-started note + non-inverted read window;
invalid `reporting_currency` → ValueError; `from > to` → ValueError before any read; MCP-wrapper smoke.

## Reviewer: please scrutinize these deliberate calls (spec-driven, but worth a fresh look)

- **Shortlists include ALL projectable accounts, not just over/under.** Per the locked spec
  ("worst_over_pacers = projectable accounts sorted by variance_pct desc"), `worst_over_pacers` /
  `worst_under_pacers` list every `over`/`under`/`on_track` account (mirror-image sorts), so an
  `on_track` account appears in *both* lists. Faithful to the ticket; confirm it reads right to a
  manager, or whether it should be pruned to genuinely over/under.
- **`budget_unread` entry shape.** For a step-2 read failure I set every budget-derived field
  (`period_budget`, `projected_spend`, `active_daily_budget`, `lifetime_budget_total`, `spend_cap`,
  `amount_spent`, and their normalized twins) to `None` — the ticket's sample entry shows numbers for a
  *normal* account and doesn't specify the failure shape. Every entry keeps the same key set;
  reviewer to confirm None-fill is the desired signal.
- **`overall_variance_pct` is `None`** when no projectable FX account contributed (total budget 0),
  rather than `0.0` — avoids a fabricated 0. Confirm.
- **`excluded_from_rollup` is an int** (= accounts not in the normalized totals). The spec comment said
  "+ why counts" ambiguously; I matched the example's integer and left the per-status breakdown to
  `status_counts`.
- **`status_counts` always carries all 8 keys** (incl. `budget_unread: 0` even with no failures) for
  deterministic output; the spec example omitted `budget_unread`.
- **Not-started read window.** When `as_of` precedes the period, `effective_as_of` clamps to
  `date_from - 1`; to avoid an inverted Meta read I read a single `[date_from, date_from]` window
  (projection is suppressed to `None` anyway). A test asserts the read window never inverts.
- **`as_of=None → today (UTC)`** is the single clock touch and the ONE branch no unit test exercises
  against the real clock (tests always pass explicit `as_of`, by design). Consider whether an
  injected-clock test is worth adding.

## Known gaps / limitations (documented, backlog filed)

- **Zero-/3-decimal currencies (JPY, KRW, …) are off by 100×/10×** — `_minor_to_major` hardcodes ÷100
  (correct for the 2-decimal majority). Documented in the docstring + README + setup doc; follow-up
  filed: `tickets/backlog/pacing-currency-aware-minor-units.md`.
- **Lifetime-only accounts are `budget_not_projectable`** (a lifetime budget spans the entity's own
  schedule, not an arbitrary reporting window; its total is surfaced but not projected). Follow-up
  filed: `tickets/backlog/pacing-prorate-lifetime-budgets.md`.
- **Read cost ~`1 + 4N`** (the shared spend read + 3 reads per readable account). Accepted per spec —
  same posture as the attention tool's 2× note; a single combined per-account read is a future
  optimization, intentionally out of scope.

## Suggested adversarial checks

- Re-derive `summarize_account_budget` against a fixture where a CBO campaign has lingering ACTIVE
  adset budgets — confirm the campaign budget wins and the adset budgets are never added (the
  double-count guard).
- Confirm currency discipline: a native budget is only ever compared to native spend per account; only
  the rollup sums normalized figures. Verify `variance_pct` is byte-identical whether computed from
  native or normalized (it is a same-currency ratio → FX-invariant).
- Confirm a no-FX account is excluded from `total_*_normalized` yet still gets a native verdict and a
  shortlist slot, and that its FX gap appears in `errors` exactly once (inherited from step 1).
