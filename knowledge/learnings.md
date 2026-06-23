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
- `propose-actions` / `apply-actions` — pause underperformers, capped ad set budget increases.
- `propose-rotation` / `apply-rotation` — rotate custom audiences (optional `--disable-advantage-audience`).
- `propose-disable-advantage` / `apply-disable-advantage` — turn Advantage Audience off in place.
- `propose-renames` / `apply-renames` — rename ad sets to match their current audience.
- `propose-enable-ads` — propose enabling currently-inactive ads (filter by `--adset-id` / `--name-contains`).
- `apply-ops` — **generic guarded executor** for an ops plan: `set_status` (ACTIVE/PAUSED at
  ad/adset/campaign), `set_daily_budget` (adset/campaign, capped vs current), `rename` (any level).
  An agent can author its own `ops_plan.json` (ops with `status: approved`) and run this. Guardrails:
  per-op approval, budget-increase cap, no Meta-AI/Advantage params.
- **Deliberately NOT built** (destructive / out of scope): delete, archive, creating new
  campaigns/ad sets/ads, and arbitrary targeting edits (targeting has its own rotation tools).
