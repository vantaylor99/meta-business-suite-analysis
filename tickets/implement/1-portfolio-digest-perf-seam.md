description: Let three of our cross-account analysis tools accept an already-fetched performance snapshot instead of each re-fetching the same numbers from Meta, so a future one-call overview can read the data once and reuse it.
prereq:
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why this ticket exists

The portfolio-digest composite (next ticket) must call four cross-account tools over the **same
scope + window** without paying 3–4× the Meta reads. Three of those tools (`grade_accounts_against_goals`,
`flag_accounts_needing_attention`, `pacing_report`) each begin by calling
`cross_account_performance` over the window — the exact read the digest already has in hand.

This ticket adds a **precomputed-perf injection seam** to those three functions so a caller that
already holds a `cross_account_performance` result for the window can hand it in and skip the internal
fetch. It is a purely **additive, backward-compatible** change: with the new kwarg omitted, every
function behaves byte-identically to today. The digest (prereq-dependent ticket) is the only consumer;
shipping the seam first keeps that ticket focused on composition, and keeps the seam independently
reviewable and testable.

The seam is the **internal/test-style** kind, exactly like the existing `fx_table` param: keyword-only,
default `None`, and **NOT exposed to the LLM** in `build_discovery_tools` (the MCP wrappers do not add
it). It exists for in-process composition only.

## The three seams

All three live in `src/meta_ads_analysis/account_discovery.py`. In each, find the internal
`cross_account_performance(...)` call and gate it behind the new kwarg.

### 1. `grade_accounts_against_goals` — add `precomputed_perf: dict[str, Any] | None = None`

Currently (line ~3148) it computes `scope_ids` then calls `cross_account_performance(reader,
date_from=..., date_to=..., account_ids=scope_ids, level="account")` and iterates `perf["accounts"]`.

- When `precomputed_perf is None`: unchanged (including the `empty_default` early-return for an empty
  configured registry with default scope).
- When `precomputed_perf is not None`: **skip** the internal fetch and use it as `perf`. The scope
  becomes exactly `precomputed_perf["accounts"]`; the `scope_ids`/`empty_default` logic is bypassed
  (the caller already resolved scope). `registry_by_id` is still built the same way, so an account in
  the injected perf that is absent from config is graded `GOAL_NO_CONFIG` exactly as today.
- **Native-metric invariant (verify, document):** grade reads only `row.get(metric)` (native
  `cost_per_result`/`roas`) and `row.get("spend")` (native) — never the `*_normalized` twins. Those
  native values are present in a `cross_account_performance` row **regardless** of the perf's
  `reporting_currency`. So an injected perf normalized to any currency yields the **same grade output**
  as grade's own USD-default fetch. Add a test asserting this parity.

### 2. `flag_accounts_needing_attention` — add `precomputed_current_perf: dict[str, Any] | None = None`

Currently (line ~1732) it calls `cross_account_performance` **twice** — `current` then `baseline`.

- When `precomputed_current_perf is None`: unchanged.
- When provided: use it verbatim as `current`; **still fetch `baseline`** itself (the baseline window
  is not the digest window, so it cannot be shared).
- **Consistency guard:** the injected current perf must have been computed with the same
  `reporting_currency` this call resolves. Add a defensive check — if
  `precomputed_current_perf["reporting_currency"] != reporting.upper()`, raise `ValueError` with an
  actionable message. (The digest passes the same `reporting_currency` + shared `fx_table`, so this
  never trips from the digest; it guards a misuse.)
- Do **not** touch flag's internal `include_pacing` path in this ticket — the digest sets
  `include_pacing=False` on its flag call and paces separately, so flag's documented current-window
  duplicate inside its own pacing call is irrelevant here. Leave it as-is.

### 3. `pacing_report` — add `precomputed_perf: dict[str, Any] | None = None`

Currently (line ~2346) it reads `read_to = date_from if elapsed_fraction <= 0 else effective_as_of`
then calls `cross_account_performance` over `[date_from, read_to]`.

- When `precomputed_perf is None`: unchanged.
- When provided **and** `elapsed_fraction > 0`: use it as `perf`, skipping the step-1 fetch. The step-2
  budget fan-out over `perf["accounts"]` is unchanged. **Contract (document in the docstring):** the
  caller guarantees the injected perf covers `[date_from, effective_as_of]` in the same
  `reporting_currency`. Add the same reporting-currency `ValueError` guard as flag.
- When provided **but** `elapsed_fraction <= 0` (period not started): **ignore** the injected perf and
  do the existing `[date_from, date_from]` read. This edge is defensive — the digest always passes
  `as_of=date_to` with `date_from <= date_to`, so `elapsed_fraction > 0` and the injected perf is used.

## Edge cases & interactions
- **Backward compatibility is the headline requirement.** Every existing test for grade / flag /
  pacing must pass **unchanged**. The default (kwarg omitted) path must be byte-identical — assert this
  by leaving the existing suites untouched and green.
- **Scope mismatch (grade):** if a caller injects a perf whose accounts differ from what `account_ids`
  would resolve, the injected accounts win (documented). This is intentional — the digest deliberately
  grades its full resolved scope, surfacing non-configured accounts as `no_goal_configured`.
- **reporting_currency mismatch (flag, pacing):** guarded with `ValueError` (see above). Test it.
- **Empty injected perf** (`{"accounts": [], "errors": [...]}`): grade returns empty accounts + zeroed
  counts (no crash); flag joins against an empty current (nothing flagged); pacing runs an empty step-2
  fan-out. Test at least grade with an empty injected perf.
- **errors passthrough:** each function already surfaces `perf["errors"]`; when perf is injected, its
  `errors` must flow through identically (no-FX entries, per-account read failures).
- **No-FX accounts in injected perf:** grade still reads their native metrics; flag still uses native
  spend for floors — same as when perf is self-fetched.

## Tests (add to tests/test_meta_ads_analysis.py, near the existing grade/flag/pacing suites)
`FakeMetaReader` records every read in `.calls` (see line ~7506 / ~9472) — use it to prove read savings.

- `grade` with `precomputed_perf` supplied issues **zero** `fetch_insights` (and zero `list_ad_accounts`)
  reads, and returns output **identical** to a `grade` run that self-fetched the same perf.
- `grade` parity when the injected perf was normalized to a **non-USD** `reporting_currency` — output
  matches the USD-self-fetch grade (proves grade reads only native metrics).
- `flag` with `precomputed_current_perf` issues `fetch_insights` for the **baseline window only** (one
  fan-out, not two) and returns output identical to the non-injected run over the same data.
- `flag` / `pacing` raise `ValueError` on a `reporting_currency` mismatch between the injected perf and
  the call.
- `pacing` with `precomputed_perf` issues **no** step-1 perf `fetch_insights` — only the `3N` budget
  reads (`list_campaigns`/`list_adsets`/`get_account`) — and returns output identical to the
  non-injected run.
- `pacing` with `precomputed_perf` but `elapsed_fraction <= 0` (as_of before date_from) **ignores** the
  injected perf and self-reads (asserts the defensive branch).

## TODO

### Phase 1 — grade seam
- Add `precomputed_perf` kwarg; gate the internal `cross_account_performance` call; bypass
  `scope_ids`/`empty_default` when injected. Update the docstring (note the native-metric invariant).

### Phase 2 — flag seam
- Add `precomputed_current_perf` kwarg; use as `current`, keep fetching `baseline`; add the
  reporting-currency `ValueError` guard. Update the docstring.

### Phase 3 — pacing seam
- Add `precomputed_perf` kwarg; use it when `elapsed_fraction > 0`, else ignore; add the
  reporting-currency `ValueError` guard. Update the docstring.

### Phase 4 — tests + validation
- Add the tests above.
- Run `pytest tests/test_meta_ads_analysis.py 2>&1 | tee /tmp/perf-seam.log` and confirm the full
  suite (all pre-existing grade/flag/pacing tests + new ones) is green. Run ruff/type checks per
  AGENTS.md.
