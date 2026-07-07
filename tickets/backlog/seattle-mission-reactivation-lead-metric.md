description: The Washington Seattle Mission lead-gen account is marked "paused" in our config but is actually spending again, and we can't read its cost-per-lead because the lead result type isn't wired up — so the account can't be graded on its goal.
prereq:
files: config/meta_ads_accounts.json, src/meta_ads_analysis/cli.py
difficulty: medium
----

## Problem

`config/meta_ads_accounts.json` marks `seattle_mission` (ad_account_id `103014553`) as
*"Currently PAUSED (no spend in the last 30 days)."* As of 2026-06-29 a live read shows it spent
**~$1,177 in the last 30 days** and the account is ACTIVE — the note is stale.

Worse, `results` / cost-per-lead come back **"Not available"** on the read, so the account cannot be
graded against its goal (cost-per-lead vs `target_cost_per_result` $10 / `pause_cost_per_result_above`
$40). The config note already anticipated the cause: the instant-form lead action type may resolve as
`leadgen_grouped` or `onsite_conversion.lead_grouped`, and needs verifying against a real read.

## Scope / what "done" looks like

- Confirm the account's live status and recent spend (it is active again, not paused).
- Update the `seattle_mission` config entry so it reflects reality — correct/replace the stale
  "paused" note and record current status.
- Resolve `primary_result_action_type` so Results and cost-per-lead populate for this account
  (verify `leadgen_grouped` vs `onsite_conversion.lead_grouped` against a live read), wiring it through
  whatever produces the grade.
- A goal-aware grade can be produced: cost-per-lead vs `target_cost_per_result` ($10) /
  `pause_cost_per_result_above` ($40). `roas_role` is `not_applicable` — never apply ROAS to this account.

## Notes

- Lead-gen via Meta Instant Forms (objective OUTCOME_LEADS); leads captured on-platform, so there is
  no website destination or offsite-checkout requirement.
- Historical: ~$98k spent / 12,600+ form leads across 2024–2025 at ~$25–30 cost-per-lead.
- Surfaced by the 2026-06-29 daily-overview check (read-only MCP pull), which caught both the stale
  config note and the missing lead metric.

**Update 2026-07-07:** the config-note portion of this ticket is done — `notes` now says ACTIVE
with current spend context (van, interactive session, not this pipeline). The `results`/cost-per-lead
"Not available" bug from `ads_get_ad_entities` **is still open** and is the remaining scope here:
requesting `lead` as an explicit field works and returns real counts (verified 2026-07-07: e.g. 12
leads on 7/1, 12 on 7/4), but `results`/`cost_per_result` still come back "Not available" — so
whatever produces the goal-aware grade (`primary_result_action_type` resolution) still needs the
fix described above. New account context now lives in `knowledge/accounts/seattle_mission/`
(profile.md, decision-log.md) — read those first, they cover current status and the recent
cost-per-lead improvement (attributed to an audience-size expansion by account manager Hunter
Johanson around 7/1).
