description: Review the new attention-scan MCP tool that flags the handful of ad accounts that changed and need a human's attention right now (spend spikes/collapses, worsening cost, stalled delivery, account-status problems).
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/config.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py, README.md, docs/META_API_SETUP.md
difficulty: hard
----

## What landed

A fifth discovery tool, **`flag_accounts_needing_attention`**, was implemented as a **pure
post-processor over `cross_account_performance`** — the same relationship `account_benchmark` has to
that tool. It calls `cross_account_performance` **twice** over the **same resolved scope** (once for a
current window, once for a prior baseline window of equal length), joins the two per-account rows by
`ad_account_id`, and runs a pure flag evaluator over each pair. No new Meta read shape was added; it
inherits FX normalization, Simpson's-paradox-safe derived metrics, per-account failure isolation, and
the deterministic fan-out for free.

Build + full test suite are green: **554 passed** (was 534 before this ticket; the plan's "520" figure
predated `account_benchmark` landing — see below). 20 new tests were added.

### Code map (what to review, and where)

- **`config.py`** — new `ATTENTION_*` block after `CONFIDENCE_CONVERSIONS_FLOOR`. Reuses
  `ATTENTION_MIN_SPEND = MIN_WASTE_SPEND` (100.0) and `ATTENTION_MIN_RESULTS_FLOOR =
  CONFIDENCE_CONVERSIONS_FLOOR` (25) by *reference* (not re-typed literals); new pct knees are
  `ATTENTION_SPEND_SPIKE_PCT`/`ATTENTION_SPEND_COLLAPSE_PCT` = 0.5 and
  `ATTENTION_CPR_DEGRADE_PCT`/`ATTENTION_CPC_DEGRADE_PCT`/`ATTENTION_CTR_DROP_PCT` = 0.3, each with a
  one-paragraph rationale.
- **`account_discovery.py`** (after the `account_benchmark` block, ~line 1066 onward):
  - `AttentionThresholds` (frozen dataclass) + `.defaults()` — the programmatic/test seam.
  - `prior_window(current_from, current_to)` — pure, clock-free, `ValueError` on `from > to` /
    unparseable.
  - `evaluate_attention_flags(current_row, baseline_row, thresholds)` — the pure heart; hand-testable
    with dict fixtures.
  - `flag_accounts_needing_attention(...)` — the tool: baseline resolution, one FX-table load shared
    across both reads, join, bucket, sort, error-merge.
- **`mcp_server.py`** — `DISCOVERY_TOOL_DESCRIPTIONS["flag_accounts_needing_attention"]` entry + a
  `build_discovery_tools` wrapper (exposes `current_from, current_to, account_ids, baseline_from,
  baseline_to, reporting_currency` only — **`thresholds` and `fx_table` are deliberately NOT exposed**
  to the LLM, matching how `fx_table` is hidden on the two prereq wrappers). Auto-registered by the
  existing `for name, func in build_discovery_tools(reader).items()` loop.
- **docs** — README.md and docs/META_API_SETUP.md updated four→**five** discovery tools, with an
  attention-scan paragraph and an explicit cross-reference to `pacing_report` as the budget-pacing
  counterpart.

### The flags (as implemented)

| flag | severity | fires when |
|------|----------|-----------|
| `insufficient_history` | info | baseline row absent OR baseline (normalized, native-fallback) spend below floor, and current also below floor |
| `newly_active` | info | baseline ~0/below floor **and** current ≥ floor (guards the ∞% divide-by-zero) |
| `spend_spike` | medium (→ high at ≥ 2× knee) | both windows ≥ floor **and** current ≥ baseline·1.5 (native %) |
| `spend_collapse` | high | baseline ≥ floor **and** current ≤ baseline·0.5 |
| `stalled_delivery` | high | baseline delivering, current spend **and** impressions ~0, **and** status ACTIVE |
| `cost_per_result_degraded` | high | both rows have `cost_per_result`, both results ≥ 25, current cpr ≥ baseline·1.3 |
| `cpc_degraded` | medium | both rows have `cpc`, both windows ≥ spend floor, current cpc ≥ baseline·1.3 |
| `ctr_dropped` | medium | both rows have `ctr`, current ≤ baseline·0.7 |
| `account_status_alert` | high / medium | status ∈ {DISABLED, PENDING_CLOSURE, CLOSED} → high; {UNSETTLED, PENDING_RISK_REVIEW, PENDING_SETTLEMENT, IN_GRACE_PERIOD} → medium |

Output buckets: `flagged` (severity ≥ medium, sorted `(severity desc, |normalized-spend delta| desc,
ad_account_id asc)`), `informational` (info-only, sorted by id), `clean_count` (int), `errors`
(union of both reads' errors, tagged `window: "current"|"baseline"`). An account read-failed in either
window is excluded from flagging and surfaces only in `errors`; a **no-FX** account is treated as
readable (native spend feeds the floor) and is still flagged, though its FX-gap note also appears in
`errors`.

## How to validate

```
.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py
.venv/bin/python -m pytest tests/ -q -k "attention or prior_window or flag_accounts or evaluate_flags" 2>&1 | tee /tmp/attention.log
.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/full.log      # expect 554 passed
```

Note: the pure `evaluate_attention_flags` tests are named `test_evaluate_flags_*` — include
`evaluate_flags` in any `-k` filter or they are silently deselected (they contain neither "attention"
nor "flag_accounts").

## Known gaps / where to push (tests are a floor, not a ceiling)

These are the spots a reviewer should scrutinize or extend — I made defensible calls but flag them
honestly:

1. **`stalled_delivery` false positives on deliberate pauses.** Account-level `account_status_label`
   (ACTIVE) is *not* ad-delivery status. An operator who pauses all ads over a weekend on an ACTIVE
   account will trip a **high** `stalled_delivery` flag. Distinguishing a deliberate all-ads pause from
   a real stall needs an ad-level fan-out, which is explicitly out of scope (parked in
   `tickets/backlog/mcp-attention-pacing-and-disapprovals.md`). Documented in the docstring; worth a
   second opinion on whether the noise is acceptable for the default scan.
2. **Overlapping flags are allowed (no dedup).** A DISABLED account that stopped spending fires BOTH
   `account_status_alert` and `spend_collapse`; a fully-stalled ACTIVE account (current spend = 0)
   fires BOTH `spend_collapse` and `stalled_delivery`. I followed the plan's flag table literally
   (independent flags); severity=max keeps the account's severity correct, but the `flags` list carries
   both. A reviewer may prefer suppressing `spend_collapse` when a more specific flag (status / stall)
   already explains the drop. No unit test pins this behavior either way — decide and lock it.
3. **`cpc_degraded` "clicks-implied volume" gate.** The plan's phrasing was fuzzy; I used the
   material-**spend** floor (both windows) as the volume proxy rather than a dedicated clicks floor
   (no such constant exists). Confirm this is the intended gate.
4. **`delta_pct` is a FRACTION** (0.6 == +60%), consistent with the threshold constants, documented in
   the `_flag` docstring. Confirm the name isn't misleading for LLM consumers (vs. a 0–100 number).
5. **Sort tiebreak uses spend delta even for non-spend-flagged accounts.** `primary_delta_magnitude`
   is the absolute normalized-spend delta regardless of which flags fired, per the plan. An account
   flagged only for `cpc_degraded`/`ctr_dropped` sorts by a possibly-small spend delta. Stable and
   documented, but a reviewer might want a metric-aware tiebreak.
6. **Read cost is 2×** a single `cross_account_performance` (one fan-out per window; ~400 reads for a
   200-account WWFT scope). Accepted and documented; single multi-window read is a future optimization.

### Test-coverage gaps to consider filling

- Integration-path coverage for `stalled_delivery`, `cpc_degraded`, `ctr_dropped`, and the
  `informational` bucket exists only at the **unit** (`evaluate_attention_flags`) level — a
  windowed-`FakeMetaReader` case for each would strengthen the join/bucket path.
- Only a **baseline-window** read failure is integration-tested; a **current-window-only** failure
  (account in baseline rows but not current rows) is handled by code but not asserted.
- No test combines a no-FX account with a genuine read failure in the same scope.
- Determinism test uses uniform accounts; a mixed-severity determinism case would be stronger.

## Scope boundaries (settled at plan — do not expand in review)

- **Budget pacing** (spend-to-date vs. configured budget) is owned by the sibling `pacing_report`
  tool — this tool never reads budget config.
- **Ad-level creative/disapproval** detection is parked in
  `tickets/backlog/mcp-attention-pacing-and-disapprovals.md` (needs the heavier per-ad fan-out).
- The cheap **account-level** health signal ships here as `account_status_alert` (zero extra reads).
