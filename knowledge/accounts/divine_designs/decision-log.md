# Divine Designs — decision log

Append-only, dated. Newest first. Record every change to the live account + the reason + result.

## 2026-06-27 (action) — killed influencer ad "IFA Base" (clicky, non-converting)

**Account change executed (via Meta MCP write tool — guarded-flow exception, hand-logged):**
- **Paused ad `IFA Base`** (120246473310000733, Influencer Test ad set, May Lower Spend campaign).
  `status=PAUSED` (effective_status IN_PROCESS, propagating). The ad set is left intact for future
  influencer tests; only the ad was paused.
  - *Why:* over Jun 24–27 it ran **9.26% CTR / 3,513 clicks but only 4 purchases on $693** (ROAS 0.65) —
    the single source of the account-wide CTR spike (1.1% → 5.5%) the operator noticed. Classic
    influencer pattern: magnetic content, curiosity clicks, no purchase intent. A ~$175/day leak at
    ~0 conversion. Not an early-life "needs more data" case — the traffic demonstrably doesn't convert.
  - *Heads-up (not yet acted on):* Influencer Test sits in the **May Lower Spend** CBO campaign, so its
    freed budget will redistribute to **Low Value Customers** (also weak, ~0.35 ROAS over the same
    window). Stopping the influencer bleed alone doesn't fix May Lower Spend — that campaign's budget
    may need attention separately.
- *Caveat:* MCP write bypasses the repo guardrails (status change; reversible) — operator-approved,
  logged per the MCP-write-exception rule.

## 2026-06-27 (action) — rolled 100K budget back $336 → $280 (account ROAS slump)

**Account change executed (via Meta MCP write tool — guarded-flow exception, hand-logged):**
- **Reverted the `100K` campaign daily budget $336 → $280 (CBO)** (campaign 120241587361240733).
  Verified after: `daily_budget $280.00`, `effective_status ACTIVE`. Connector force-paused on edit
  again (`status_forced_to_paused: true`); reactivated via `ads_activate_entity`, ~seconds downtime.
  - *Why:* the daily trend showed the account dropped to **sub-1.0 ROAS starting ~Jun 23–24**
    (purchases ~25/day → 2–6/day) — **before** the Jun 26 scale, so the scale did not cause it.
    Most likely re-learning from the earlier Jun 22 (Advantage Audience disabled) + Jun 24 (placement
    policy) changes, plus attribution lag understating the latest 1–2 days. With ROAS weak, the +20%
    was overspending into the dip, so we undid it. No point scaling into a slump.
  - *Not done:* did not cut below $280 or pause — just reverted the scale. Re-evaluate once the account
    exits re-learning (watch the 6/29 follow-up).
- *Caveat:* MCP write bypasses the repo's propose→approve→validate→execute guardrails (CLI can't do CBO
  budgets yet) — operator-approved one-off, logged here per the MCP-write-exception rule.

## 2026-06-26 (action) — scaled 100K campaign +20% ($280→$336) to feed the Engaged Audience winner

**Account change executed (via Meta's official MCP write tool — NOT the repo guarded flow):**
- **Raised the `100K` campaign daily budget $280 → $336 (+20%, CBO)** (campaign 120241587361240733).
  Verified after: `daily_budget $336.00`, `effective_status ACTIVE`; Engaged Audience + High Value
  Customers ad sets both ACTIVE.
  - *Why:* Engaged Audience (inside 100K) is the account's best ad set — **3.39 ROAS** (above the 3.0
    target) — and underfunded vs the Low Value Customers drag (2.00 ROAS on ~$8k/30d). +20% stays
    within the guardrail cap and the ~20% "safe zone" that avoids a learning reset. Trusting CBO to
    push the extra toward Engaged. This is a **before/after directional test, not a clean A/B.**
  - *Gotcha logged:* Meta's MCP connector **force-paused the campaign on edit**
    (`status_forced_to_paused: true` — its safety default for any edit). Reactivated immediately via
    `ads_activate_entity`; a brief pause does not reset learning (needs ~7 days off). Net downtime: seconds.
  - *Caveat:* executed via the Meta MCP, which **bypasses the repo's propose→approve→validate→audit
    guardrails** (the repo's own budget tool can't do CBO campaign budgets yet — see the CBO backlog
    gap). Operator-approved manually.
- **Considered but deliberately NOT done:** an Engaged Audience min-spend (would reset that ad set's
  learning — defeats the short test) and cutting the `White BG V2` drag in Low Value Customers
  ($3,833 @ 1.78 — the bigger leak, to handle separately).

**Follow-up:** 3-day check (due 2026-06-29) — read Engaged Audience + 100K ROAS/spend; confirm CBO
fed the winner (not the 1.75 High Value drag); directional read, confirm over ~a week.

## 2026-06-25 (action) — paused Sisters First Test - Copy; diagnosed Mission Call bait failure; scaling deferred

**Account changes executed (Graph API):**
- **Paused `Sisters First Test - Copy`** (ad 120243000099550733, May Lower Spend / Low Value
  Customers). Ran proposed → approved → validate → execute; Meta returned `{"success": true}`,
  confirmed `status=PAUSED`.
  - *Why:* $128.59 spend, **0 results**, ROAS 0.00 over its run — clean waste, below the 1.50
    pause floor. The one fully-grounded executable pause in the action plan.

**Diagnosed (no new action — already paused):**
- **`Mission Call Opening - He Lives` confirmed PAUSED** (it was ACTIVE on 06-24; retired since).
  Diagnosed *why* it failed beyond the creative: it pulled **CTR 3.39% / outbound 2.16%
  (~2.4–2.5× the account median) at a cheap $0.83 CPC**, but converted only **0.4% of link
  clicks (533 clicks → 2 purchases)** on $443.92 spend. CPM only mildly elevated ($18.01 vs
  $15.79 median), so Meta had not heavily penalized it yet. Signature of a
  **curiosity/outrage-bait creative**: magnetic at the top of funnel, but the clicks are
  low-intent (curiosity/irritation) and don't convert — amplified by serving to the warm
  Engaged Audience. The "make people mad" risk would surface later as rising CPM.
  - *Caveat:* conversion sample is thin (2 purchases); the click-side signal is conclusive.
    **Confirm via A/B** (bait vs. straight creative, measured on purchases, not clicks).

**Deferred (deliberately — no writes):**
- **Scaling the seasonal winners — NOT done.** BYU Football / Easter / Easter Video 3 carry
  strong *historical* ROAS (11.79 / 5.42 / 6.50), but their campaigns (Byu, Younger Audience,
  Easter Campaign) are all **PAUSED with 0 active ads** (seasonal). Raising budget on a paused
  campaign is a no-op. Also: these use **CBO (campaign-level budgets)** — Byu $100/day, Younger
  Audience $50/day, Easter Campaign $80/day — so the tool's ad-set-level `increase_adset_budget`
  targets the wrong level here (logged as a tool gap to fix). Follow-up logged to scale +15% at
  the **campaign** level on relaunch (due 2026-08-01), re-checking current ROAS first.
- **Creative refreshes** for 5 fatigued ads (White BG V2 - Copy, BYU Ad (updated), New Black,
  Still Black - Copy, Missionaries - Copy 3) need new creative assets, not an API toggle.
  Follow-up logged (due 2026-07-09).

**Data:** synced a 12-month window (data effectively starts 2025-11-14 → 156 active days).
Blended ROAS **3.29** over $63.9K spend / $210K purchase value, 85 ads / 22 ad sets / 13 campaigns.

## 2026-06-24 (action) — placement policy applied to High Value Customers; Selfie Mom unblocked

- **Verified at the ad-set level first** (120d, High Value Customers specifically, not account-wide):
  Feed+Reels+Stories = 96% of spend / **99% of value** (1.82 ROAS); the dropped placements = 4% of
  spend / **1% of value at 0.32 ROAS** (mostly FB in-stream video, money-losing). So fix #2 is a net
  positive, not a sacrifice.
- **Applied:** set High Value Customers (120245034013770733) to manual placements
  (FB/IG Feed + Reels + Stories) and enabled `Selfie Mom - Copy` (120246062743790733).
  Result: Selfie Mom **ACTIVE / IN_PROCESS, "No Valid Formats" cleared.**
- Side effect (accepted): the 4 sibling Test-Selfie OG ads re-learn under the new placements; the ad
  set was already soft/mid-relearn, so expect a few noisy days. Lesson reinforced: constraining
  placements to Feed+Reels+Stories fixes "No Valid Formats" for single-video ads.

## 2026-06-24 (status check) — Mission Call ad delivering; recent window soft (learning churn)

- Mission Call ad is **ACTIVE/delivering** ($88/2d, 0 purchases — <1 day live, no signal yet).
- 30d ROAS **2.40** (≈flat). 7d ROAS **1.77** (down from 1.93); **Engaged Audience 7d = 1.26** vs
  its 30d 3.61 — best ad set running cold *recently*. Attributed to stacked changes over the prior
  days (AA-off, mass re-enable, renames, creative-features edit, new ad) all resetting learning;
  likely transitional. **Hold judgment to the 06-30 eval; watch Engaged's recovery.**
- Only open delivery issue: `Selfie Mom - Copy` (No Valid Formats, still pending).

## 2026-06-24 — Mission Call ad ENABLED (live)

- Enabled the Mission Call ad (120246788494170733) → `status: ACTIVE` (eff IN_PROCESS = Meta
  reviewing the creative; delivers once approved). No issues; parent Engaged Audience ad set +
  campaign active. Operator (Van) approved enabling and stepped away.
- Final config as launched: copy = option 1 (missionary-gift, Elder/Sister capitalized) →
  shopdivinedesigns.com, Shop Now, additive creative enhancements ON / copy+AI OFF, single-text in
  Engaged Audience. Don't judge performance for ~5–7 days (learning phase, and the account is still
  re-stabilizing). The 2026-06-30 follow-up covers the evaluation.

## 2026-06-24 — built set_creative_features; applied default policy to the Mission Call ad

- Built a `set_creative_features` ops op (re-attaches the creative with a `degrees_of_freedom_spec`
  per-feature enroll, since creatives are immutable) + `propose-creative-features` CLI with an
  account default (additive ON, copy/AI OFF). Two API gotchas learned + handled: the umbrella
  `standard_enhancements` field is deprecated (set individual features), and read-back video_data
  carries both `image_hash` and `image_url` (must drop one). 69 tests pass.
- Applied it to the Mission Call ad (120246788494170733): 8 additive features OPT_IN
  (enhance_cta, image_brightness_and_contrast, inline_comment, product_extensions,
  reveal_details_over_time, show_destination_blurbs, show_summary, site_extensions); Meta opted out
  all others (incl. text_optimizations, replace_media_text). Verified. Ad still PAUSED (re-reviewing
  the new creative). Policy codified in profile.md.
- Follow-up added (due 2026-07-07): discuss a real on-vs-off A/B test to measure causal lift.

## 2026-06-23 — first pipeline ad created (PAUSED): Mission Call / He Lives

- Ran the video→ad pipeline end to end: `intake-video` on "mission call opening.MOV" (UGC mission-call
  reveal, vertical) → agent drafted 5 copy options grounded in `winning_copy.md` (missionary-gift +
  faith angles) → `upload-video` (video_id 2224235811675934) → created the ad **PAUSED**.
- **Path decision: B (single-text in the proven Engaged ad set), not a Dynamic Creative ad set.**
  Rationale: creative > audience > copy in impact; a DC ad set needs ~$150/day to learn and would
  add a fresh-learning silo mid-relearn; we already know the winning angles, so one strong text
  (option 1, missionary-gift) is an informed bet. Logged in learnings (revised "standard practice").
- Ad: `Mission Call Opening - He Lives (missionary gift)` (id **120246788494170733**) in Engaged
  Audience, → shopdivinedesigns.com, Shop Now, thumbnail = video's preferred frame. PAUSED / no issues.
- Follow-up created (due 2026-06-30): review/enable, then evaluate after ~1 week and consider A/B-ing
  the other 4 copy options. **Not yet enabled — awaiting operator review.**

## 2026-06-23 (decision) — placement policy: Feed + Reels + Stories, exclude FB in-stream

- **Decision:** keep the manual placement set at **Facebook/Instagram Feed + Reels + Stories** and
  **deliberately leave Facebook in-stream video OUT** (and all other non-core placements).
- **Why (120-day `publisher_platform,platform_position` data):** Feed+Reels+Stories hold **~98% of
  spend and value**; FB in-stream ran at **2.62 ROAS** (below the 3.25 account average) on only
  ~$10/day; Explore / Audience Network / Marketplace / right-column / Search / Messenger / Threads
  were ~0 spend, 0 conversions. So the exclusion costs ~nothing and slightly lifts blended ROAS.
- **Standard going forward:** use Feed + Reels + Stories (Instagram-leaning, vertical-first) as the
  **default placement set** for new/edited ad sets on this account unless data says otherwise.

## 2026-06-23 (later) — fixed "No Valid Formats" via placements (safe experiment)

- Ran a zero-spend experiment on the **paused** "Selfie" ad set (`120241591268250733`): set manual
  placements (FB/IG **Feed + Reels + Stories**) via `set_placements`, re-enabled `Cody` +
  `selfie FM SP` to force re-review. **Result: "No Valid Formats" cleared** (issues → none,
  effective_status IN_PROCESS). Confirms constraining placements fixes the single-video format
  incompatibility. **Re-paused both ads afterward** (turned off, per plan); ad set remains paused →
  no spend. The corrected placements are left in place (that's the fix).
- Built `set_creative` op (swap an ad onto a valid creative) as the other lever.
- **Still open:** `Selfie Mom - Copy` (active in High Value Customers) has the same issue and is
  NOT yet fixed — a placement change there would also affect its sibling Test-Selfie OG ads, so
  it needs a per-ad decision (pause it, or recreate the creative). `Cody` (historical 3.04) is now
  format-clean and could be revived into an active ad set when desired.

## 2026-06-23 (status check) — health restored, short learning-phase dip

- Dev-mode blocker fully cleared: **24 of 26 ads now ACTIVE** (most of the library re-enabled,
  operator-side), Advantage+ off on all 3 ad sets. Remaining issues: 3 ads with "No Valid
  Formats" (`Cody`, `selfie FM SP`, `Selfie Mom - Copy`) — a creative-format problem, not dev-mode.
- Performance: **7-day ROAS 1.93** vs 30-day 2.42 and the 3.0 target. Attributed to the
  learning-phase reset from the AA-off + renames + ~18-ad mass re-enable over the prior ~48h.
  Expect noise for a few more days before judging.
- Bright spot: **New Pink 3.20 (7d)** validating in Engaged Audience. Watch: `White BG V2 - Copy`
  dipped 5.63 → 1.33 (fatigue/relearn). Engaged Audience still best on 30d (3.72) — recommended
  home for the next ad.

## 2026-06-23 — re-enabled proven winners

- **Enabled `New Pink` (30d ROAS 3.08) and `New Black` (3.89)** in the Engaged Audience ad set
  via `apply-ops` (validate-only `{"success": true}` → execute). Both went `ACTIVE / IN_PROCESS`
  with no issues — confirms the dev-mode flag clears on re-review now that the apps are Live.
  Started narrow (winners only) to keep the Advantage+-off experiment readable.
- `White BG V2 - Copy` (5.37) was already re-reviewing on its own; left as-is.
- Deliberately left off: the 1.4–2.0 laggards (Test-Selfie OG cluster, Christian Blue Shirt),
  the `White BG V2` drag (1.85), the fatigued `Selfie FM - Copy`, and `Selfie Mom - Copy`
  (needs a tracking pixel first).
- **Next:** hold ~5–7 days, then `inspect` + `sync-api`/`report` and compare to the 2.43 baseline.

## 2026-06-22 (later) — dev-mode app blocker resolved at the root

- **Both business apps ("codex", "AI Ad Analysis") published to Live** (by operator, via the
  App Dashboard). Verified the fix: `White BG V2 - Copy` (previously blocked) re-reviewed and
  cleared to no-issues / `IN_PROCESS`. The other 17 blocked ads still show the **stale**
  dev-mode flag and will clear only when re-enabled/edited (re-review required).
- **Next:** selectively re-enable the proven winners (New Pink, White BG V2 - Copy, New Black,
  etc.) to clear them, then hold ~5–7 days and measure (the AA-off experiment).
- `Selfie Mom - Copy` still has a separate "Tracking Pixel Required" issue (needs a pixel).

## 2026-06-22

**Account changes executed (Graph API):**
- **Disabled Advantage Audience** on all 3 active ad sets (Engaged Audience, High Value
  Customers, Low Value Customers). Audiences preserved exactly; `advantage_audience=0` set
  and `age_range` dropped. Verified after: all 3 `advantage_audience: False`, still ACTIVE.
  - *Why:* Advantage+ was overriding the defined custom audiences (treating them as
    suggestions) and blocking audience edits, so no clean audience read was possible.
- **Renamed ad sets to match their audience:** `Selfie - Copy → High Value Customers`,
  `HV Cust Audience → Low Value Customers` (was mislabeled — it targets the low-value list),
  `Stills → Engaged Audience`. All validated + executed `{"success": true}`.

**Discovered (no action — needs human/Meta-side fix):**
- **Dev-mode app blocker.** Most paused ads, including the top performers (New Pink, White
  BG V2 - Copy, New Black), are `WITH_ISSUES` = "Ads creative post was created by an app
  that is in development mode" and cannot deliver until the app is set Live. Re-enabling
  them via the API will not help. `Selfie Mom - Copy` separately needs a tracking pixel.
- **Diagnosed deeper:** all 22 affected creatives are dark posts on a single Page
  (`766046059925199`) with API-generated post IDs → the posts were created programmatically
  via the Marketing API by a custom app, and that app is in Development mode. The business
  has two apps ("codex", "AI Ad Analysis"); the creating app couldn't be confirmed via API
  (reading a post's `application` field needs `pages_read_engagement`, which our token lacks).
  **Fix path:** set the offending app to **Live** in the App Dashboard
  (developers.facebook.com/apps → Settings→Basic needs Privacy Policy URL + Category + icon,
  then toggle App Mode to Live), or recreate the winning creatives natively in Ads Manager.
  The Business "Advertising settings → Apps" (app-install) page is unrelated.

**Analysis run (read-only):** full account analysis on the 30-day window (baseline in
`profile.md`). Blended ROAS 2.43 vs 3.0 target.

**Not done (deliberately):** no pauses, budget changes, ad enables, or audience rotation
executed. Audience rotation tooling is built and validated but deferred (see experiments.md).

**Tooling shipped this day:** repo migrated 100% to the Graph API (retired the `meta` CLI);
added rotation, in-place Advantage-Audience disable, ad set rename, and `--validate-only`
dry-test mode. All gated proposed → approved → validate-only → execute.
