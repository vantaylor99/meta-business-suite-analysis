---
title: Check 100K +20% budget scale result (Engaged Audience)
account: divine_designs
due: 2026-06-29
status: done
completed: 2026-06-29
created: 2026-06-25
---

On 2026-06-26 raised 100K campaign budget $280 -> $336 (+20%, CBO) to feed the Engaged Audience ad set (3.39 ROAS winner). Check:
1. Did CBO push the extra budget to Engaged Audience, or did it leak into High Value Customers (1.75 drag)? Compare ad-set spend share vs before.
2. Engaged Audience ROAS holding >= ~3.0 at the higher spend? (scaling often softens ROAS a bit)
3. 100K campaign blended ROAS vs the 2.65 baseline.
Directional read only at 3 days (CBO re-balances for a few days); confirm over ~a week. If CBO fed the 1.75 drag instead of Engaged, escalate to a structural fix (trim High Value Customers) rather than a min-spend (which would reset learning).

## Findings (2026-06-29)

Overtaken by events — judged against a directional, not clean, test.
- **Q1 (did CBO feed Engaged, or leak to the High Value drag?):** Fed Engaged. 7d ad-set spend —
  Engaged Audience $1,723 vs High Value Customers $197 (~9:1). CBO targeting is healthy; **no structural
  trim of High Value warranted.**
- **Q2 (Engaged holding ~3.0 at higher spend?):** No — Engaged 7d ROAS 0.64. But this is the
  account-wide crash (Advantage+-off + placement re-learning + attribution lag), not the budget scale.
- **Q3 (100K blended vs the 2.65 baseline?):** Collapsed — 100K campaign 0.79 (7d) / 0.59 (3d). The
  sibling `May Lower Spend` campaign is ~0.70 too, confirming an account-wide cause, not the scale.
- The +20% was rolled back ($336 → $280) on 2026-06-27 during the slump (see decision-log / PR #5).

**Conclusion:** budget-scale experiment is inconclusive/moot — the account-wide tank + the rollback
dominated. Durable takeaway: CBO fed the winner, not the drag. "Is Engaged recovering?" now lives in the
daily watch, not this follow-up. Do not re-scale or trim until the account stabilizes. Closing.