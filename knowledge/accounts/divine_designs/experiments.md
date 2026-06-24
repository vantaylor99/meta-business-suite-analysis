# Divine Designs — experiments

What we're testing, what we're waiting on, and what past tests taught us. When an experiment
concludes, copy the durable lesson into `knowledge/learnings.md`.

> Hypotheses carry a confidence level too (see README rubric). When an experiment concludes,
> fold its result into `learnings.md` as a dated ➕/➖ evidence line and adjust that learning's level.

## ACTIVE — "Does turning Advantage+ off restore the proven creatives?"

- **Hypothesis:** Several creatives that performed well in other tests underperformed here
  because Advantage+ Audience was expanding delivery past the intended audience. With AA off
  (done 2026-06-22), the defined audience is respected, so those creatives should perform
  closer to their prior ROAS.
- **Confidence in the hypothesis:** 🔴 Low → — plausible and operator-reported ("did well in
  other tests"), but untested here and currently un-testable (creatives blocked). Will move
  once the test below runs.
- **Change made:** Advantage Audience disabled on all 3 active ad sets (2026-06-22).
  Audiences and exclusions left unchanged (so this is a clean one-variable test of AA).
- **BLOCKED ON:** the **dev-mode app** issue. Most of the proven creatives (New Pink, White
  BG V2 - Copy, New Black, etc.) are `WITH_ISSUES` and cannot deliver, so we cannot yet
  evaluate AA's effect on them. The account is running on only a few unaffected ads.
- **Sequence to run the test properly:**
  1. Human: set the Facebook App to **Live** in the Meta App Dashboard (and add the pixel for
     `Selfie Mom - Copy`). Re-check `issues_info` clears.
  2. Re-enable the proven winners inside the now-AA-off ad sets (not just "test" ads — the
     3–5 ROAS performers).
  3. Hold **~5–7 days** (longer than 3–5: AA change + re-enable both reset learning, and daily
     purchase volume is low, so 3 days would be noisy).
  4. Re-pull (`sync-api` + `report`) and compare ROAS **at the ad-set level** against the
     2026-06-22 baseline (2.43 blended; per-ad-set in profile.md). Log the result here.
- **Success signal:** blended ROAS trends toward 3.0; the unblocked winners deliver at/near
  their prior 3–5 ROAS now that the audience is respected.
- **Caveat / interpretation:** re-enabling ads + AA-off happen together, so a positive result
  shows the *combination* works, not AA alone. Good enough for optimization; note it when reading.

## PLANNED — creative-enhancement A/B (the 2026-07-07 follow-up)

> We believe enhancements (the additive set) helped our winners, but that's **correlational** —
> the winners happened to run with them on. This is the test that makes it causal. Tooling is now
> built: the `experiment` harness (`define` / `readout`). Discuss scope with the operator on 07-07.
>
> **Setup (one variable = enhancements on vs off):**
> 1. Pick one stable, delivering ad as the control (enhancements ON via our default OPT_IN set).
> 2. `propose-duplicate-ad` to clone it into a **matched separate ad set** (same audience/budget;
>    separate ad set avoids the two arms competing for the same auction → cleaner read).
> 3. `apply-ops set_creative_features` on the clone to flip the enhancements **OFF** — nothing else.
> 4. `experiment define --account divine_designs --id enh-on-vs-off --variable "enhancements on vs off"
>    --level ad --control <orig ad id> --variant <clone ad id> --days 14`.
> 5. Hold ~14d (low daily purchase volume → need the window to clear the 25-purchases/arm gate).
> 6. `experiment readout --account divine_designs --id enh-on-vs-off` → fold the verdict into
>    `learnings.md` as a dated ➕/➖ line and adjust the creative-enhancements learning's confidence.
> - **Caveat the readout prints:** significance is on conversion-rate; if both arms ever share an
>   ad set they compete. Matched separate ad sets (step 2) is what keeps this honest.

## PLANNED / DEFERRED — audience rotation (isolate audience vs creative)

- **Question:** Is "High Value Customers" weak (1.78) because of the high-value audience or
  because of the weak "Test - Selfie OG" creatives it ran? Right now they're confounded.
- **Test:** rotate audiences across ad sets while holding creatives constant (`propose-rotation`).
- **Deferred until:** creatives are unblocked and the AA-off effect above has been measured —
  otherwise we'd stack variables. Tooling is built and validated (validate-only passed).

## Open questions to revisit

- Does the engaged audience keep its higher AOV (~$67) and 3.7 ROAS once AA is off and the
  full creative set is running?
- Is the Low Value Customers ad set (45% of spend, 2.04) over-funded? Reallocation candidate.
- `White BG V2` (one ad, ~21% of spend, 1.85) vs its sibling `White BG V2 - Copy` (5.37) —
  why the gap? Same concept, very different result.
