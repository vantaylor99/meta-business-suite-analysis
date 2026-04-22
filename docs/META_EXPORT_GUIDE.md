# Meta Export Guide

This guide shows exactly how to export the three CSV files this repo expects:

- `performance_daily.csv`
- `video_daily.csv`
- `creative_lookup.csv`

It also explains what date range to use, where to click in Meta, and why you should not use an "export every column" workflow as your default.

## Short Answer

Use three curated exports, not one giant "everything" export.

Why:

- giant exports are noisy and inconsistent across campaign objectives, attribution settings, and ad formats
- a curated export is easier to keep stable over time
- the code in this repo works best when the same fields appear every run

If you want extra insurance, do this:

- use the curated 3-file workflow every time
- optionally export one extra backup file like `archive_full_export.csv` once per month for debugging or future analysis work

## Where To Go In Meta

Use **Meta Ads Manager**, not generic account-history downloads.

Typical path:

1. Open **Meta Business Suite**
2. Go to **All tools**
3. Open **Ads Manager**
4. Select the correct ad account

If you already work directly in Ads Manager, that is fine too.

## Date Range Recommendation

### Recommended default for this account

- export the **last 30 days**

Why:

- your team is refreshing ads more frequently than a 90-day cycle
- 30 days is enough to catch recent fatigue, spend waste, and performance drift
- 30 days matches the workflow you were actually able to save in Ads Manager
- the report logic still works well with 30 daily rows

### For your ongoing workflow

- keep exporting the **rolling last 30 days**
- do the refresh weekly or whenever you want a fresh report

### If you later want longer-trend context

- optionally export 60 or 90 days as a separate comparison run
- keep the main recurring workflow at 30 days unless your operating cadence slows down

## Important Setup Before Every Export

Before you create any export:

1. Switch to **Ads** level, not Campaigns or Ad Sets
2. Make sure you are including all ads you want analyzed, not only active ads
3. Use the same account every time
4. Use the correct date range
5. Export as **CSV**

If you export only active ads, you will lose a lot of the fatigue and waste history.

## Best Weekly Workflow

Your best operating setup is:

1. save each report view in Ads Manager
2. reopen those saved views each week
3. export the rolling last 30 days
4. drop the files into a new dated folder
5. run the repo workflow

That is better than rebuilding the columns every time.

## Exact Header Sets

These are the exact headers that worked in your real account on `2026-04-21`. The docs should recommend these exact headers first, because they are the ones you were actually able to export.

If Meta later makes one unavailable, export the closest available equivalent and keep going.

## File 1: `performance_daily.csv`

This is the main commercial performance file.

### Purpose

This file drives:

- spend analysis
- ROAS analysis
- waste detection
- scaling candidate detection
- fatigue inputs like frequency and CTR drift

### Recommended settings

- Location: **Ads Manager**
- Level: **Ads**
- Date range: **Last 30 days**
- Breakdown: **Time > Day**
- Format: **CSV**

### Step-by-step

1. In **Ads Manager**, switch the table to **Ads**
2. Open your saved `performance_daily` report view if you created one
3. Set the date range to **Last 30 days**
4. Make sure the table includes the full ad population you want analyzed
5. Open **Breakdown**
6. Choose **Time**
7. Choose **Day**
8. Open **Columns**
9. Choose **Customize columns**
10. Add the exact columns below, or just use your saved view
11. Apply the customized column set
12. Export as **CSV**
13. Rename the file to `performance_daily.csv`
14. Put it in `data/raw/meta_ads/<run_date>/`

### Exact recommended headers

- `Campaign name`
- `Ad set name`
- `Campaign ID`
- `Ad name`
- `Ad set ID`
- `Ad ID`
- `Day`
- `Reach`
- `Impressions`
- `Frequency`
- `Result type`
- `Results`
- `Amount spent (USD)`
- `Cost per result`
- `Clicks (all)`
- `Outbound clicks`
- `Link clicks`
- `CTR (all)`
- `CPC (cost per link click)`
- `CPC (all)`
- `CPM (cost per 1,000 impressions)`
- `Average purchases conversion value`
- `Purchase ROAS (return on ad spend)`
- `Result value type`
- `Results ROAS`
- `Objective`
- `Reporting starts`
- `Reporting ends`

### Notes on this file

- `Amount spent (USD)` is the exact label your account exported, and the docs now recommend that exact label.
- `Clicks (all)` is useful and should stay in the recurring export.
- `Average purchases conversion value` is valuable even though the current report uses it less directly than spend and ROAS.
- `Results ROAS` is worth keeping as an extra ROAS-style signal when the standard purchase ROAS field is sparse.
- If `Purchase ROAS (return on ad spend)` is unavailable in some views, keep the closest ROAS field Meta allows.

## File 2: `video_daily.csv`

This is the creative attention file for hook-rate and hold-rate analysis.

### Purpose

This file drives:

- hook rate
- hold rate
- creative retention quality
- early creative fatigue signals for video ads

### Recommended settings

- Location: **Ads Manager**
- Level: **Ads**
- Date range: **same range as** `performance_daily.csv`
- Breakdown: **Time > Day**
- Format: **CSV**

### Step-by-step

1. Stay in **Ads Manager**
2. Stay at **Ads** level
3. Open your saved `video_daily` report view if you created one
4. Use the same **Last 30 days** range
5. Open **Breakdown**
6. Choose **Time > Day**
7. Open **Columns**
8. Choose **Customize columns**
9. Add the exact columns below, or just use your saved view
10. Export as **CSV**
11. Rename the file to `video_daily.csv`
12. Put it in the same `data/raw/meta_ads/<run_date>/` folder

### Exact recommended headers

- `Ad name`
- `Ad ID`
- `Day`
- `Attribution setting`
- `Starts`
- `Ends`
- `Impressions`
- `3-second video plays`
- `Cost per 3-second video play`
- `ThruPlays`
- `Cost per ThruPlay`
- `Video average play time`
- `Video plays at 25%`
- `Video plays at 50%`
- `Video plays at 75%`
- `Video plays at 95%`
- `Video plays at 100%`
- `Reporting starts`
- `Reporting ends`

### Notes on this file

- This is a stronger export than the original minimum spec because you included additional video-depth metrics that are genuinely useful.
- The current code mainly relies on `Impressions`, `3-second video plays`, and `ThruPlays`, but the extra fields are worth keeping every time.
- `Attribution setting`, `Starts`, and `Ends` are useful operational context even though they are not the main scoring inputs yet.

## File 3: `creative_lookup.csv`

This is the lightweight creative metadata file.

### Purpose

This file helps with:

- labeling creative type
- tying ad IDs to ad names cleanly
- giving the report more context when reviewing creative variation
- keeping a headline field attached to the ad for interpretation

### Recommended settings

- Location: **Ads Manager**
- Level: **Ads**
- Date range: use the same account and same general export window
- Breakdown: **none**
- Format: **CSV**

### Step-by-step

1. In **Ads Manager**, stay at **Ads** level
2. Use the same account as the other exports
3. Open your saved `creative_lookup` report view if you created one
4. Clear any day breakdown if one is active
5. Open **Columns**
6. Choose **Customize columns**
7. Add the exact columns below, or just use your saved view
8. Export as **CSV**
9. Rename the file to `creative_lookup.csv`
10. Put it in the same `data/raw/meta_ads/<run_date>/` folder

### Exact recommended headers

- `Ad ID`
- `Ad name`
- `Ad set name`
- `Campaign name`
- `Media type`
- `Headline (ad settings)`
- `Reporting starts`
- `Reporting ends`

### Notes on this file

- `Media type` is valuable and should stay in the export every time.
- `Headline (ad settings)` is useful context for the report, so keep it.
- `Reporting starts` and `Reporting ends` are fine to keep, but they are not the same thing as a true creative launch date.
- If Meta later allows a real launch-date or preview-link field in this export, that would be worth adding.

## Folder Workflow

For a run date like `2026-04-21`, put files here:

```text
data/raw/meta_ads/2026-04-21/
```

The folder should look like this:

```text
data/raw/meta_ads/2026-04-21/
  performance_daily.csv
  video_daily.csv
  creative_lookup.csv
```

Then run:

```powershell
python -m meta_ads_analysis ingest --run-date 2026-04-21
python -m meta_ads_analysis report --run-date 2026-04-21
```

Or, if the installed scripts are on your `PATH`:

```powershell
ingest_meta_exports --run-date 2026-04-21
build_meta_report --run-date 2026-04-21
```

## Recommended Routine

### Weekly operating rhythm

1. Export `performance_daily.csv` for the rolling last 30 days
2. Export `video_daily.csv` for the same 30 days
3. Export `creative_lookup.csv`
4. Put all 3 files into a new dated folder
5. Run the repo workflow
6. Review the generated report

In practice, because you already saved the report views, this becomes:

1. open saved view 1 and export
2. open saved view 2 and export
3. open saved view 3 and export
4. drop the files into the dated folder
5. run the commands

### Monthly backup

Optionally once per month:

1. Export a much wider raw report
2. Save it outside the main workflow as something like `archive_full_export.csv`
3. Keep using the 3 curated exports as the actual input to the code

## What Not To Do

- do not use generic Facebook data downloads instead of Ads Manager exports
- do not export only active ads unless that is truly all you want analyzed
- do not mix different date ranges across `performance_daily.csv` and `video_daily.csv`
- do not use Campaign or Ad Set level exports for the main files
- do not rely on one giant "everything" export as your only workflow

## Safest Default

If you want the shortest practical version:

1. Go to **Meta Business Suite > All tools > Ads Manager**
2. Export from **Ads** level
3. Use **Last 30 days**
4. Use **Breakdown > Time > Day** for `performance_daily.csv` and `video_daily.csv`
5. Use **no day breakdown** for `creative_lookup.csv`
6. Prefer your saved report views so the columns stay consistent
7. Export the exact headers listed in this guide
8. Drop the files into the dated folder
9. Run the repo commands
