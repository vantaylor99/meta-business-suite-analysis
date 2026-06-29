---
title: Verify the 2026-06-29 placement fix lifted Engaged/Low Value (else consider Advantage+ re-enable)
account: divine_designs
due: 2026-07-01
status: open
created: 2026-06-29
---

Recovery checkpoint for the post-2026-06-22 account tank.

UPDATE 2026-06-29: we applied the leading fix — restricted Engaged Audience (120241592681330733) and Low
Value Customers (120242997920660733) to the proven manual placement set (Feed+Reels+Stories) that High
Value uses, via the CLI guarded flow (see the 2026-06-29 "action" decision-log entry). Both re-entered
learning (IN_PROCESS) on the edit, so the first ~2 days will be soft regardless. Diagnosis context: High
Value had the *identical* 1% lookalike + AA-off but manual quality placements and recovered (1.72x→2.10x);
Engaged/Low Value were on automatic/Advantage+ placements (all inventory) and tanked.

Decision owed on 2026-07-01:
1. FIRST confirm both ad sets are DELIVERING again (spend > $0, not stuck IN_PROCESS) — they were edited
   2026-06-29; an edit can hang in review.
2. Read the daily / 3-day trend for Engaged + Low Value. Are they climbing toward ~1.5x+ now that they
   are off automatic placements (allow for attribution lag + the post-edit learning reset)?
3. If YES / climbing: the placement fix is working — leave it alone, keep watching. The Engaged 5%→1%
   lookalike is the next optional lever if it is still short of target.
4. If still flat/down AFTER delivery resumed: placements were not sufficient — THEN test re-enabling
   Advantage+ Audience on Engaged via the CLI guarded flow (reversible; AA was disabled on purpose because
   it overrode the custom audiences). Test on Engaged first, not all ad sets at once.

Guardrails: one change at a time; do NOT mass-pause mid-relearn. All writes via the CLI, never the MCP.
