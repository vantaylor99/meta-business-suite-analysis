description: A tool that shows which ads are actually winning or losing across the accounts you manage — ranked by spend, results, and cost-per-result — looking only at ads that ran in the chosen window, so it stays fast instead of walking every ad ever created in each account.
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/mcp_server.py, tests/test_meta_ads_analysis.py
difficulty: hard
----
## Opportunity

There is no ad-level cross-account view today — everything is account-level. A specialist's real
question is *"which specific ads/creatives across my accounts should I pause or scale?"*

## THE load-bearing design constraint (read this first)

**Do NOT enumerate `/{ad_account_id}/ads`.** Accounts hold thousands of ads (Washington Seattle
Mission 2,229; Michigan Detroit 6,975), pagination is sequential, and walking them all is exactly the
cost that times out the ad-health scan (see the `flag-ad-health-scan-scale` ticket).

Instead, build on **ad-level insights over the window**:
`reader.fetch_insights(ad_account_id, level="ad", date_from, date_to, fields=[...])`. Meta's insights
endpoint returns rows **only for ads that actually delivered** (had impressions/spend) in that
window — so it is naturally scoped to recently-active ads and never touches the dormant graveyard,
**and** it already carries the metrics we want (spend, impressions, clicks, results/actions). This
sidesteps the enumeration wall entirely. (`fetch_insights` already defaults to `level="ad"` and takes
a date range — verified.)

## What it delivers

A read tool (working name `cross_account_creative_triage`) over a scope + window returning per-ad
rows — `{account_id, account_name, ad_id, ad_name, spend, impressions, clicks, results,
cost_per_result, cpc, ctr, currency}` — ranked top/bottom by a chosen metric, i.e. **winners vs
losers**. Money normalized to one `reporting_currency` (static FX, per
[[currency-precision-low-priority]]). Reuse the concurrent fan-out engine, the FX normalization, and
the "recompute ratios from summed components" discipline already in `cross_account_performance` —
applied at ad level.

## Behavior / interface (proposal — plan stage finalizes)
- `cross_account_creative_triage(date_from, date_to, account_ids=None, metric="spend"|"cost_per_result"|..., order, limit=N, reporting_currency="USD")`.
- Per-account: one `fetch_insights(level="ad")` call (paginated), concurrent across accounts via the
  existing fan-out; then rank the pooled ad rows.

## Edge cases & interactions
- Big account with many *delivering* ads → still a large (paginated) insights pull; support `limit` /
  top-N and document read cost. It is bounded to delivering ads (usually far fewer than total), not
  the full ad set — that's the whole point.
- Ad with zero results → `cost_per_result` absent (not `inf`); ranked into an `unranked` bucket with
  a reason (mirror `rank_accounts`).
- Lead vs sales accounts → result metric vs purchase/value; resolve results against the lead/action
  family like `cross_account_performance` does.
- Cross-currency → normalize money metrics; ratio/count metrics currency-invariant.
- Missing `ad_name` → fall back to `ad_id`.
- Explicitly **out of scope**: ad *health* (disapproved / active-but-not-delivering) — those ads may
  have zero delivery so insights won't surface them; that is the separate `flag-ad-health-scan-scale`
  problem. This tool is about the performance of ads that DID run.
- Partial per-account failure isolates into `errors` (never fatal), like the other fan-outs.

## Use cases
- Specialist: "top and bottom 10 ads by cost-per-lead across my accounts this month."
- The ad-level drill-down beneath `grade_accounts_against_goals` (which is account-level). Not folded
  into the portfolio digest (that stays account-level).
