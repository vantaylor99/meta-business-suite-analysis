Use this repository's `AGENTS.md` as the analysis contract.

Your task is to analyze the most recent Meta ads run for a specified `account_slug` and produce a decision-ready account review.

The caller will provide the `account_slug` to use, for example `pollen_sense` or `divine_designs`.

Instructions:

1. Use the provided `account_slug`.
2. Find the latest available `run_date` by checking the date-named folders under `reports/<account_slug>/` and choosing the most recent folder in `YYYY-MM-DD` format.
2. Use sources in this exact order:
   - `reports/<account_slug>/<run_date>/meta_ads_report.json`
   - `reports/<account_slug>/<run_date>/meta_ads_report.md`
   - `data/normalized/meta_ads/<account_slug>/<run_date>/ad_daily_metrics.csv`
   - `data/normalized/meta_ads/<account_slug>/<run_date>/creative_lookup.csv`
   - `data/raw/meta_ads/<account_slug>/<run_date>/` only if a detail must be verified
3. Follow all interpretation rules, severity heuristics, and guardrails from `AGENTS.md`.
4. Do not overstate certainty. If the export coverage is incomplete or tracking looks weak, say so plainly.
5. Prefer direct, operator-useful language over marketing language or generic analysis.

Deliver the analysis using exactly this structure:

1. Executive summary
2. Budget waste findings
3. Fatigue and staleness findings
4. Hook-rate and creative-performance findings
5. Scaling candidates
6. Tracking and measurement concerns
7. Recommended actions for the next 7 days

Additional output requirements:

- Name the selected `run_date` at the top.
- Name the selected `account_slug` at the top.
- Separate strong findings from uncertainty.
- Call out specific ads, campaigns, or ad sets when the data supports it.
- If an ad has low spend and weak performance, label it `insufficient data` before calling it wasted budget.
- If an ad has strong ROAS but very small spend, label it a `promising test` rather than a clear scale winner.
- Do not recommend scaling from hook rate alone without downstream conversion evidence.
- If purchase counts exist but purchase value is missing, explicitly say ROAS confidence is low.

End with a short prioritized action list for the operator to take this week.
