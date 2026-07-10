description: A scan that automatically surfaces the handful of ad accounts that changed and need a human's attention right now — sudden spend spikes or drops, worsening cost-per-lead, or delivery that stalled — so reviewing 200 accounts becomes reviewing the 8 that moved.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/config.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: hard
----

## What ships

A fifth discovery-surface tool **`flag_accounts_needing_attention`** alongside `list_ad_accounts`,
`cross_account_spend_summary`, `cross_account_performance`, and `account_benchmark`. It turns a
full-fleet review into a short, prioritized attention list by comparing a **current window** against
a **prior baseline window of equal length** and flagging accounts whose behavior changed or breached
a threshold.

It is a **pure post-processor over `cross_account_performance`** — the same relationship
`account_benchmark` already has to that tool (see `account_discovery.py:801-1064`). It calls
`cross_account_performance` **twice** (once per window) over the **same resolved scope**, joins the
two per-account metric rows by `ad_account_id`, and runs a pure flag-evaluation function over each
pair. This inherits — for free — FX normalization, Simpson's-paradox-safe derived metrics
(`compute_derived_metrics`), per-account partial-failure isolation, and the deterministic
bounded-concurrency fan-out. **No new Meta read shape is introduced.**

### Scope boundary (settled at plan — do NOT expand)

The source plan listed five flag families. Two are deliberately **out of this tool**:

- **Budget pacing off** — this is a *different question* (spend-to-date vs. configured budget) over a
  *different data surface* (account spend cap / CBO / adset budgets). It is owned by the sibling tool
  `pacing_report` (`tickets/plan/3-mcp-pacing-report.md`), which already emits a shortlist of the
  worst over/under-pacers. Reimplementing budget-config reads here would duplicate it. **This tool
  does not read budget config.** Merging pacing into a single unified attention list is parked in
  `tickets/backlog/mcp-attention-pacing-and-disapprovals.md` (needs `pacing_report` landed first).
- **Ad-level creative/disapproval problems** — detecting DISAPPROVED ads requires a per-account
  ad-level fan-out (heavy: N accounts × their ads), a materially different read cost. Parked in the
  same backlog ticket. What we *can* deliver cheaply is the **account-level** health signal, which is
  already in every `cross_account_performance` row: `account_status` / `account_status_label`
  (DISABLED / UNSETTLED / PENDING_RISK_REVIEW / IN_GRACE_PERIOD / PENDING_CLOSURE). That becomes the
  `account_status_alert` flag below — zero extra reads.

So this tool's flags all derive purely from the two performance reads + the account-status label
already on each row.

## Behavior / interface

New in `account_discovery.py`:

```python
@dataclass(frozen=True)
class AttentionThresholds:
    """Overridable thresholds for the attention scan. Defaults come from config.py constants so no
    magic numbers live in the engine. Injectable for tests; the MCP wrapper uses the defaults (this
    is a programmatic/test seam, exactly like ``fx_table`` on cross_account_performance)."""
    spend_spike_pct: float          # ATTENTION_SPEND_SPIKE_PCT      (current >= baseline*(1+pct))
    spend_collapse_pct: float       # ATTENTION_SPEND_COLLAPSE_PCT   (current <= baseline*(1-pct))
    cost_per_result_degrade_pct: float  # ATTENTION_CPR_DEGRADE_PCT  (cpr up beyond pct)
    cpc_degrade_pct: float          # ATTENTION_CPC_DEGRADE_PCT
    ctr_drop_pct: float             # ATTENTION_CTR_DROP_PCT
    min_spend_floor: float          # ATTENTION_MIN_SPEND (normalized) — material-spend gate
    min_results_floor: float        # ATTENTION_MIN_RESULTS_FLOOR — cost-degradation significance gate

    @classmethod
    def defaults(cls) -> "AttentionThresholds": ...


def prior_window(current_from: str, current_to: str) -> tuple[str, str]:
    """The immediately-preceding window of equal length. Pure, clock-free: parses ISO YYYY-MM-DD,
    length = (to - from).days + 1 (inclusive), baseline_to = from - 1 day,
    baseline_from = baseline_to - (length - 1). Raises ValueError on unparseable dates or from > to."""


def evaluate_attention_flags(
    current_row: dict[str, Any] | None,
    baseline_row: dict[str, Any] | None,
    thresholds: AttentionThresholds,
) -> list[dict[str, Any]]:
    """PURE flag evaluation over two per-account metric rows (as emitted by
    cross_account_performance). Fully unit-testable with hand-built dict fixtures — no reader.
    Returns the fired flags, each: {name, severity, current, baseline, delta, delta_pct, detail}."""


def flag_accounts_needing_attention(
    reader: "MetaReaderProvider",
    *,
    current_from: str,
    current_to: str,
    account_ids: list[str] | None = None,
    baseline_from: str | None = None,
    baseline_to: str | None = None,
    reporting_currency: str = "USD",
    thresholds: AttentionThresholds | None = None,
    fx_table: FxTable | None = None,
) -> dict[str, Any]:
    ...
```

**Baseline resolution.** Both `baseline_from`/`baseline_to` omitted → `prior_window(current_from,
current_to)`. Both given → used verbatim. Exactly one given → `ValueError` (ambiguous). Overlap with
the current window is allowed but not corrected (the caller's explicit choice); document it.

**Comparison currency discipline.** Percent deltas are computed on **native** figures — for a single
account the currency is identical in both windows, so a % move is currency-invariant and exact.
Absolute floors (`min_spend_floor`) are compared on the **normalized** figure (`spend_normalized`)
so "$100 of spend" means the same across a USD and an MXN account; fall back to native `spend` only
when the account has no FX rate (already surfaced in `errors` by the underlying read). Reuse the
`reporting_currency` / `fx_table` plumbing exactly as `account_benchmark` does — load the table once
here and pass it through to **both** `cross_account_performance` calls so both windows normalize
against one table, and an invalid `reporting_currency` fails the whole call with the same `ValueError`
contract as the prereq.

### Flags (each with trigger + default severity)

`compute_derived_metrics` already omits (never zero/inf-fills) any ratio whose denominator is 0 or
whose component is absent, so a row simply *lacks* `cost_per_result`/`cpc`/`ctr`/`roas` when
undefined — the evaluator must treat "key absent" as "cannot compute this flag," never as 0.

| flag | trigger | severity |
|------|---------|----------|
| `insufficient_history` | baseline row absent, OR baseline `spend` (normalized) below `min_spend_floor` | info |
| `newly_active` | baseline spend ~0 / below floor **and** current spend ≥ `min_spend_floor` (divide-by-zero baseline → "newly active", never an ∞ % spike) | info |
| `spend_spike` | both windows ≥ floor **and** current ≥ baseline·(1 + `spend_spike_pct`) | medium (→ high when ≥ 2× the threshold move) |
| `spend_collapse` | baseline ≥ floor **and** current ≤ baseline·(1 − `spend_collapse_pct`) | high |
| `cost_per_result_degraded` | both rows have `cost_per_result` **and** both windows cleared `min_results_floor` **and** current cpr ≥ baseline·(1 + `cost_per_result_degrade_pct`) | high |
| `cpc_degraded` | both rows have `cpc`, both ≥ floor on clicks-implied volume, current cpc ≥ baseline·(1 + `cpc_degrade_pct`) | medium |
| `ctr_dropped` | both rows have `ctr`, current ≤ baseline·(1 − `ctr_drop_pct`) | medium |
| `stalled_delivery` | baseline was delivering (spend or impressions above floor) **and** current ≈ 0 (spend and impressions) **and** `account_status_label == "ACTIVE"` (i.e. NOT a deliberately paused/disabled account) | high |
| `account_status_alert` | `account_status_label` ∈ {DISABLED, PENDING_CLOSURE, CLOSED} → high; {UNSETTLED, PENDING_RISK_REVIEW, PENDING_SETTLEMENT, IN_GRACE_PERIOD} → medium | high/medium |

`stalled_delivery` vs. a deliberately-off account is the key disambiguation: only fire when the
account reads ACTIVE. A DISABLED/paused account with zero current delivery surfaces via
`account_status_alert`, not a false "stalled" failure.

### Severity + prioritized output

Severity rank: `high(3) > medium(2) > low(1) > info(0)`. An account's severity is the **max** over
its fired flags. Output buckets so the reader goes straight to the worst:

```python
{
  "current_window":  {"date_from": ..., "date_to": ...},
  "baseline_window": {"date_from": ..., "date_to": ...},
  "reporting_currency": "USD", "fx_as_of": ..., "fx_note": ...,
  "account_count": <resolved scope size>,       # attempted
  "reachable_count": <same as account_count>,   # mirror the prereq's field
  "flagged": [                                   # severity >= medium, sorted worst-first
     {"ad_account_id", "account_id", "name", "currency", "account_status_label",
      "severity": "high", "flags": [ {name, severity, current, baseline, delta, delta_pct, detail}, ... ]}
  ],
  "informational": [ ... ],   # accounts whose ONLY flags are info (newly_active / insufficient_history)
  "clean_count": <int>,       # accounts compared with no flags at all — kept as a count, not a table
  "errors": [ {ad_account_id, window: "current"|"baseline", error}, ... ],
}
```

**Sort order (deterministic):** `flagged` sorted by `(severity_rank desc, primary_delta_magnitude
desc, ad_account_id asc)` where `primary_delta_magnitude` is the largest absolute normalized-spend
delta among the account's fired flags (a stable, documented numeric tiebreak; `ad_account_id` is the
final total-order tiebreak so ties never reorder run-to-run).

**Errors merge:** an account that errored in **either** window cannot be compared. Union the two
reads' `errors`, tagging each with which window it came from, and **exclude** that account from
flagging (it appears only in `errors`, never silently dropped, never in `clean_count`). A no-FX
account (present in `errors` of the underlying read but still carrying a native row) is treated as
readable — it just uses native spend for the floor.

### config.py constants (new block, documented like the existing ones)

Add an `# Attention scan (see account_discovery.flag_accounts_needing_attention)` block. Prefer
**reuse over new magic numbers** where a peer constant already carries the right semantics, with a
comment tying them together:

- `ATTENTION_MIN_SPEND` — material-spend floor. Same magnitude/rationale as `MIN_WASTE_SPEND` (100.0);
  set `ATTENTION_MIN_SPEND = MIN_WASTE_SPEND` (reference, do not re-type the literal) unless the
  implementer finds a reason to diverge, in which case document why.
- `ATTENTION_MIN_RESULTS_FLOOR` — cost-degradation significance floor; reuse
  `CONFIDENCE_CONVERSIONS_FLOOR` (25) the same way (both windows must clear it before a cpr flag
  fires — this is the ticket's "low-volume % deltas are noisy" guard).
- `ATTENTION_SPEND_SPIKE_PCT = 0.5`, `ATTENTION_SPEND_COLLAPSE_PCT = 0.5`,
  `ATTENTION_CPR_DEGRADE_PCT = 0.3`, `ATTENTION_CPC_DEGRADE_PCT = 0.3`, `ATTENTION_CTR_DROP_PCT = 0.3`
  — new, with a one-paragraph comment on each choice (mirror the config.py comment style; explain the
  50%/30% picks as the "noticeable move, not noise" knees).

### MCP wiring (mcp_server.py)

- Add a `DISCOVERY_TOOL_DESCRIPTIONS["flag_accounts_needing_attention"]` entry — plain-language,
  state the defaults (compares a window vs. the prior equal window; 50% spend move / 30% cost
  degradation; sorted worst-first) and that budget pacing is a separate tool (`pacing_report`).
- Add a `flag_accounts_needing_attention` wrapper inside `build_discovery_tools` and to its returned
  dict. Exposed params: `current_from, current_to, account_ids=None, baseline_from=None,
  baseline_to=None, reporting_currency="USD"`. **Do NOT expose `thresholds` or `fx_table`** to the
  LLM — both are programmatic/test seams (same treatment `fx_table` already gets on the two prereq
  wrappers). The `for name, func in build_discovery_tools(reader).items()` loop at
  `mcp_server.py:1028` auto-registers it; the description-count in docs is the only manual surface.

## Edge cases & interactions (tests must cover)

- **New account / no baseline** → `insufficient_history` (info), never a false `spend_spike`.
- **Baseline of 0 → current spend** → `newly_active` (info), never an ∞ `delta_pct`. The evaluator
  must guard every `/ baseline` on a zero/absent baseline.
- **Very low volume** → cost-degradation flags (`cost_per_result_degraded`, `cpc_degraded`) require
  BOTH windows to clear `min_results_floor` / the spend floor first, so a 2→1 result swing on $5 does
  not fire an alarm.
- **Deliberately paused vs. stalled** → `stalled_delivery` only when `account_status_label ==
  "ACTIVE"`; a DISABLED account with zero delivery surfaces as `account_status_alert`, not stalled.
- **Currency** → % deltas native (currency-invariant on one account); floors on `spend_normalized`
  with native fallback for a no-FX account; invalid `reporting_currency` → whole-call `ValueError`.
- **Metric present current, absent baseline (or vice-versa)** — e.g. results only tracked in the
  current window → the cpr flag is skipped (cannot compute), not a crash, and never a spurious "∞
  degradation."
- **Per-account failure in one window only** → account excluded from flagging, appears in `errors`
  tagged with the window; the rest of the fleet still evaluates. **Determinism**: identical inputs →
  identical buckets and order regardless of fan-out completion order (both underlying reads are
  already order-deterministic; the join/sort here must be too — hence the total-order tiebreak).
- **Only-one-baseline-bound** → `ValueError`. **`from > to`** in either window → `ValueError` from
  `prior_window` / validation.
- **Read cost note (not a bug, document it):** this tool issues **2× the per-account insight reads**
  of a single `cross_account_performance` (one fan-out per window). For a 200-account WWFT scope that
  is ~400 reads under the bounded pool. Acceptable and documented; a future optimization (single
  multi-window read) is out of scope. Surface nothing new — just note it in the docstring + docs.

## Docs

- `README.md` and `docs/META_API_SETUP.md`: update "four discovery tools" → **five**, add a
  one-paragraph description of the attention scan and its window/threshold defaults, and explicitly
  cross-reference `pacing_report` as the budget-pacing counterpart so no one expects pacing here.

## Key tests (write these; ~mirror the cross_account_performance / account_benchmark test blocks)

**Pure `prior_window`:**
- 7-day window `2026-06-08..2026-06-14` → `2026-06-01..2026-06-07`.
- Month/length boundaries (e.g. a 30-day window) compute the correct preceding span.
- `from > to` → `ValueError`.

**Pure `evaluate_attention_flags` (dict fixtures, no reader):**
- Each flag fires on a crafted row-pair; each stays silent just below its threshold (boundary tests).
- `insufficient_history` when baseline is `None` and when baseline spend below floor.
- `newly_active` (baseline 0, current material) → info, `delta_pct` is None/omitted, not ∞.
- `cost_per_result_degraded` suppressed when either window is below `min_results_floor`.
- `stalled_delivery` fires for ACTIVE+zero-current; does NOT fire for a DISABLED account (that yields
  `account_status_alert` instead).
- Severity of a multi-flag account = max of its flags.

**Integration with a `FakeMetaReader` (vary the row by `date_from`, as existing fakes do — see
`tests/test_meta_ads_analysis.py:9438` onward):**
- Two windows over a 3-account scope: one spikes, one collapses, one clean → `flagged` has the two
  worst sorted correctly, `clean_count == 1`.
- Per-account error in the baseline window only → that account in `errors` (window="baseline"),
  excluded from `flagged`, others still evaluated.
- Determinism: same inputs twice → identical output (including order).
- Explicit `baseline_from`/`baseline_to` honored; exactly-one-bound → `ValueError`.
- Invalid `reporting_currency` → `ValueError` (propagated from the prereq).
- No-FX account uses native spend for the floor and still gets flagged.

## TODO

### Phase 1 — pure core (no reader)
- Add the `ATTENTION_*` constants block to `config.py` (reuse `MIN_WASTE_SPEND` /
  `CONFIDENCE_CONVERSIONS_FLOOR`; document the new pct knees).
- Implement `AttentionThresholds` (+ `.defaults()`), `prior_window`, and `evaluate_attention_flags`
  in `account_discovery.py`, next to the `account_benchmark` block.
- Write the pure `prior_window` + `evaluate_attention_flags` tests (all flag/boundary/edge cases).

### Phase 2 — the tool
- Implement `flag_accounts_needing_attention`: load FX once, resolve baseline, call
  `cross_account_performance` twice over the same scope with the shared `fx_table`, join rows by
  `ad_account_id`, evaluate flags, bucket + sort + merge errors.
- Integration tests with a windowed `FakeMetaReader` (spike/collapse/clean, error isolation,
  determinism, explicit-baseline, invalid currency, no-FX).

### Phase 3 — wiring + docs
- Add the `DISCOVERY_TOOL_DESCRIPTIONS` entry and the `build_discovery_tools` wrapper (defaults only;
  no `thresholds`/`fx_table` exposed).
- Update `README.md` + `docs/META_API_SETUP.md` (four → five discovery tools; cross-reference
  `pacing_report`).

### Phase 4 — validate
- `.venv/bin/python -m py_compile src/meta_ads_analysis/{account_discovery,mcp_server,config}.py`
- Focused: `.venv/bin/python -m pytest tests/ -q -k "attention or prior_window or flag_accounts" 2>&1 | tee /tmp/attention.log`
- Full suite: `.venv/bin/python -m pytest tests/ -q 2>&1 | tee /tmp/full.log` (baseline was **520 passed**; expect 520 + the new tests).
