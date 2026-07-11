description: A new read tool that ranks the actual ads across all managed accounts — best/worst by spend, results, or cost-per-result — so a specialist sees at a glance which specific ads to scale or pause, without walking every ad ever created.
prereq:
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: hard
----

## What shipped

`cross_account_creative_triage` — the ad-level sibling of `rank_accounts`. It pools **one row per
delivering ad** across every reachable account (or an explicit `account_ids` subset), ranks the pool
by a single metric over a window, and returns the top-or-bottom N (winners vs losers = two calls,
`order="desc"` then `"asc"`). Built on `reader.fetch_insights(level="ad", time_increment="all_days")`,
so it sees only ads that actually delivered and never enumerates `/{account}/ads` — a *performance*
read, not an ad-health read.

Registered as the **ninth** discovery tool. See the implement-stage handoff (git
`ticket(implement): cross-account-creative-triage`) for the full design rationale — everything there
was verified correct in review and is not re-litigated here.

## Review findings

Adversarial pass over the implement diff (`af1df54`), read fresh before the handoff summary, then
cross-checked against the two siblings it reuses (`rank_accounts`, `cross_account_performance`) and
their shared helpers (`compute_derived_metrics`, `_resolve_result_key` /
`_infer_primary_result_action`, `_find_metric`, `fan_out_accounts`, `RANK_METRIC_ALIASES` /
`_RANK_MONEY_METRICS`).

### Checked

- **Correctness / parity.** The result-key resolution (config-first, else infer from the account's
  *pooled* ad actions), the lead-family self-heal branch, per-ad `compute_derived_metrics` (never an
  averaged ratio, never `inf`/`0`), the money `*_normalized` twin (`value` normalized /
  `value_native` native), and the partition → sort → 1-based-rank block all match the sibling
  semantics, adapted to key on `ad_id`. `account_count = len(scope.account_ids)` matches
  `cross_account_performance`.
- **Determinism.** `fan_out_accounts` returns results in input order regardless of worker completion;
  ranking sorts on `(value, ad_id)` with an `ad_id`-ascending tiebreak. The staggered-sleep partial-
  failure test confirms order-independence.
- **Metric/validation surface.** `RANK_METRIC_ALIASES` covers spend/cpm/cpc/ctr/cost_per_result
  (`cpl`/`cpa`)/roas/impressions/clicks/results; the four fail-fast `ValueError` paths (metric, order,
  limit, reporting_currency) raise before any Meta read.
- **Lint / types.** No mypy/pyright/ruff/black configured (`pyproject.toml` has only
  `[tool.pytest.ini_options]`). `py_compile` clean on all three source files. Nothing to lint.
- **Tests.** `python -m pytest tests/test_meta_ads_analysis.py -q` → **664 passed** (full file, so both
  discovery-tool enumeration assertions run).

### Found & fixed inline (minor)

1. **Docs were stale (the change *should* have touched them and didn't).** Both `README.md` and
   `docs/META_API_SETUP.md` enumerated "the **eight** discovery tools" and described up through
   `grade_accounts_against_goals` with no mention of the new tool. Fixed: bumped the count/enumeration
   to nine in `docs/META_API_SETUP.md`, and added a parallel prose description of
   `cross_account_creative_triage` in both files matching the surrounding style (ad-level sibling,
   delivery-only scope, two-call winners/losers, normalized-twin money ranking, per-account single
   no-FX error). Grepped the whole tree afterward — no remaining stale count reference.

2. **Test interaction gap.** The implementer's tests covered mixed-currency ranking (MXN+EUR) and a
   lone no-FX account separately, but not the two *pooled in one money-metric call*. Added
   `test_creative_triage_mixed_fx_and_no_fx_accounts_in_one_money_call`: one EUR (FX-able) + one JPY
   (no-FX, two ads) account, `metric="spend"` — asserts the EUR ad ranks on its normalized twin, both
   JPY ads land in `unranked` with the no-FX reason, `ad_count` still counts all three, and the JPY
   account emits **exactly one** `errors` entry (not one per ad). Passes; suite now 664.

### Found, verified intentional (no change)

- **A no-FX account emits its per-account error even when ranking by a currency-invariant metric
  (e.g. `ctr`).** Confirmed empirically. This is parity with `rank_accounts`, which always surfaces
  `cross_account_performance`'s no-FX errors regardless of the ranking metric — the error means "this
  account's money couldn't be normalized," which is true independent of the current sort field.
- **The no-FX error is per-account even for an account with zero delivering ads.** Consistent with
  `cross_account_performance` granularity; harmless (noise only, never affects ranking).
- **`result_label` is emitted unconditionally in ranked entries (may be `None`).** Differs from
  `rank_accounts` (which omits it entirely) but matches the documented ranked-entry shape; the per-ad
  *row* still omits it when no key resolved.
- **`ad_id` tiebreak is lexicographic string order** (Meta ids are strings), same convention as
  `rank_accounts`' `ad_account_id` tiebreak. Deterministic; string-sorted not numeric — intentional.
- **Duplicate `ad_id` across accounts** would affect only tiebreak ordering, never value correctness
  (each row carries its own `ad_account_id`). Ids are unique within Meta, so not a real case; the
  stable sort is sufficient.

### Deferrals confirmed (agree — not defects, no ticket filed)

- **No shared `_rank_pooled_rows(...)` helper.** ~40 lines of the partition/sort/rank block are
  duplicated from `rank_accounts` rather than extracted, per the plan's explicit lower-risk call. A
  future cleanup could unify both (re-run the `rank_accounts` **and** triage suites together if so).
- **No one-call `both_ends` winners+losers variant.** Winners vs losers is two calls, each re-running
  the ad-level fan-out; the tool description already steers operators to scope `account_ids`. Future
  enhancement.

### Not covered — flagged, not fixed (not agent-runnable)

- **No live-scale test.** Everything is mock-only. On the real token (792 accounts per
  [[token-reach-and-summary-scaling]]) an all-accounts triage issues one ad-level insights read per
  account through the bounded fan-out; each such read can itself be large/paginated for a high-ad-count
  account. Wall-clock and paging behavior against live data were not measured — this needs live
  credentials (not agent-runnable) and is the same class of scaling concern already tracked for
  `cross_account_spend_summary`. The tool description already steers operators to scope `account_ids`;
  no new ticket filed (it is operational validation, not a code defect). Worth a bounded `account_ids`
  sanity check out-of-band before trusting the all-accounts path.

### Major findings

None. No new fix/plan/backlog tickets filed.
