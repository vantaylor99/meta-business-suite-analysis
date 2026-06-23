# Durable learnings

Cross-account, long-lived lessons. Date-stamped so staleness is visible. Newest first.
If a fact here turns out wrong, fix or delete it — don't let it rot.

## Meta platform & API behavior

- **(2026-06-22) "App in development mode" silently kills delivery.** Ads whose creative
  post was created by a Facebook App still in *Development* mode show `effective_status =
  WITH_ISSUES` with issue `"Ads creative post was created by an app that is in development
  mode"` and **cannot deliver**. This is independent of audience, budget, Advantage+, or
  ad status — toggling the ad ACTIVE does nothing. Fix is in the Meta **App Dashboard**
  (set the app Live / publish it) or recreate the posts under a published app/page. The
  ads API cannot fix it. Always check `issues_info` before assuming a paused ad can just be
  re-enabled.
- **(2026-06-22) Advantage+ Audience confounds and blocks audience work.** While an ad set
  has `targeting_automation.advantage_audience = 1`: (a) Meta **rejects** any change to
  `custom_audiences` / `excluded_custom_audiences` ("can't be changed because your campaign
  uses Advantage+ audience"), and (b) it treats your defined audience as a *suggestion* and
  expands delivery beyond it. So any "is this audience good?" test is meaningless until AA
  is off. To disable: set `advantage_audience = 0` **and drop the `age_range` field**
  (Meta error subcode 1487079: "targeting_automation must be enabled to use age_range").
  `age_min`/`age_max` stay.
- **(2026-06-22) Targeting edits trigger transient re-review.** Right after a write that
  changes targeting, an ad set briefly drops out of `effective_status = ACTIVE` (Meta
  re-reviews) and can be under-counted by `effective_status=["ACTIVE"]` listing. Settles
  back within ~a minute. Re-read by ID to confirm.
- **(2026-06-22) `validate_only` is honored on ad set updates.** Sending
  `execution_options=['validate_only']` validates the payload and returns the real
  success/error **without persisting** — the safe way to dry-test a write. Exposed as the
  `--validate-only` flag on the apply commands.
- **(2026-06-22) Token scope:** the active `META_ACCESS_TOKEN` has `ads_management`
  (writes work). Reads/dry-runs only need `ads_read`. The repo is 100% Graph API (the
  Linux/macOS-only `meta` CLI was retired), so everything runs natively on Windows.

## Advertising / measurement principles (learned on these accounts)

- **(2026-06-22) Audience and creative are confounded by default.** Each ad set carries a
  different audience *and* a different creative mix, so a low-ROAS ad set can't be blamed on
  the audience without a controlled swap. The `propose-rotation` tooling exists to swap
  audiences while holding creatives constant to isolate this.
- **(2026-06-22) Average order value varies by audience, not just CPA.** On Divine Designs
  the engaged audience showed both a lower cost-per-result *and* a higher AOV (~$67 vs ~$47),
  so audience choice moves ROAS through two levers at once.
- **(2026-06-22) ROAS here is often derived, not Meta-reported.** Many ads lack a direct
  `purchase_roas` field, so ROAS is computed from purchase value / spend. Treat second-decimal
  precision as directional.

## What the tooling can do (so you don't rebuild it)

All write paths follow the same gate: `proposed → approved (edit the plan JSON) → --validate-only
(optional real dry test) → --execute`. Commands (via `python -m meta_ads_analysis <cmd>`):

- `sync-api` — pull insights/ads from the Graph API; `report` — build the analysis report.
- `propose-actions` / `apply-actions` — pause underperformers, capped ad set budget increases.
- `propose-rotation` / `apply-rotation` — rotate custom audiences across ad sets (optional
  `--disable-advantage-audience`).
- `propose-disable-advantage` / `apply-disable-advantage` — turn Advantage Audience off in
  place, audiences preserved.
- `propose-renames` / `apply-renames` — rename ad sets to match their current audience.
- There is **no** "enable ad" / creative-create / app-mode write path yet.
