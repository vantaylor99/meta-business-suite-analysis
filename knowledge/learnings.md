# Durable learnings

Cross-account, long-lived lessons. Every entry carries a **confidence level** backed by a
dated **evidence log**, so confidence grows as evidence repeats and shrinks when something
contradicts it. See "Confidence & evidence" in `README.md` for the rubric and how levels move.
Record observations early (even at 🔴 Low) — let the evidence move them.

Legend: 🟢 High · 🟡 Medium · 🔴 Low · trend ↑ rising / → stable / ↓ falling.

## Meta platform & API behavior

### Ads whose creative post was made by a dev-mode app can't deliver (WITH_ISSUES)
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-22 — ~20 paused ads across all 3 active Divine Designs ad sets showed
  `issues_info = "Ads creative post was created by an app that is in development mode"`;
  matches documented Meta behavior. _(direct API observation; divine_designs)_
- ➕ 2026-06-22 — all 22 affected creatives are dark posts on one Page (`766046059925199`)
  with API-generated post IDs → created programmatically by a custom app in Dev mode. The
  creating app couldn't be confirmed via API (post `application` field needs
  `pages_read_engagement`; our token lacks it). _(API observation; divine_designs)_
- ➕ 2026-06-22 — after both business apps ("codex", "AI Ad Analysis") were published to Live,
  the one previously-blocked ad that got re-enabled (`White BG V2 - Copy`) cleared to
  no-issues / `IN_PROCESS`; the other 17 still show the stale flag because they have not been
  re-reviewed. **Confirms: publishing the app fixes it, but the WITH_ISSUES flag only clears
  on re-review — re-enable/edit each blocked ad to trigger it.** _(direct verification; divine_designs)_

**Apply:** Check `issues_info` before assuming a paused ad can be re-enabled. The fix is in the
Meta App Dashboard (publish the app / set it Live — needs Privacy Policy URL + Category first),
or recreate the creatives natively in Ads Manager. Not fixable via the ads API, and unrelated
to the Business "Advertising settings → Apps" (app-install) page. To confirm the creating app
via API you'd need a token with `pages_read_engagement`.
**Key nuance:** a **Development-mode app can still fully manage your own ad account** via the
API (reads + pause/budget/targeting) — Dev mode is fine for *control*. Live mode is needed
*only* so posts the app *creates* can be served publicly, and **Live mode without App Review
stays private to your own business** (no public users, no review needed). Corollary: if you'll
create future ads *via the API*, the app must be Live; if ads are created in Ads Manager and the
app only manages them, Dev mode is fine indefinitely.
**Would lower:** an ad with this exact issue delivering anyway, or the flag clearing with no app change.

### While Advantage+ Audience is on, custom-audience edits are rejected and the audience is only a suggestion
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-22 — validate-only POST to change `custom_audiences` was rejected on all 3 ad sets
  ("can't be changed because your campaign uses Advantage+ audience"). _(direct API response; divine_designs)_

**Apply:** Disable Advantage Audience before any audience change or audience test.
**Would lower:** a successful custom-audience edit on an Advantage-Audience-on ad set.

### Disabling Advantage Audience requires also dropping `age_range`
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-22 — `advantage_audience=0` with `age_range` present → HTTP 400 subcode 1487079
  ("targeting_automation must be enabled to use age_range"); dropping `age_range` then validated
  `{"success": true}` on all 3 ad sets. _(direct API response + re-verified fix; divine_designs)_

**Apply:** When disabling AA, drop `age_range` (keep `age_min`/`age_max`). Done in `compute_new_targeting`.

### `validate_only` is honored on ad set updates (a safe dry-test)
**Confidence:** 🟡 Medium ↑  ·  **Domain:** platform
- ➕ 2026-06-22 — reversible name probe: a validate-only "rename" returned success but the live
  name was unchanged on re-read → no mutation. Matches documented `execution_options` behavior.
  _(direct verification, n=1; divine_designs)_

**Apply:** Use `--validate-only` to get Meta's real verdict before `--execute`.
**Would raise:** a few more clean uses with no surprise mutation → High.  **Would lower:** any
validate-only call that actually changes state.

### Creative *enhancements* ≠ Advantage *Audience*; our winners ran with enhancements ON
**Confidence:** 🟡 Medium →  ·  **Domain:** strategy
- ➕ 2026-06-24 — pulled `degrees_of_freedom_spec` on active Divine Designs ads: the proven
  performers (White BG V2 - Copy, New Pink, New Black, Cody - Copy, Missionaries Copy/2…) all ran
  with **many creative enhancements OPT_IN** — `standard_enhancements`, `enhance_cta`,
  `inline_comment`, `show_summary`, `reveal_details_over_time`, `site_extensions`,
  `product_extensions`, `image_brightness_and_contrast`, `video_filtering`, some with
  `replace_media_text` / `description_automation`. The account did well (2.4–3.25 ROAS) WITH these on.
  The new API-created ad has ALL of them off (API default without opt-in). _(own-account read;
  enhancement *lift* is NOT cleanly measurable — no per-feature insights breakdown; confounded.)_
- ➕ 2026-06-24 — practitioner consensus (Jon Loomer, Metalla, Meta Help): keep **additive/visual**
  enhancements (enhance CTA, comments, summaries, brightness/contrast, image expansion — Meta cites
  ~4% lower cost/result for standard enhancements); **turn OFF copy-rewriting "Text Improvements"**
  (it moves/rewrites your primary text/headline and can truncate, breaking tested copy) and be
  cautious with video touch-ups if the creative is intentional.

- ➕ 2026-06-24 — correlational join (30 ads, ≥$100 spend, 120d, spend-weighted ROAS): the
  additive/essential cohort trends positive — `enhance_cta` 3.98 vs 2.96 without, brightness 5.90
  vs 2.92 (n=6), `inline_comment` 3.56 vs 2.74, essentials-on 3.40 vs essentials-off 2.93. A few
  trend negative (`ig_video_native_subtitle` 2.06, `video_filtering` 2.78) but those almost
  certainly ride on weaker video ads — **confounded, small samples, NOT causal**. Consistent with
  "keep additive on, don't blanket-disable." Causal proof needs an A/B test. _(own-account read)_

**Apply:** Two *different* axes that the old "disable_meta_ai_features" stance conflated:
1. **Advantage+ AUDIENCE (targeting)** — correctly OFF (it overrode our custom audiences; proven).
2. **Advantage+ CREATIVE enhancements** — do NOT blanket-disable. Match what works: keep the
   additive/essential ones ON; keep **Text Improvements OFF** (protect our `winning_copy` texts);
   A/B test visual ones if we want rigor. Don't ship new ads all-off as the lone outlier.
**Would raise:** a clean on-vs-off A/B test quantifies the lift.

### Multiple text options (asset_feed_spec) = "Dynamic Creative" → needs its own ad set (max 1 ad)
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-23 — creating a video ad with 5 primary texts (`asset_feed_spec.bodies`) into the
  existing **Engaged Audience** ad set failed: *"Dynamic Creative Ad Set allows at most one active
  ad in it"* (subcode 1885553). Also: including the **deprecated `standard_enhancements`** opt-out
  field is rejected (*"should not include standard enhancements… set individual features instead"*,
  subcode 3858504) — so omit it. _(direct API diagnosis; divine_designs)_

**Apply:** To ship multiple operator-written text options (Meta optimizing among them, then prune to
the winner — the desired workflow): create a **dedicated Dynamic Creative ad set**
(`is_dynamic_creative: true`) that holds that **single** multi-text ad. To put an ad into an
existing shared ad set instead, it must be **single-text** (object_story_spec); run more variants
as separate single-text ads if you want to A/B inside a normal ad set. `create_video_ad` with
`primary_texts` builds the asset_feed_spec creative; it requires a DC ad set as the home.

### "No Valid Formats" = creative incompatible with the selected placements
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-23 — 3 Divine Designs video ads (`Selfie Mom - Copy`, `Cody`, `selfie FM SP`) showed
  `WITH_ISSUES` / "No Valid Formats: your creative is incompatible with the selected placements."
  All were single-video creatives running under **automatic (Advantage+) placements** — the video
  didn't satisfy every placement in the set and there was no format fallback. _(direct API diagnosis)_

- ➕ 2026-06-23 — **fix confirmed live:** setting the paused "Selfie" ad set to manual placements
  (FB/IG Feed + Reels + Stories) and re-reviewing cleared "No Valid Formats" on `Cody` +
  `selfie FM SP` (issues → none). _(controlled experiment; divine_designs)_

**Apply:** Meta's own two fixes — **change placements** (`set_placements` to a video-friendly
manual set: Facebook/Instagram Feed + Reels + Stories — verified to work) or **change the creative**
(`set_creative` to a valid creative; creatives are immutable so you swap, not edit). Note
`set_placements` is ad-set-level (affects sibling ads). Confirm by applying and re-reading
`issues_info` (validate-only checks write validity, not delivery eligibility).

### Targeting edits trigger a transient re-review (ad set briefly leaves ACTIVE)
**Confidence:** 🟡 Medium →  ·  **Domain:** platform
- ➕ 2026-06-22 — right after disabling AA, an `effective_status=["ACTIVE"]` listing returned only
  1 of 3 ad sets; all 3 read ACTIVE again minutes later. _(single observation; divine_designs)_

**Apply:** After a targeting write, re-read by ID or wait ~a minute before trusting an ACTIVE filter.
**Would raise:** the same dip-then-recover on later edits.  **Would lower:** edits with no status blip.

### Token has `ads_management`; repo runs 100% on the Graph API
**Confidence:** 🟢 High →  ·  **Domain:** platform
- ➕ 2026-06-22 — write calls accepted; the Linux/macOS-only `meta` CLI was fully retired.

**Apply:** Writes run natively on Windows; reads need only `ads_read`.

## Advertising & measurement

### Audience and creative are confounded unless deliberately separated
**Confidence:** 🟢 High →  ·  **Domain:** strategy (methodological)
- ➕ 2026-06-22 — each Divine Designs ad set pairs a distinct audience with a distinct creative
  mix, so ad-set ROAS can't be attributed to audience vs creative. _(structural reasoning)_

**Apply:** To attribute audience effects, hold creatives constant and swap audiences (rotation).

### Engaged audience may carry higher AOV and ROAS than the high/low-value lists
**Confidence:** 🔴 Low ↑  ·  **Domain:** strategy
- ➕ 2026-06-22 — 30-day window: engaged ad set 3.74 ROAS / ~$67 AOV vs low-value 2.04 / ~$47.
  _(single window, cross-sectional, **confounded** by creative mix + Advantage+ being on — weak)_

**Apply:** Treat as a hunch only; do not reallocate heavily on this alone yet.
**Would raise:** it persists after AA-off with the full creative set, or holds in a controlled
rotation (same creatives, swapped audience).  **Would lower:** it flips once creatives/AA are equalized.

### Divine Designs: Instagram (esp. Stories) outperforms Facebook; value concentrates in Feed/Reels/Stories
**Confidence:** 🟡 Medium ↑  ·  **Domain:** strategy
- ➕ 2026-06-23 — 30d `metrics --breakdown`: Instagram 2.79 ROAS vs Facebook 1.94; by `age`,
  18-24 (3.18) and 45-54 (2.93) led, 55-64 (1.41) / 65+ (1.57) lagged. _(30d, account-wide, confounded)_
- ➕ 2026-06-23 — **120d** `publisher_platform,platform_position` (corroborates + adds detail):
  Instagram 3.63 vs Facebook 2.55; **IG Stories is the single best placement at 4.33 ROAS** ($60k
  value), then IG Reels 3.30, IG feed 3.25, FB feed 2.66, FB Reels 2.38. **~98% of spend AND value
  sits in Feed + Reels + Stories**; Explore / Audience Network / Marketplace / right-column / Search /
  Messenger / Threads were ~0 spend and 0 conversions. Only notable non-core placement: FB
  in-stream video (2.62 ROAS, ~$1.3k/120d). _(120d, account-wide; auto-placement-influenced)_

**Apply:** Lean Instagram-heavy and vertical-first (Stories/Reels) for new ad sets/creative.
**Adopted default placement set for this account: Facebook/Instagram Feed + Reels + Stories,
with FB in-stream video deliberately excluded** (2026-06-23 decision — see decision-log; the
exclusion costs ~nothing and slightly lifts ROAS). Consider de-weighting 55+ and Facebook.
**Would raise:** holds in a controlled per-ad-set test; **would lower:** Facebook/other placements
gain share once creative is tailored to them.

### ROAS on this account is partly derived, not Meta-reported
**Confidence:** 🟢 High →  ·  **Domain:** measurement
- ➕ 2026-06-22 — 30 ads lacked a direct `purchase_roas` field; ROAS is computed from value/spend.

**Apply:** Treat second-decimal ROAS as directional; don't over-fit decisions to tiny differences.

## Tooling capabilities (factual reference — not a probabilistic claim)

All write paths follow the same gate: `proposed → approved (edit the plan JSON) → --validate-only
(optional real dry test) → --execute`. Commands (`python -m meta_ads_analysis <cmd>`):

- `sync-api` (pull insights/ads) · `report` (build analysis).
- `inspect` — **read-only situational-awareness snapshot**: campaign→ad set→ad tree with status,
  effective_status, delivery issues, budgets, and audiences + rollups. Writes `account_snapshot.json`.
- `metrics` — **live per-entity performance** (ROAS / spend / purchases / CPP) over a window at
  account/campaign/adset/ad level (`--level`, `--date-from/--date-to`). On-demand, no CSV pipeline.
- `diagnose` — **account-wide delivery-issue scan**, grouped by issue (the dev-mode-app finder).
- `watch` — **read-only runaway/outlier scanner**: flags ads spending while underperforming
  (urgent/underperforming/watch), with a significance floor + a protective grace that never flags
  ads created/changed within ~5 days for killing (uses `updated_time`, so mid-relearn ads are safe).
  Persistent watchlist tracks consistency. Flag-only — AI/human decides, pauses via the guarded flow.
- `experiment define|list|readout` — **A/B experiment harness** that turns opinions into evidence.
  `define` records a test (hypothesis, the ONE variable changed, control vs variant entity ids, a
  window) at `knowledge/accounts/<slug>/experiments/<id>.json` so the record travels with the repo.
  `readout` pulls both arms live, compares ROAS, runs a two-proportion z-test on conversion rate
  (purchases/impressions, pure-Python via `math.erf`), and gates with a `--min-conversions` (default
  25) "needs more data" floor so we don't call a winner early. **Setup of the two arms reuses
  existing tools** — `propose-duplicate-ad` to clone, then `apply-ops set_creative_features` /
  `set_placements` to flip the single variable; for a zero-overlap audience split Meta's native
  split-test is more rigorous, this is a pragmatic in-repo A/B (directional→solid by isolation).
  Caveat surfaced in the readout: significance is on conversion-rate; arms sharing an ad set compete.
  `readout` accepts `--json-output-path <file>` to also persist the full result dict as JSON (useful for dashboards/automation).
- `list-audiences` — **custom-audience inventory** (id, name, subtype, size, status).
- `account-info` — account status, currency, lifetime spend, spend cap, balance, funding source.
- `metrics --breakdown <dim>` — performance split by age, gender, country, region,
  publisher_platform, platform_position, impression_device, device_platform, etc.
- `estimate --adset-id <id>` — estimated reachable audience size (MAU range) for an ad set.
- `search-interests --query <kw>` — discover detailed-targeting interests (id, name, size).
- `list-pixels` — account pixels (+ last fired) and custom conversions.
- `copy-library` — pulls top-ROAS ads + their actual primary text/headline/description into a
  proven-winner swipe file at `knowledge/accounts/<account>/winning_copy.md` (the base for writing
  new ad copy; git history keeps the record over time).
- `propose-actions` / `apply-actions` — pause underperformers, capped ad set budget increases.
- `propose-rotation` / `apply-rotation` — rotate custom audiences (optional `--disable-advantage-audience`).
- `propose-disable-advantage` / `apply-disable-advantage` — turn Advantage Audience off in place.
- `propose-renames` / `apply-renames` — rename ad sets to match their current audience.
- `propose-enable-ads` — propose enabling currently-inactive ads (filter by `--adset-id` / `--name-contains`).
- `propose-pause-ads` — propose pausing ACTIVE ads by filter and/or a performance rule
  (`--roas-below` + `--min-spend` over a window, pulled live). Executes via `apply-ops`.
- `apply-ops` — **generic guarded executor** for an ops plan. Ops: `set_status` (ACTIVE/PAUSED at
  ad/adset/campaign), `set_daily_budget` (adset/campaign, capped vs current), `rename` (any level),
  **targeting ops** (adset, read-modify-write the full targeting spec): `set_age_range`,
  `set_genders`, `set_geo_locations`, `set_placements` (manual placements or `{automatic:true}` for
  Advantage+ placements), and `set_creative` (ad — point an ad at a different/valid creative_id, the
  way to "fix"/swap an ad's creative since creatives are immutable). An agent can author its own
  `ops_plan.json` (ops with `status: approved`)
  and run this. Guardrails: per-op approval, budget-increase cap, no Meta-AI/Advantage params,
  targeting ops never modify `targeting_automation`.
- `propose-duplicate-ad` / `propose-lookalike` / `propose-video-ad` / `apply-authoring` —
  **authoring layer**: create campaigns/ad sets/ads/**video ads** (everything created **PAUSED**),
  duplicate an existing ad's creative into another ad set, create lookalike audiences. Same
  approval/validate-only/execute gate; rejects Meta-AI/Advantage params.
- **Video → ad pipeline** (`intake-video` → agent drafts copy → `upload-video` → `propose-video-ad`
  → `apply-authoring`): drop a video in `data/video_intake/<account>/inbox/`, transcribe locally
  (faster-whisper, the `media` extra + ffmpeg), the agent writes 5 copy options + picks the ad set,
  then a PAUSED video ad is created. Full flow in `knowledge/video_ad_pipeline.md`; copy guidance in
  `knowledge/ad_copy_best_practices.md`.
- **Deliberately NOT built** (destructive / out of scope): delete, archive, creative/media upload
  (duplicate-ad reuses an existing creative instead), and arbitrary targeting edits (targeting has
  its own rotation tools).
