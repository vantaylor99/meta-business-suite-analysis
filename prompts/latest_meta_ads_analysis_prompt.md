Use this repository's `AGENTS.md` as the analysis contract.

Your task is to analyze the most recent Meta ads run for a specified `account_slug` and produce a decision-ready account review.

The caller will provide the `account_slug` to use, for example `pollen_sense` or `divine_designs`.

This prompt **extends** the base sections in `AGENTS.md` with **two window snapshots** (last 7 days and last 3 days) so the operator gets both the **full-period story** and **short-window reads** in one analysis. Apply `AGENTS.md` interpretation rules, severity heuristics, and guardrails throughout.

## Instructions

1. Use the provided `account_slug`.
2. Find the latest available `run_date` by checking the date-named folders under `reports/<account_slug>/` and choosing the most recent folder in `YYYY-MM-DD` format.
3. Use sources in this exact order:
   - `reports/<account_slug>/<run_date>/meta_ads_report.json`
   - `reports/<account_slug>/<run_date>/meta_ads_report.md`
   - `data/normalized/meta_ads/<account_slug>/<run_date>/ad_daily_metrics.csv`
   - `data/normalized/meta_ads/<account_slug>/<run_date>/creative_lookup.csv`
   - `data/raw/meta_ads/<account_slug>/<run_date>/` only if a detail must be verified
4. Follow all interpretation rules, severity heuristics, and guardrails from `AGENTS.md`.
5. Do not overstate certainty. If the export coverage is incomplete or tracking looks weak, say so plainly.
6. Prefer direct, operator-useful language over marketing language or generic analysis.

## Report JSON: multi-window fields (when present)

The generated report includes **trailing calendar windows** ending on the latest `report_date` in the ingest (typically **30d**, **7d**, **3d**). Prefer these keys from `meta_ads_report.json` for snapshots:

- **`window_comparison_meta`** — `window_end`, requested windows, and **`coverage`** per window (`days_with_data`, `coverage_note` if the export has fewer days than requested).
- **`account_window_summary`** — rolled-up account metrics for **`30d`**, **`7d`**, **`3d`** (spend, results, installs, cost per result, cost per install, hook rate when applicable, etc.).
- **`ad_window_summaries`** — per-ad metrics for each window plus **`trajectory`** (e.g. 7d vs 30d efficiency, 3d vs 7d where data passes review floors).
- **`trajectory_highlights`** — ads flagged as **improving** or **degrading** between windows (use as a shortcut; still spot-check `ad_window_summaries` for context).

**Short-window discipline:** Treat **3-day** figures as **directional** (noisy: integer events, weekday effects, thin spend). Do not treat 3d hook or CPI alone as proof to scale or kill an ad. Compare **7d vs 30d** for a steadier “recent vs baseline” read when coverage is full.

## Deliver the analysis using exactly this structure

1. **Executive summary** — Full ingest window (the report’s overall coverage): spend, primary results, secondary signals, and blended ROAS context per `measurement_focus`. In **two or three sentences**, state whether **recent** behavior (**7d**, and briefly **3d** if useful) **matches, improves on, or diverges from** the full-period story using `account_window_summary` (do not duplicate the entire snapshot here; point forward to sections 8–9).
2. **Budget waste findings**
3. **Fatigue and staleness findings**
4. **Hook-rate and creative-performance findings**
5. **Scaling candidates**
6. **Tracking and measurement concerns**
7. **Recommended actions for the next 7 days**
8. **Last 7 days snapshot** — Operator-facing summary of **only the trailing 7 days** (`account_window_summary["7d"]`): spend, results, installs, cost per result / cost per install (or whichever metrics the account prioritizes per `measurement_focus`). Call out **2–4 ads** worth attention using `ad_window_summaries` (meaningful spend or strong trajectory in this window). Compare **7d vs 30d** at account level where it clarifies trend (**improving**, **softening**, or **flat**). If `coverage["7d"]` shows incomplete days, say so.
9. **Last 3 days snapshot** — Same idea for **`account_window_summary["3d"]`**, with **explicit** labeling that this is a **directional / early-signal** window only. Highlight only ads with enough spend or volume in 3d to mention; otherwise say **insufficient data** for granular ad calls. If `coverage["3d"]` is incomplete, say so.

## Additional output requirements

- Name the selected `run_date` at the top.
- Name the selected `account_slug` at the top.
- Separate **strong findings** from **uncertainty** (and separate **7d** reads from **noisy 3d** reads where relevant).
- Call out specific ads, campaigns, or ad sets when the data supports it.
- If an ad has low spend and weak performance, label it `insufficient data` before calling it wasted budget.
- If an ad has strong ROAS but very small spend, label it a `promising test` rather than a clear scale winner.
- Do not recommend scaling from hook rate alone without downstream conversion evidence.
- If purchase counts exist but purchase value is missing, explicitly say ROAS confidence is low.
- If an ad has **no video metrics** in the export, do not invent hook rate; say hook analysis is **not applicable** for that ad.
- Use **`measurement_focus`** from the JSON (`primary_metric`, `secondary_metric`, `roas_role`) so primary vs fallback signals match the account.

End with a short **prioritized action list** for the operator to take this week (bullet list after section 9).
