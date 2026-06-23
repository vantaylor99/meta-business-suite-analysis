# Divine Designs — decision log

Append-only, dated. Newest first. Record every change to the live account + the reason + result.

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
