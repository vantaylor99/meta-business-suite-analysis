# Meta API Setup

This repo can fetch Meta ads data directly from the Marketing API with:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22
```

## What You Need

- A Meta app with read access to the ad accounts you manage
- A working access token with `ads_read`
- Real ad account IDs in `config/meta_ads_accounts.json`

## Configuration

Update:

```text
config/meta_ads_accounts.json
```

Each account entry should include:

- `account_slug`
- `account_name`
- `ad_account_id`
- optional `timezone`
- optional `notes`
- optional `primary_result_action_type`
- optional `primary_result_label`
- optional `measurement_focus`

If `primary_result_action_type` is omitted, the sync will try to infer a primary result from Meta action data. If it cannot, the `Results` column may be blank and the sync summary will warn you.

Recommended `measurement_focus` shape:

```json
{
  "primary_metric": "results",
  "primary_result_action_type": "app_custom_event.fb_mobile_subscribe",
  "primary_result_label": "In-app subscriptions",
  "secondary_metric": "app_installs",
  "secondary_metric_label": "App installs",
  "roas_role": "supporting_only_until_subscription_value_is_stable",
  "analysis_notes": "Optimize for subscriptions first. Use app installs as a fallback when revenue reporting is still stabilizing."
}
```

Use `secondary_metric` for the best fallback signal when primary results are sparse. Use `roas_role` to describe whether ROAS should be treated as primary, supporting, or low-confidence for that account.

## Environment Variables

Required:

```powershell
$env:META_ACCESS_TOKEN="your-token-here"
```

Optional:

```powershell
$env:META_API_VERSION="v22.0"
```

## Default Date Window

If you only pass `--run-date`, the sync will fetch the trailing 30-day daily window ending on that date.

Example:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22
```

This will fetch:

- `2026-03-24` through `2026-04-22`

Reports also derive 30-day, 7-day, and 3-day performance windows from this same daily pull. You do not need separate API syncs for each window; the report slices the exported daily rows ending on the latest exported day and labels short-window reads as directional when data is thin.

You can override the window:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22 --date-from 2026-04-01 --date-to 2026-04-22
```

## What Gets Written

Raw API exports:

```text
data/raw/meta_ads/<account_slug>/<run_date>/
  performance_daily.csv
  video_daily.csv
  creative_lookup.csv
  api_sync_summary.json
```

If you do not pass `--raw-only`, the command also writes:

```text
data/normalized/meta_ads/<account_slug>/<run_date>/
reports/<account_slug>/<run_date>/
```

## Common Failure Modes

- Missing `META_ACCESS_TOKEN`
- Placeholder or incorrect `ad_account_id` in the account registry
- Token does not have `ads_read`
- The ad account is not accessible by the token
- The account’s primary result action cannot be inferred cleanly from the returned `actions`
- Some creative preview or post links may be blank if Meta does not return story identifiers

## Notes

- The sync is read-only. It does not modify account settings or ads.
- V1 runs one account at a time.
- V1 preserves the same raw CSV contract the manual export workflow uses today.
