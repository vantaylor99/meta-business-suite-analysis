# Divine Designs — account profile

- **Meta ad account:** `act_1506116630658438`
- **Primary goal:** **3.0 blended ROAS account-wide.** (config policy: `primary_goal: roas`,
  `target_roas: 3.0`, `pause_roas_floor: 1.5`, `scale_roas_floor: 3.0`,
  `max_budget_increase_percent: 20`.)
- **Default destination URL:** **https://www.shopdivinedesigns.com** — every ad points here
  unless we deliberately decide to deep-link to a specific product page (a future test). Also in
  `config/meta_ads_accounts.json` as `default_destination_url`.
- **Creative-enhancements policy (2026-06-24):** additive/visual enhancements **ON**, everything
  else **OFF**. Default OPT_IN set = `enhance_cta, inline_comment, show_summary,
  show_destination_blurbs, reveal_details_over_time, site_extensions, product_extensions,
  image_brightness_and_contrast`; OPT_OUT the rest, especially **`text_optimizations`** (rewrites
  our tested copy) and `replace_media_text`. Apply with `propose-creative-features` →
  `apply-ops` (defaults live in `control.DEFAULT_OPT_IN_FEATURES/DEFAULT_OPT_OUT_FEATURES`). This is
  research- + correlation-backed, not A/B-proven — see the open follow-up to run a real test.
  (Distinct from Advantage+ **Audience**, which stays off — see learnings.)
- **Copy conventions:** **"Elder" and "Sister" are always capitalized** (missionary titles). Ship
  every ad with **up to 5 primary texts (+ up to 5 headlines / 5 descriptions)** and let Meta
  optimize among them, then prune to the winner after ~1 week (see ad_copy_best_practices.md).
- **Measurement:** optimize on purchases (Website purchases) first; ROAS is the headline
  metric but is partly derived (see learnings). Outbound clicks are a diagnostic secondary
  signal only.
- **Pixels (as of 2026-06-23, via `list-pixels`):** two exist — `Shopify Store Pixel`
  (2263151130774482, firing) and `Devine Designs` (1117839580144872, **never fired** — note the
  misspelling). 0 custom conversions. The active/correct pixel is the Shopify one; the
  `Selfie Mom - Copy` "Tracking Pixel Required" issue likely stems from a missing/incorrect pixel.

## Structure (after 2026-06-22 cleanup)

Three **active** ad sets, each a distinct audience, mutually exclusive (each excludes the
other two audiences and their lookalikes). Advantage Audience is now **off** on all three.

| Ad set (current name) | Audience (seed + lookalike) | Notes |
| --- | --- | --- |
| **Engaged Audience** `120241592681330733` | engaged-audience + 5% LAL | Best ROAS (3.74) and highest AOV (~$67). |
| **High Value Customers** `120245034013770733` | high-value-customers + 1% LAL | Weakest active ROAS (1.78) — but ran the weak "Test - Selfie OG" creatives, so audience-vs-creative is confounded. |
| **Low Value Customers** `120242997920660733` | low-value-customers + 1% LAL | Largest spend share (~45%); ROAS 2.04. Was mislabeled "HV Cust Audience". |

~15 other ad sets exist but are paused/archived (legacy tests, Easter, Temples, etc.).

## Performance baseline — 30 days ending 2026-06-22

**Rot:** fast · **Verified:** 2026-06-22  _(these are live-account numbers — `lint-vault` flags them
for re-verification once they age past `KNOWLEDGE_REVERIFY_DAYS`; refresh with `account_metrics` and
bump the date when reconfirmed)_

- Spend **$18,478** → purchase value **$44,865** → **blended ROAS 2.43** (target 3.0).
- 843 purchases, ~$53 AOV, cost/result $21.92. (Report: `reports/divine_designs/2026-06-22/`.)
- **ROAS by band:** ≥3.0 = 27% of spend @ 3.69 · 1.5–3.0 = **63% of spend @ 2.08** (the core
  gap) · 1.0–1.5 = 7% @ 1.45 · <1.0 = 3% @ 0.76.
- **Campaigns:** 100K (2.81) > May Lower Spend (2.10) > New Temple w/ HV (1.09, tiny).
- To reach 3.0 at current spend, value must reach ~$55.4k (+24%). The account already proves
  3.0 is reachable (27% of spend runs at 3.69; the engaged ad set at 3.74).

## Active blocker (as of 2026-06-22)

Most of the proven creative library — **New Pink (3.08), White BG V2 - Copy (5.37), New
Black (3.89)** and many others — is **blocked** with `WITH_ISSUES` / "app in development
mode" and cannot deliver until the Facebook App is set Live (Meta App Dashboard; not fixable
via the ads API). The account is currently running on only the few ads not tied to that app
(Missionaries Copy/2/3, Cody - Copy, Test - Selfie OG, Sisters First Test - Copy). One ad
(`Selfie Mom - Copy`) is separately blocked by "Tracking Pixel Required".

## The path to 3.0 (analysis 2026-06-22)

1. **Unblock the dev-mode app** — without this, the best creatives can't run; everything else
   is secondary.
2. **Cut genuine waste** (~$1,877 below the 1.5 pause floor; New Temple/Temples @ 1.09).
3. **Biggest single lever:** `White BG V2` — one ad, ~21% of spend at 1.85 ROAS. Refresh or
   reallocate; its sibling `White BG V2 - Copy` does 5.37.
4. **Refresh fatiguing winners** (White BG V2 - Copy ROAS −60% / freq +34%; New Pink; Cody -
   Copy; Selfie FM - Copy) before they decay further.
5. **Reallocate** ~$5–7k from sub-2.0 inventory into proven 3.0+ inventory (engaged audience,
   New Pink, Cody, New Black). Rough math: $6k shifted from ~1.9 to ~3.5 ≈ +$9.6k value ≈ 2.95.
