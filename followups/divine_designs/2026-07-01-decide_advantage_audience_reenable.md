---
title: Decide — re-enable Advantage+ Audience if Engaged/Low Value haven't recovered
account: divine_designs
due: 2026-07-01
status: open
created: 2026-06-29
---

Recovery checkpoint for the post-2026-06-22 account tank. Context: blended ROAS held ~2.2x through
Jun 22, then cliffed to sub-1.0 on Jun 23-24 — coincident with our stacked changes (Advantage+ Audience
disabled Jun 22 + ~18-ad mass re-enable after the dev-mode fix + placement policy Jun 24). It is NOT a
tracking break and NOT a turned-off campaign: High Value Customers got the SAME Advantage+-off treatment
and recovered (30d 1.72x -> 7d 2.10x), which proves the config can convert — so this reads as a severe,
self-inflicted RE-LEARNING episode, concentrated in the most-churned ad sets (Engaged 2.89x -> 0.64x,
Low Value 1.99x -> 0.91x). See the 2026-06-29 (diagnosis) decision-log entry for the full read.

Decision owed:
1. Read the daily trend for Engaged + Low Value (account-level + per-adset). Are they climbing toward
   ~1.5x+ (allowing for attribution lag understating the last 1-2 days)?
2. If YES / climbing: leave it alone — re-learning is resolving. Do NOT add changes.
3. If still flat/down (sub-1.0): test re-enabling Advantage+ Audience on Engaged Audience via the CLI
   guarded flow (propose -> validate -> execute). Reversible. Caveat: AA was disabled on purpose (it was
   overriding the custom audiences and blocking clean audience reads), so this is a deliberate tradeoff —
   test on Engaged first, not all ad sets at once.

Guardrails: one change at a time (the root cause was too many simultaneous edits); do NOT mass-pause the
sub-1.0 ads mid-relearn (pausing resets the learning clock). All writes via the CLI, never the MCP.
