description: Shipped the attention-scan MCP tool that flags the handful of ad accounts that changed and need a human's attention right now (spend spikes/collapses, worsening cost, stalled delivery, account-status problems), and reviewed it.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
----

## What shipped

A fifth discovery tool, **`flag_accounts_needing_attention`**, implemented as a **pure post-processor
over `cross_account_performance`** (the same relationship `account_benchmark` has to that tool). It
calls `cross_account_performance` twice over the same resolved scope — a current window and an
equal-length prior baseline — joins the per-account rows by `ad_account_id`, and runs a pure flag
evaluator over each pair. No new Meta read shape; it inherits FX normalization, Simpson's-paradox-safe
derived metrics, per-account failure isolation, and the deterministic fan-out for free.

Flags: `spend_spike` (medium→high at ≥2× knee), `spend_collapse` (high), `stalled_delivery` (high,
ACTIVE-gated), `cost_per_result_degraded` (high), `cpc_degraded` (medium), `ctr_dropped` (medium),
`account_status_alert` (high/medium), plus info-only `newly_active` / `insufficient_history`. Output
buckets: `flagged` (severity ≥ medium, sorted severity desc → |normalized-spend delta| desc →
`ad_account_id` asc), `informational`, `clean_count`, and window-tagged `errors`.

Build + full test suite green: **555 passed** (534 pre-ticket + 20 from implement + 1 added in review).

## Review findings

**Approach:** read the implement diff (`git show 36f98c8`) with fresh eyes before the handoff summary,
traced every division/guard in `evaluate_attention_flags`, verified the join/bucket/sort/error-merge in
`flag_accounts_needing_attention` against the actual `cross_account_performance` row shape, cross-checked
the docs against the code, and ran `py_compile` + the full suite. No separate lint tool (`ruff`/`mypy`)
is configured in `pyproject.toml`, so lint = `py_compile` (clean).

### Correctness / logic — no defects found

- **All ratio denominators are guarded.** Every `/ baseline` sits behind a truthy/`>= floor` check
  (`base_s`, `base_cpr`, `base_cpc`, `base_ctr`); a zero/absent/below-floor baseline routes to
  `newly_active` / `insufficient_history` and returns before any comparison flag. Confirmed no
  `ZeroDivisionError` / `inf` path exists. `compute_derived_metrics` omits (never zero-fills) undefined
  ratios, so a missing key correctly reads as "cannot compute this flag," never 0.
- **Bucketing is sound.** Only `info`/`medium`/`high` severities are ever emitted (the `low` rank is
  dead-but-harmless defensive code), so `informational` == info-only and `flagged` == medium+, matching
  the docstrings. Severity = max over fired flags is correct.
- **Error/join isolation is correct.** An account that read-failed in the baseline window is `continue`d
  (never mis-counted as clean); one that failed the current window is never iterated (not in `cur_rows`);
  both surface in window-tagged `errors`. A no-FX account keeps a native row and is still flagged, with
  its FX-gap note additionally in `errors`.
- **Determinism holds** — verified by the existing reverse-finish-order test; join iterates `cur_rows`
  insertion order and the final sort is a stable total order.

### Tests — gaps filled inline (minor)

Added `test_flag_accounts_attention_stall_informational_and_current_error`, covering three join/bucket
paths the implement tests exercised only at the pure-unit level:
- `stalled_delivery` through the **real** two-window join (ACTIVE account, delivering baseline, empty
  current row),
- the `informational` bucket (`newly_active`),
- a **current-window-only** read failure (account in baseline rows but not current) — excluded from all
  buckets, never counted clean, surfaced in `errors` tagged `"current"` (previously only the
  baseline-only failure was integration-tested).

Remaining lower-value test gaps left as-is (unit-covered, low risk): integration cases for `cpc_degraded`
/ `ctr_dropped`, and a no-FX-plus-read-failure-same-scope combination.

### Design calls reviewed and ACCEPTED (not defects)

- **Overlapping flags (no dedup)** — a DISABLED-and-collapsed or stalled-ACTIVE account fires two flags
  (e.g. `spend_collapse` + `stalled_delivery`). Accepted: severity = max keeps the account's severity
  correct, and surfacing every independent signal is defensible for a human triage list. No behavior
  change made; no test demands suppression either way.
- **`stalled_delivery` false-positive on deliberate all-ads pause** — account-level `ACTIVE` ≠ ad
  delivery status; distinguishing needs an ad-level fan-out, explicitly parked in
  `tickets/backlog/mcp-attention-pacing-and-disapprovals.md`. Documented in the docstring. Accepted.
- **`cpc_degraded` volume gate uses the material-spend floor** (no dedicated clicks floor constant
  exists) and **`delta_pct` is a fraction** (documented; the `detail` string carries the human "%").
  Both confirmed intentional and consistent with the threshold constants.
- **2× read cost** vs a single `cross_account_performance` — accepted and documented; a single
  multi-window read is a future optimization.
- **Sort tiebreak uses normalized-spend delta** even for non-spend flags, and for no-FX accounts falls
  back to native magnitude (incomparable across currencies at equal severity). Cosmetic ordering only —
  never affects which flags fire or which bucket an account lands in. Accepted as documented.

### Docs — accurate, with one forward-reference noted (minor, no change)

README.md and docs/META_API_SETUP.md were read in full against the code: the five-tool count, the flag
list, the bucket semantics, the low-volume gating, and the ~2×-read note all match the implementation.
The docs and code comments point budget-pacing questions at a **`pacing_report`** "sibling tool" that
does **not exist yet** — it is still a planned ticket (`tickets/plan/3-mcp-pacing-report.md`). Left
as-is: the whole MCP discovery suite is in active build, `pacing_report` is a committed planned ticket,
and "design/document as if the sibling lands" is the correct posture per the workflow rules. Flagging so
the next agent knows the reference is forward-looking, not stale.

### Validation

```
.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py   # clean
.venv/bin/python -m pytest tests/ -q                                                            # 555 passed
```

No major findings — no new fix/plan tickets filed. The one behavioral question (flag dedup) was
resolved in favor of the shipped behavior. Follow-on scope (budget pacing, ad-level disapprovals) is
already tracked in `tickets/plan/3-mcp-pacing-report.md` and
`tickets/backlog/mcp-attention-pacing-and-disapprovals.md`.
