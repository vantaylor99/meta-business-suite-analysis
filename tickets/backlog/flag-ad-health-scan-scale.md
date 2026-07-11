description: The "which accounts need attention" scan can time out when its optional ad-health check runs over several large accounts, because it lists every single ad in each flagged account (some have thousands) just to count the disapproved or stalled ones. Make that check scale without losing its robust way of counting.
files: src/meta_ads_analysis/account_discovery.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md
difficulty: medium
----
## Problem

`flag_accounts_needing_attention(..., include_ad_health=True)` times out on real fleets. Observed
live 2026-07-11 over MCP: `include_pacing=True` + `include_ad_health=True` on **6 accounts** returned
`MCP error -32001 (timed out)`; the same call on **2 accounts** completed.

Root cause is **not** missing concurrency — the ad-health scan already fans out concurrently across
flagged accounts via `fan_out_accounts`. The cost is that, per flagged account, it enumerates
**every ad** — `reader.iter_paginated("/{ad_account_id}/ads", fields=_AD_HEALTH_FIELDS, limit=200)` —
to count `ads_disapproved` and `ads_not_delivering`. Large accounts make this brutal:
- Washington Seattle Mission: 2,229 ads (~12 sequential pages)
- Michigan Detroit Mission: 6,975 ads (~35 sequential pages)

Pagination within one account is sequential, so concurrency across accounts doesn't save the slowest
account, and several large accounts together exceed the MCP client timeout.

## The design tension to resolve (why this isn't a trivial fix)

The obvious mitigation — filter the ads edge **server-side by `effective_status`** — would cut the
read volume dramatically, but it trades away the tool's deliberately **robust, self-healing** count.
`ads_not_delivering` is defined by **exclusion**: `status == "ACTIVE"` AND `effective_status` NOT in
`_AD_NOT_DELIVERING_EXCLUSIONS` (a delivering status, a deliberate pause, or DISAPPROVED). That
exclusion-based rule keeps working when Meta adds a new non-delivering status; a positive/inclusion
server-side filter would silently **miss** any status not in its hardcoded list. So the plan must cut
the read cost WITHOUT regressing to brittle inclusion-based counting.

Note `ads_disapproved` is a single fixed status (`DISAPPROVED`) and **is** safely server-side
filterable — that half is easy; the `ads_not_delivering` half is the hard part.

## Options to weigh (plan stage picks one, documents the tradeoff)
- **(a) Split the two counts.** Server-side `effective_status=DISAPPROVED` filter for the disapproved
  count (safe, fixed status); for not-delivering, bound the enumeration (see b) or accept a cheaper
  proxy.
- **(b) Cap + honest signal.** Cap ads scanned per account (e.g. first N) and emit an explicit
  "counts are a floor, capped at N" marker in the output — never a silent truncation.
- **(c) Async/job execution** for the heavy scan.
- **(d) Delivery via insights** — infer "active but not delivering" from an insights/delivery signal
  rather than full ad enumeration.

## Interim (cheap, do regardless)
- Document a bounded-scope ceiling for `include_ad_health` in the tool description and
  `docs/META_API_SETUP.md` (the scan already reads ads for flagged accounts only, never the full
  scope — that gate stays).

## Edge cases & interactions
- Account with 0 ads; account with thousands of ads (the timeout case).
- Partial page-fetch failure already isolates to `errors` with `stage:"ad_health"` — keep that.
- Whatever the chosen approach, the disapproved count (fixed status) and the not-delivering count
  (exclusion-based) must both stay correct; add a test that a newly-introduced unknown non-delivering
  `effective_status` is still counted (guards against a brittle inclusion regression).
- Interacts with `include_pacing` (the other read-heavy opt-in) — the combined cost is what actually
  blew the timeout; consider the ceiling for both flags on together.
