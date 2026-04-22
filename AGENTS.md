# Meta Ads Analysis Agent Guide

This file defines how an agent should analyze the normalized Meta ads data and how the final report should be written.

## Purpose

The goal is to help a human operator quickly understand:

- what is wasting budget,
- what is fatiguing or going stale,
- which ads hook well,
- which ads are strong candidates to scale,
- and whether the account's measurement quality makes ROAS trustworthy.

The agent should prefer clarity and actionability over sounding sophisticated.

## Source Hierarchy

Use these sources in this order:

1. `reports/<run_date>/meta_ads_report.json`
2. `reports/<run_date>/meta_ads_report.md`
3. `data/normalized/meta_ads/<run_date>/ad_daily_metrics.csv`
4. `data/normalized/meta_ads/<run_date>/creative_lookup.csv`
5. raw files in `data/raw/meta_ads/<run_date>/` only when a detail needs to be verified

Do not infer confidence that is not supported by the exported data.

## Output Structure

Every written analysis should use this structure:

1. Executive summary
2. Budget waste findings
3. Fatigue and staleness findings
4. Hook-rate and creative-performance findings
5. Scaling candidates
6. Tracking and measurement concerns
7. Recommended actions for the next 7 days

## Metric Definitions

- `hook_rate = video_3s_plays / impressions`
- `hold_rate = thruplays / video_3s_plays`
- `blended_roas = purchase_value / spend`
- `average_order_value = purchase_value / purchase_count`
- `fatigue_score` is a composite score based on rising frequency and degrading CTR, CPA, and ROAS between prior and recent windows
- `waste_score` is a composite score based on meaningful spend with weak or missing commercial output

## Interpretation Rules

- If an ad has no video metrics, do not pretend it has a hook rate. Treat hook analysis as not applicable.
- If purchase counts exist but purchase value is missing, explicitly say ROAS confidence is low.
- If an ad has low spend and weak results, call it `insufficient data` before calling it wasted budget.
- If an ad has strong ROAS but very small spend, call it a `promising test` rather than a clear scale winner.
- If an ad has rising frequency and falling efficiency over enough history, call out fatigue even if it still produces results.
- If export coverage is incomplete, say so plainly.

## Severity Heuristics

- High waste risk:
  - high spend,
  - zero or near-zero purchases,
  - or clearly poor ROAS versus the rest of the account
- High fatigue risk:
  - enough history,
  - recent frequency up,
  - recent CTR down,
  - and either CPA worse or ROAS weaker
- Strong scaling candidate:
  - enough spend to matter,
  - strong ROAS,
  - low fatigue,
  - and no obvious tracking issue

## Tone

- Be direct and business useful.
- Avoid jargon when a plain statement is clearer.
- Prefer statements like `This ad is absorbing spend without producing proportional value` over vague commentary.
- Separate findings from uncertainty.

## Guardrails

- Do not claim causal certainty from export data alone.
- Do not assume the pixel or Conversions API is healthy just because Meta reports purchases.
- Do not recommend scaling solely off hook rate without downstream conversion evidence.
- Do not collapse missing data into zeros unless the normalized output already did so intentionally.
