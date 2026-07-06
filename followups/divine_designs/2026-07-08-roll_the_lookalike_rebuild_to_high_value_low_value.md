---
title: Roll the lookalike rebuild to High Value + Low Value (if Engaged's fix worked)
account: divine_designs
due: 2026-07-08
status: open
created: 2026-07-02
---

2026-07-02 we found all 3 active ad sets' lookalikes were broken (size 1,000, lookalike_spec.country=None, never built). Rebuilt Engaged's as a real 5% US LAL (120247434998550733, ~12M) and swapped it in via the new set_custom_audiences guarded op; Advantage+ kept OFF. High Value (LAL 120245033400010733) and Low Value (LAL 120245880988810733) are STILL on broken 1% lookalikes - deliberately left as-is so High Value is the clean control for reading whether the audience fix helped Engaged. AFTER the Jul 7 settle read: if Engaged improved, rebuild HV + LV as proper 1% US lookalikes (propose-lookalike --country US --ratio 0.01 from seeds 120242999579470733 and 120242999644960733) and swap them in (set_custom_audiences, keep AA off). Low Value's campaign (May Lower Spend) is paused, so fix it whenever it relaunches. Also: HV/LV exclusions still reference Engaged's OLD lookalike id 120245881024420733 - update exclusions when reworking them.
