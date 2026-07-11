description: Reviewed and shipped the opt-in "budget pacing off" alert that lets the "which accounts need attention" scan also flag accounts materially over- or under-spending their budget for the window.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: medium
----

## What shipped

`flag_accounts_needing_attention` gained an opt-in `include_pacing: bool = False`. When `True`, it calls
`pacing_report` **once** over the same resolved scope / `reporting_currency` / shared `fx_table`, pacing
the **current** window (`date_from=current_from`, `date_to=current_to`, `as_of=current_to`), and folds
each account's over/under verdict into the attention list as a new `budget_pacing_off` flag (`over` →
**high**, `under` → **medium**, gated by a 25% `ATTENTION_PACING_VARIANCE_PCT` knee). `include_pacing=False`
(the default) is byte-identical to before — no pacing read is issued. New pure helper
`_budget_pacing_flag`; a two-phase bucketing loop lets a pacing flag promote an otherwise clean/info
account into `flagged`. MCP wrapper threads `include_pacing` through (exposed to the LLM);
`thresholds`/`fx_table` remain test-only seams. Full detail in the implement commit (972d306).

## Review findings

**Read the implement diff (972d306) first, with fresh eyes, before the handoff summary.** Verdict:
implementation is correct and well-tested; one doc-staleness issue found and fixed inline. No major
findings, no new tickets filed.

### Checked — correctness / logic
- **`variance_pct` sign vs. severity/detail.** `classify_pacing` sets `variance_pct = (projected −
  budget)/budget`; `over` ⇒ positive, `under` ⇒ negative. `_budget_pacing_flag` uses `abs(variance_pct)`
  for the knee and the detail string and the `status` word for direction — correct for both signs.
- **`as_of=current_to` ⇒ `elapsed_fraction == 1`.** Verified via `pacing_period` (line 1808–1813): when
  `as_of == date_to`, `elapsed_days == total_days`, so the fraction is exactly 1 and `project_spend`
  returns `spend_to_date` — the "realized variance" claim holds for daily-only accounts. (Minor nuance:
  for a lifetime budget paced over a schedule extending past the window, projected can differ from
  spend-to-date; this is inherited pacing-model behavior, the verdict is still well-defined, out of
  scope here.)
- **Native fallback.** `projected_spend_normalized`/`period_budget_normalized` → `projected_spend`/
  `period_budget` fallback for a no-FX account; `delta`/`current`/`baseline` all then native (same
  currency) — consistent with `_flag`'s shaping. Unit-tested.
- **Status filtering.** Only `over`/`under` past the knee fire; all other statuses (`on_track`,
  `no_budget_set`, `budget_not_projectable`, `account_inactive`, `not_started`, `budget_unread`) → None,
  and `variance_pct is None` → None. Unit-tested exhaustively.
- **Error re-tagging safety.** Every error entry produced by `cross_account_performance` (pacing step-1,
  lines 375/620/736) and pacing step-2 (line 2195) carries both `ad_account_id` and `error` keys, so the
  `stage:"pacing"` re-tag at 1651–1656 never yields silent `None`s. Confirmed.
- **Join/scope.** Bucketing iterates attention's `cur_rows`; `pacing_flag_by_id` is a by-id map, so a
  pacing flag for an account outside the both-windows join is silently dropped (documented limitation)
  and an account that pacing-failed keeps `clean` + a `stage:"pacing"` error. Both tested.
- **Determinism / sort.** `_sort_delta` derives from window spend (independent of pacing); a
  pacing-only-flagged account sorts by severity → |spend delta| → id. Idempotence test passes.

### Checked — no regression
- **Default path issues zero budget reads** — hard regression guard test asserts no
  `list_campaigns`/`list_adsets`/`get_account` call reaches the reader with `include_pacing` omitted.

### Found & FIXED (minor) — stale docs
The implement pass did not touch prose docs, which still asserted pacing was *excluded* from the
attention tool:
- `README.md` (~line 72): "**Budget pacing is a separate concern:** … answered by the `pacing_report`
  tool, not this one." → rewritten to document the opt-in `include_pacing`, its flag/severity mapping,
  promotion behavior, and the off-by-default cost guarantee.
- `docs/META_API_SETUP.md` (~line 326): "**Budget pacing is deliberately NOT here** …" → rewritten to
  describe the opt-in path (`as_of=current_to`, 25% knee, `stage:"pacing"` errors, `~1+4N` added reads,
  the accepted duplicate current-window read).
`docs/META_API_SETUP.md` line 262 ("none takes an `account` argument") remains accurate —
`include_pacing` is not an `account` arg. `config.py` inline comments and the code docstrings are
accurate and current.

### Reviewed & accepted (no action) — implementer's documented gaps
- **Duplicate current-window read** (attention's current read + pacing's step-1 read) — a deliberate
  tradeoff (threading a shared perf would break `pacing_report`'s contract for one `N`-read saving
  dwarfed by pacing's `3N` budget reads). Agree; not worth a ticket.
- **Off-pace but unreadable in BOTH attention windows is not flagged** — inherent to a window-comparison
  tool; surfaced via errors. Agree, not a bug.
- **No-FX + `include_pacing` triple-error redundancy** — cosmetic; consistent with the "surface all
  errors" philosophy. No end-to-end no-FX test was added, but the native-fallback branch of
  `_budget_pacing_flag` is unit-tested and pacing's no-FX path is independently tested, so the
  interaction risk is low. Accepted as a documented floor, not a required fix.
- **`stage:"pacing"` flattens pacing's internal `budget`/insight sub-stages** — matches the ticket's
  "each tagged `{stage: pacing, …}`" design. Intended.
- **Severity mapping `over`→high / `under`→medium** — the ticket's explicit call. Confirmed.

### Tests / lint
- `pytest -k "attention or pacing or budget_pacing"` → **35 passed**; full `pytest tests/` → **609
  passed**. Doc-only edits do not affect tests (not re-run after the markdown edits).
- **No linter/type-checker is configured** — `pyproject.toml` lists only `pytest` under `dev`; no
  `.ruff.toml`/`mypy.ini`/`setup.cfg`/`[tool.ruff]`/`[tool.mypy]` anywhere. Confirmed the implementer's
  claim; nothing to run.
