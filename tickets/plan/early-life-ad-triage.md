description: When a brand-new ad starts out badly, decide whether to kill it early or give it more time by comparing it to how similar past ads on this account behaved at the same age — then force a clear keep-or-kill call by day 3.
prereq:
files: src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/analyze.py, src/meta_ads_analysis/confidence.py, knowledge/accounts/<slug>/, followups/
difficulty: hard
----
## Why / intent (operator's words)

"If an ad is performing horribly on the first or second day, it should be turned off — but on day 1
that decision should be made based on previous data. If there's another ad from previous runs that
performed badly *comparatively* (similar amount spent, similar lead/result count, or a similar ratio
of spend-to-results) but later turned around and started performing well, then we should keep it on
with a follow-up to check on the third day. By the third day we should 100% be making a solid
keep-or-kill decision."

## What this is

An **early-life triage** for newly-launched ads (roughly day 1–3) that does NOT auto-kill on thin
early data, but instead grades a struggling new ad against the account's own history of comparable
new ads and either (a) kills it, (b) keeps it with a scheduled day-3 re-check, or (c) by day 3 makes
a confident keep/kill call. It is the constructive complement to the abstain-on-thin-data rule: same
discipline (don't act confidently on tiny samples), but it adds a *historical-analog* lookback so a
genuinely-bad ad still gets caught early without killing the next slow-starting winner.

## Behavior sketch

- **Day 1–2, ad is struggling** (below the early-performance bar — define below): do NOT abstain
  silently and do NOT auto-kill. Instead find **historical analogs** — past ads on this account that,
  at the *same age*, had a similar profile: similar spend, similar lead/result count, or (most robust)
  a similar spend-to-result ratio.
  - If one or more close analogs **later turned around** and performed well → **keep**, and file a
    day-3 follow-up (the repo already has the `followups` mechanism) to force the re-check.
  - If analogs at the same age **stayed bad / never recovered**, or there are **no analogs** → flag
    as an early **pause** candidate (through the normal guarded propose flow, carrying Evidence +
    confidence; never a silent kill).
- **Day 3:** enough data exists for a confident call — make a solid keep/kill recommendation with a
  real confidence band, not "insufficient data."

## Open design decisions (resolve in plan stage)

- **"Comparable" definition + tolerance:** which features (spend, result count, spend/result ratio)
  and what tolerance counts as an analog; recommend the spend-to-result *ratio at the same age* as the
  primary signal (operator called it out as the most robust), with spend/result-count as secondary.
- **Source of historical analogs:** the synced daily history per ad (first_seen/age) and/or the
  knowledge vault. Need an "at the same age" slice of past ads, not their lifetime totals.
- **"Turned around" signal:** how much later-window improvement counts as a recovery (e.g. crossed the
  account ROAS/cost-per-result target by day N).
- **Early-performance bar:** what "performing horribly" means on day 1–2 (per-account, goal-aware —
  ROAS vs target for purchase accounts, cost-per-result for others).
- **Age computation:** deterministic (`run_date - first_seen`), passed in — keep `monitor.py`'s
  no-`datetime`-in-logic style.

## Constraints

- Goes through the existing guarded flow (propose → approve → validate → execute), carries Evidence +
  a computed confidence band, passes the adversarial review gate. No silent kills.
- Goal-aware: works for purchase/ROAS accounts AND (eventually) install-goal accounts — note the
  interaction with the install-goal backlog items.
- Mocks-only tests (fake history provider); no live Meta in tests.

## Edge cases & interactions

- **No historical analogs at all** (new account / new creative direction): fall back to abstain +
  day-3 follow-up rather than a confident early kill.
- **Survivorship bias:** "an analog turned around" must not be cherry-picked from one lucky recovery —
  weigh how *many* similar analogs recovered vs. stayed bad.
- **Grace window:** must not contradict the existing `monitor.py` protective grace for recently-changed
  ads; reconcile the two so an ad isn't simultaneously "protected" and "early-pause flagged."
- **Day-3 follow-up loop:** the filed follow-up must be findable/closable; don't spam duplicate
  follow-ups across runs for the same ad.
