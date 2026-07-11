description: Add an opt-in "budget pacing off" signal to the "which accounts need attention" scan, so an account that is materially over- or under-spending its budget for the window shows up alongside the behavior-change alerts.
prereq: pacing-currency-aware-minor-units, pacing-prorate-lifetime-budgets
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py
difficulty: medium
----

## Context

`flag_accounts_needing_attention` (`account_discovery.py:1463`) is a **pure post-processor** over
`cross_account_performance`: two fan-outs (current + baseline window), join per-account rows by
`ad_account_id`, run `evaluate_attention_flags` (`account_discovery.py:1244`) over each pair, bucket
into `flagged` (severity ≥ medium) / `informational` / `clean_count`, sort deterministically.

Budget pacing (spend-to-date vs. configured budget) is a **different data surface** already owned by
the sibling `pacing_report` (`account_discovery.py:1983`), which is a two-source join
(`cross_account_performance` + a per-account campaign/adset/account budget fan-out) and emits, per
account, `{status, variance_pct, projected_spend[_normalized], period_budget[_normalized], …}` where
`status ∈ {over, under, on_track, no_budget_set, budget_not_projectable, account_inactive,
not_started, budget_unread}` and `variance_pct = (projected_spend - period_budget) / period_budget`
(FX-invariant). See `classify_pacing` (`account_discovery.py:1916`).

This ticket folds `pacing_report`'s per-account over/under verdict into the attention list as a new
`budget_pacing_off` flag — **reuse, never re-read budget config here**.

## Design (resolved)

**Opt-in join, no circular dependency.** `flag_accounts_needing_attention` gains
`include_pacing: bool = False`. When `True`, it calls `pacing_report` **once** over the SAME resolved
scope (`account_ids`), the SAME `reporting_currency`, and the SAME shared `fx_table`, pacing the
**current window**: `date_from=current_from`, `date_to=current_to`, `as_of=current_to`. There is no
cycle — attention → pacing → cross_account_performance; pacing never calls attention. Both live in
`account_discovery.py`.

**Why the current window / `as_of=current_to`.** The attention tool's window is arbitrary (e.g. last
7 days), not a calendar budget period. With `as_of=current_to` the period is *complete*
(`elapsed_fraction == 1`), so `projected_spend == spend_to_date` and `variance_pct` is the realized
actual-vs-budgeted-for-that-window variance — a legitimate, well-defined off-pace signal. An operator
wanting month-pacing calls `pacing_report` directly. Documented default; not a blocked question.

**Accepted duplicate read (documented tradeoff).** attention already reads `[current_from,
current_to]` via `cross_account_performance`; `pacing_report` reads the same window again internally.
We do **not** refactor `pacing_report` to accept a pre-fetched perf — that would break its contract
for one `N`-insight-read saving that is dwarfed by pacing's own `3N` budget reads. Accept the
duplicate; document it. Future optimization: thread a shared perf.

**The `budget_pacing_off` flag** — a pure helper `_budget_pacing_flag(pacing_entry, thresholds) ->
dict | None`, unit-testable with a hand-built pacing entry (no reader):

- Fires only when `status ∈ {over, under}` **and** `abs(variance_pct) >= thresholds.pacing_variance_pct`
  (a new, larger knee than pacing's own 5% `on_track` tolerance — a tiny 5% variance is not
  attention-worthy). All other statuses (`on_track`, `no_budget_set`, `budget_not_projectable`,
  `account_inactive`, `not_started`, `budget_unread`) → `None` (no flag).
- Severity: `over` → **high** (over-spend burns budget fast — urgent); `under` → **medium**
  (under-delivery is a missed-pacing concern, less urgent). This is the ticket's "materially over =
  high; materially under = medium" mapping (the ticket's word "cap" is loose — the denominator is the
  period *budget*, never the spend cap, matching `pacing_report`).
- Shape mirrors `_flag` (`account_discovery.py:1202`): `{name:"budget_pacing_off", severity,
  current: projected_spend_normalized (native fallback), baseline: period_budget_normalized (native
  fallback), delta: current-baseline, delta_pct: variance_pct, detail: "projected to spend {|v|*100:.0f}%
  over/under the period budget"}`.

**Loop refactor (two-phase).** Today the loop buckets each account immediately off its behavior
flags, `continue`-ing on empty. Pacing can push a **clean** or **informational** account into
`flagged`, so evaluation must precede bucketing:

1. Build `pacing_flag_by_id: dict[str, dict]` from the pacing read (only over/under-that-clear-the-knee
   accounts appear).
2. For each account readable in BOTH windows: `flags = evaluate_attention_flags(...)`, then append
   `pacing_flag_by_id.get(ad_account_id)` if present.
3. If the combined `flags` is empty → `clean_count += 1`; else build the entry (`severity =` max over
   ALL flags incl. pacing), bucket by `severity >= medium`, sort as today.

`evaluate_attention_flags` stays **pure and unchanged** (no pacing input) — the pacing flag is
appended by the orchestrator, exactly as `account_status_alert` is a baseline-independent flag.

**Errors + scope.** Merge `pacing["errors"]` into the attention `errors` list, each tagged
`{"stage": "pacing", …}` (distinct from the existing `{"window": current|baseline, …}` tags). An
account off-pace but unreadable in both attention windows is **not** surfaced (the loop already skips
it — attention is fundamentally a window-comparison tool); documented limitation, not a bug. An
invalid `reporting_currency` still raises the same `ValueError` (attention's own reads validate it
first; pacing uses the same table).

**New config knob** (`config.py`, mirroring the `ATTENTION_*` block at `config.py:57`):
`ATTENTION_PACING_VARIANCE_PCT = 0.25` — the "materially off-pace" knee (25%). Add it to
`AttentionThresholds` (`account_discovery.py:1124`) as `pacing_variance_pct` with the default wired in
`AttentionThresholds.defaults()`. Import the constant in `account_discovery.py` alongside the other
`ATTENTION_*` imports.

**MCP wrapper** (`mcp_server.py:556`): add `include_pacing: bool = False` to the discovery wrapper and
pass it through. Update `DISCOVERY_TOOL_DESCRIPTIONS["flag_accounts_needing_attention"]`
(`mcp_server.py:454`): document the opt-in `budget_pacing_off` flag and soften the existing "budget
pacing … is a SEPARATE tool" note to "off by default; pass include_pacing=true to fold pacing_report's
over/under verdict in as a flag." `thresholds`/`fx_table` remain test-only seams (not exposed).

## Read cost (documented)

Default (`include_pacing=False`): unchanged `~2N` insight reads — a hard regression guard. With
`include_pacing=True`: `+ ~1 + 4N` (pacing_report's own `cross_account_performance` `1+N` + budget
fan-out `3N`), of which the current-window `cross_account_performance` (`N`) duplicates attention's
own current read. Opt-in and documented.

## Edge cases & interactions

- **`include_pacing=False` is byte-identical to today** — no pacing read issued, no new reads, output
  shape unchanged. Assert an existing attention test still passes untouched.
- **Clean → flagged promotion:** an account with no behavior flags but `over`/`under` past the knee
  moves out of `clean_count` into `flagged`; `clean_count` must decrement correctly.
- **Informational → flagged promotion:** a `newly_active`/`insufficient_history` (info) account that is
  also off-pace lands in `flagged` (severity high/medium wins over info). Verify it is not
  double-listed.
- **Variance below the knee:** `status == over` but `abs(variance_pct) < pacing_variance_pct` → no
  flag (pacing's 5% over/under must not leak in as an attention alert).
- **Non-over/under statuses:** `no_budget_set`, `budget_not_projectable`, `account_inactive`,
  `not_started`, `budget_unread` → never a `budget_pacing_off` flag.
- **Pacing read failure for one account (`budget_unread`):** no flag for it; the pacing error is
  surfaced tagged `stage:"pacing"`, never silently dropped.
- **Off-pace but unreadable in both windows:** skipped (documented); surfaces only via errors.
- **Severity = max over ALL flags:** an account with a medium behavior flag + a high `budget_pacing_off`
  reports `high` and sorts accordingly.
- **Determinism:** with pacing joined, identical inputs → identical buckets/order. The flagged sort key
  (severity desc, |normalized-spend delta| desc, id asc) is unchanged; re-run the determinism test with
  `include_pacing=True`.
- **Currency:** `variance_pct` is FX-invariant (same-currency ratio) — use it directly. `current`/
  `baseline` use the normalized twins with a native fallback for a no-FX account.
- **Empty / no-accounts-reachable scope:** no pacing read side-effects; `note` preserved.
- **Invalid `reporting_currency`:** whole-call `ValueError` (unchanged contract), shared `fx_table`.

## TODO

### Phase 1 — engine
- Add `ATTENTION_PACING_VARIANCE_PCT = 0.25` to `config.py` with a comment in the `ATTENTION_*` block.
- Add `pacing_variance_pct: float` to `AttentionThresholds` + wire it in `.defaults()`; import the
  constant in `account_discovery.py`.
- Write `_budget_pacing_flag(pacing_entry, thresholds) -> dict | None` (pure).
- Refactor `flag_accounts_needing_attention`: add `include_pacing: bool = False`; when true call
  `pacing_report` once (current window, `as_of=current_to`, same scope/currency/fx_table); build
  `pacing_flag_by_id`; move bucketing to after behavior-flag evaluation + pacing-flag append; merge
  pacing errors tagged `stage:"pacing"`.

### Phase 2 — MCP surface
- Thread `include_pacing` through the `mcp_server.py` discovery wrapper.
- Update `DISCOVERY_TOOL_DESCRIPTIONS["flag_accounts_needing_attention"]`.

### Phase 3 — tests (extend `tests/test_meta_ads_analysis.py`; reuse `_attention_reader`, `_fx`)
- `_budget_pacing_flag` unit: over→high, under→medium, below-knee→None, each non-over/under status→None.
- End-to-end (build a reader whose insights + campaign/adset/account budget stubs drive `pacing_report`):
  a clean account promoted to `flagged` by `budget_pacing_off`; `clean_count` decrements.
- `include_pacing=False` path unchanged (regression guard — no budget reads issued: assert the
  reader recorded no `list_campaigns`/`list_adsets`/`get_account` calls).
- Pacing per-account failure → `budget_unread`, no flag, error tagged `stage:"pacing"`.
- Determinism with `include_pacing=True`.
- MCP smoke: `build_discovery_tools(...)["flag_accounts_needing_attention"](..., include_pacing=True)`.

### Validation
- `python -m pytest tests/test_meta_ads_analysis.py -k "attention or pacing" 2>&1 | tee /tmp/att.log`
- Run the linter/type-check the repo uses (see AGENTS.md) before handoff.
