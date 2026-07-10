description: A scan that automatically surfaces the handful of ad accounts that need a human's attention right now — sudden spend spikes or drops, worsening cost-per-lead, stalled delivery, budget over/under-pacing — so reviewing 200 accounts becomes reviewing the 8 that changed.
prereq: mcp-cross-account-performance
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/early_triage.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: hard
----
## Problem

The highest-value multi-account operation for a manager is not "show me everything" — it's "tell me
which accounts I need to look at." This tool turns a full-fleet review into a short, prioritized
attention list by comparing a recent window against a prior baseline and flagging accounts whose
behavior changed or breached a threshold. There is existing single-account triage logic
(`early_triage.py`, `monitor.py`) whose signal definitions and thresholds should be reused/extended
rather than reinvented.

## What it must deliver

- **Period-over-period comparison** per account: a current window vs. a prior window of equal
  length (e.g. last 7 days vs. the 7 before), computing deltas on spend, CPL/CPA, CPC, CTR, and
  result volume.
- **A set of named flags**, each with a clear trigger and severity, at least:
  - spend spike / spend collapse (large % move vs. baseline),
  - cost-per-result degradation (CPL/CPA up beyond a threshold),
  - zero / stalled delivery (was delivering, now ~0),
  - budget pacing off (projected month spend materially over/under the daily-budget implied pace),
  - creative/ad delivery problems where detectable (e.g. disapprovals / active-but-not-delivering),
    to the extent the read surface exposes them.
- **Prioritized output**: accounts sorted by severity, each with the flags fired, the numbers behind
  each flag, and the account identity — so the reader can go straight to the worst first.
- Thresholds are **parameters with documented defaults**, not hardcoded magic numbers, and scale to
  any scope (specialist's 15 or the WWFT's 200) via `resolve_scope`.

## Behavior / interface (proposal — plan stage to finalize)

- `flag_accounts_needing_attention(current_from, current_to, account_ids=None,
  baseline=<prior equal window | explicit dates>, thresholds=<overridable>)` → list of
  `{account_id, name, severity, flags:[{name, current, baseline, delta, detail}]}` sorted by
  severity, plus an `errors` channel.
- Flag evaluation is a pure function over the two windows' metric rows (unit-testable with fixtures);
  reuse threshold/signal definitions from `early_triage.py` where they already exist.

## Edge cases & interactions

- New account with no baseline window → "insufficient history" flag, not a false spike.
- Very low volume (few clicks/results) → % deltas are noisy; require a minimum-volume floor before
  firing cost-degradation flags to avoid alarm spam.
- Account paused intentionally vs. stalled delivery → distinguish where status is readable; don't
  flag a deliberately-off account as a failure (cross-check with account/campaign status).
- Currency: cost-based thresholds must compare like-for-like — use normalized (reporting-currency)
  figures from `cross_account_performance`.
- Divide-by-zero on baseline of 0 (e.g. 0 → spend): report as "newly active," not infinite % change.
- Determinism + partial failure inherited from the fan-out engine.

## Use cases

- WWFT / manager: daily "what needs attention across all accounts" → an 8-item list instead of a
  200-row table.
- Supervisor: same over their department, to know which specialist to check in with.
- Specialist: early warning on their own accounts before a bad week compounds. Complements the
  existing single-account triage/monitor path.
