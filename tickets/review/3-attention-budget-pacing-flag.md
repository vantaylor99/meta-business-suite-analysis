description: Review the new opt-in "budget pacing off" alert that lets the "which accounts need attention" scan also flag accounts that are materially over- or under-spending their budget for the window.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## What shipped

`flag_accounts_needing_attention` gained an opt-in `include_pacing: bool = False`. When `True`, it
calls `pacing_report` **once** over the same resolved scope / `reporting_currency` / shared `fx_table`,
pacing the **current** window (`date_from=current_from`, `date_to=current_to`, `as_of=current_to`), and
folds each account's over/under verdict into the attention list as a new `budget_pacing_off` flag.
`include_pacing=False` (the default) is byte-identical to before — no pacing read is issued.

### Engine (`account_discovery.py`)
- **`_budget_pacing_flag(pacing_entry, thresholds) -> dict | None`** (new, pure, unit-testable with a
  hand-built pacing entry — no reader). Fires only when pacing `status ∈ {over, under}` **and**
  `abs(variance_pct) >= thresholds.pacing_variance_pct` (the new 25% knee). `over` → **high**, `under`
  → **medium**. Every other status (`on_track`, `no_budget_set`, `budget_not_projectable`,
  `account_inactive`, `not_started`, `budget_unread`) → `None`. Shape mirrors `_flag`: `current` /
  `baseline` use the normalized projected-spend / period-budget twins with a **native fallback** for a
  no-FX account; `delta_pct = variance_pct` (FX-invariant); `detail = "projected to spend {|v|*100:.0f}%
  over/under the period budget"`.
- **Two-phase loop refactor.** Bucketing now follows behavior-flag evaluation **and** the pacing-flag
  append, so a pacing flag can be the only fired flag and promote a *clean* or *informational* account
  into `flagged`. `evaluate_attention_flags` stays **pure and unchanged** (no pacing input) — the
  pacing flag is appended by the orchestrator, exactly like the sibling flags.
- **Errors.** `pacing["errors"]` are merged into the attention `errors` list tagged
  `{"ad_account_id", "stage": "pacing", "error"}` (distinct from the window-tagged `{"window":
  current|baseline, …}` attention errors).
- **`AttentionThresholds`** gained `pacing_variance_pct: float`, wired in `.defaults()` from the new
  `config.ATTENTION_PACING_VARIANCE_PCT = 0.25`.

### MCP surface (`mcp_server.py`)
- `include_pacing: bool = False` threaded through the discovery wrapper (exposed to the LLM; `thresholds`
  /`fx_table` remain test-only seams).
- `DISCOVERY_TOOL_DESCRIPTIONS["flag_accounts_needing_attention"]` updated: softened the old "budget
  pacing is a SEPARATE tool" note to "off by default; pass include_pacing=true to fold pacing_report's
  over/under verdict in as a budget_pacing_off flag", and points month-pacing users at `pacing_report`.

## Why the current window / `as_of=current_to`
The attention window is arbitrary (e.g. last 7 days), not a calendar budget period. `as_of=current_to`
makes the period *complete* (`elapsed_fraction == 1`), so `projected_spend == spend_to_date` and
`variance_pct` is the realized actual-vs-budgeted-for-that-window variance — a well-defined off-pace
signal. An operator wanting month-pacing calls `pacing_report` directly (documented default).

## Validation performed
- `python -m pytest tests/test_meta_ads_analysis.py -k "attention or pacing or budget_pacing"` → **35
  passed**.
- Full suite: `python -m pytest tests/` → **609 passed**.
- **No linter/type-checker was run: the repo configures none** (pyproject lists only `pytest`; `ruff`
  and `mypy` are not installed and there is no `.ruff.toml`/`mypy.ini`/config-section). Reviewer:
  confirm this matches project convention; if a linter is expected out-of-band, run it.

## Tests added (all in `tests/test_meta_ads_analysis.py`; a new `_attention_pacing_reader` helper
serves both attention's two windowed insight reads AND pacing's budget fan-out)
- **`test_budget_pacing_flag_unit_over_under_knee_and_statuses`** — `over`→high, `under`→medium,
  at-knee (0.25) fires, below-knee (0.10 / −0.24) → None, `None` variance → None, every non-over/under
  status → None, and the no-FX native-fallback for `current`/`baseline`/`delta`.
- **`test_flag_accounts_attention_pacing_promotes_clean_account`** — a behaviorally-clean, over-pacing
  account promoted into `flagged`; an on-track peer stays clean; `clean_count` decrements to exactly the
  on-track count.
- **`test_flag_accounts_attention_pacing_promotes_informational_account`** — a `newly_active` account
  that is also over-pacing lands in `flagged` (high wins over info), listed exactly once.
- **`test_flag_accounts_attention_pacing_off_issues_no_budget_reads`** — regression guard: default path
  issues **zero** `list_campaigns`/`list_adsets`/`get_account` calls; output unchanged.
- **`test_flag_accounts_attention_pacing_budget_read_failure_tagged`** — a per-account budget read
  failure → `budget_unread` inside pacing → no flag; error surfaced tagged `stage:"pacing"`.
- **`test_flag_accounts_attention_pacing_deterministic`** — identical inputs → identical buckets/order
  with `include_pacing=True`.
- **`test_build_discovery_tools_flag_accounts_attention_pacing_smoke`** — the wired MCP tool threads
  `include_pacing` through (loads the committed FX table itself); confirms the flag surfaces only with
  the opt-in on.

## Known gaps / things to scrutinize (tests are a floor)
- **Accepted duplicate read (by design, not fixed).** With `include_pacing=True`, attention reads the
  current window via `cross_account_performance`, and `pacing_report` reads the same window again
  internally. We deliberately did **not** refactor `pacing_report` to accept a pre-fetched perf (that
  would break its contract for one N-read saving dwarfed by pacing's own 3N budget reads). Documented;
  a shared-perf thread is a future optimization. Confirm the tradeoff is acceptable.
- **Off-pace but unreadable in both attention windows is not surfaced as a flag.** The join only
  evaluates accounts readable in BOTH windows, so such an account gets no `budget_pacing_off` flag; it
  appears only via errors. Documented limitation (attention is fundamentally a window-comparison tool),
  not a bug — verify you agree.
- **No-FX account + `include_pacing=True` produces redundant error entries.** Such an account already
  emits `window:"current"` and `window:"baseline"` FX-gap errors; pacing's own step-1 no-FX error then
  adds a third, `stage:"pacing"` entry with the same message. This is consistent with the "surface all
  errors" philosophy and errors are informational, but it's cosmetic redundancy a reviewer may want to
  dedup. **No end-to-end no-FX + include_pacing test was added** (the native-fallback mechanics are
  unit-tested and pacing's no-FX path is tested independently) — a candidate test to close the gap.
- **`stage:"pacing"` flattens pacing's internal sub-stages.** Both pacing step-1 (insight/no-FX) and
  step-2 (`stage:"budget"`) errors are re-tagged `stage:"pacing"` in the attention output, so the
  budget-vs-insight distinction is lost there. This matches the ticket's "each tagged `{stage: pacing,
  …}`" design; confirm the flattening is intended.
- **Severity mapping.** `over` → high, `under` → medium is the ticket's explicit "materially over =
  high; materially under = medium" call. The denominator is the period *budget*, never the spend cap
  (matching `pacing_report`); the ticket's word "cap" was loose.

## Read cost
Default (`include_pacing=False`): unchanged `~2N` insight reads (hard regression guard, tested). With
`include_pacing=True`: `+ ~1 + 4N` (pacing's own `1+N` insight + `3N` budget reads), of which the
current-window insight read (`N`) duplicates attention's own — accepted, documented.
