# Divine Designs — decision log

Append-only, dated. Newest first. Record every change to the live account + the reason + result.

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

**Analysis run (read-only):** full account analysis on the 30-day window (baseline in
`profile.md`). Blended ROAS 2.43 vs 3.0 target.

**Not done (deliberately):** no pauses, budget changes, ad enables, or audience rotation
executed. Audience rotation tooling is built and validated but deferred (see experiments.md).

**Tooling shipped this day:** repo migrated 100% to the Graph API (retired the `meta` CLI);
added rotation, in-place Advantage-Audience disable, ad set rename, and `--validate-only`
dry-test mode. All gated proposed → approved → validate-only → execute.
