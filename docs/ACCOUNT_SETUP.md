# Setting Up a New Ad Account

When you add an account that's never been used in this repo, the goal is to capture **what
"good" and "bad" mean for this specific account** so the analysis, the operator brief, and the
guarded write tools all judge it correctly. Every account is one entry in
[`config/meta_ads_accounts.json`](../config/meta_ads_accounts.json).

Different accounts have different goals — a store optimizes for **ROAS**, a lead-gen account for
**cost per lead (CPR)**, an app for **cost per install/subscription**. The fields below let one
pipeline serve all of them.

## The checklist

### 1. Identity (required)
| Field | What it is |
|---|---|
| `account_slug` | Short nickname used everywhere (`divine_designs`, `seattle_mission`). |
| `account_name` | Human-readable name. |
| `ad_account_id` | The numeric Meta ad-account id (no `act_` prefix). |
| `default_destination_url` | (e-commerce only) the landing site. Omit for lead/instant-form accounts. |

### 2. `measurement_focus` — what counts as a result
Tells the reports which number is "success" for this account.
- `primary_metric` — `results` for goal-driven accounts.
- `primary_result_action_type` — the Meta action behind a result (`purchase`, `leadgen_grouped`,
  `app_custom_event.*`, …). **Verify on first sync** — if Results come back blank, adjust it.
- `primary_result_label` — human label (`Website purchases`, `Leads (form)`).
- `secondary_metric` / `_label` — a fallback signal (e.g. outbound clicks, app installs). `null` if none.
- `roas_role` — how much to trust ROAS: a real KPI, a supporting metric, or `not_applicable`
  (lead/install accounts have **no** ROAS — say so here so nothing applies revenue logic to them).
- `analysis_notes` — free-text context for the analyst/agent.

### 3. `action_policy` — the guardrails (the important part)
This is where you set the numbers that drive pause/scale decisions.

**The goal (what you're aiming for):**
- `primary_goal` — `roas` | `minimize_cost_per_lead` | `maximize_in_app_subscriptions` | …
- `primary_metric` — `blended_roas` | `cost_per_result` | `results`.
- **`target_roas`** *(ROAS accounts)* or **`target_cost_per_result`** *(CPR accounts)* — the goal
  line. e.g. Divine Designs targets 3.0 ROAS; Seattle Mission targets $10/lead.

**The cutoff (when to kill a loser):**
- **`pause_roas_floor`** *(ROAS accounts)* — pause when ROAS is **at or below** this.
- **`pause_cost_per_result_above`** *(CPR accounts)* — pause when cost-per-result is **at or above** this.
- e.g. Divine Designs cutoff 1.8 ROAS; Seattle Mission cutoff $40/lead.

**The two rules that keep the cutoff honest (apply to every account):**
- **`evaluation_grace_days`** (suggest **3**) — the cutoff does **not** apply during a new ad's
  first 1-2 days, or to any ad right after a **big change** (creative / audience / budget reset).
  Give it up to this many days to stabilize, then judge. By day 3 you should make a clear keep/kill
  call rather than waiting forever. *(Enforced by the early-life triage engine — see
  `EARLY_LIFE_*` in `config.py` / `early_triage.py`.)*
- **`min_spend_before_pause`** (suggest **100.0**, mirrors `MIN_WASTE_SPEND`) — never pause a thin
  ad on the cutoff. If an ad is at/under the cutoff **but has barely spent**, that's *insufficient
  data*, not a loser — keep it running. *(Enforced by the confidence engine's abstain-below-floor
  rule.)*

**Scaling + safety:**
- `scale_roas_floor` / `scale_if_primary_results_at_least` — when a winner earns more budget.
- `max_budget_increase_percent` (suggest **20**) — cap on a single budget bump (avoids learning resets).
- `disable_meta_ai_features` — keep Advantage+/Meta-AI overrides off (`true` for these accounts).
- `operator_brief_todo` — include this account in the operator brief.
- `guardrail_notes` — plain-English statement of the goal, cutoff, grace, and low-spend rules so a
  human reading the config understands the policy without decoding the numbers.

### 4. `notes`
Account context: current state (active/paused), historical performance, quirks (e.g. "Instant Forms
capture leads on-platform — no website/offsite-checkout needed").

## Worked examples (live in the config)
- **`divine_designs`** — ROAS account: target 3.0, cutoff **1.8**, grace 3 days, min-spend $100.
- **`seattle_mission`** — lead-gen account: target **$10/lead**, cutoff **$40/lead**, grace 3 days,
  ROAS `not_applicable`.
- **`pollen_sense`** — app/subscription account: primary = in-app subscriptions, install cost as the
  secondary fallback.

## Enforcement status (honest)
- **Enforced today:** ROAS target/floor, `min_spend_before_pause` (via `MIN_WASTE_SPEND` + confidence
  abstain), the early-life grace window, `max_budget_increase_percent`.
- **In flight:** full goal-awareness for **non-ROAS** accounts (lead/install CPR targets and
  direction checks) is being wired by the `confidence-install-goal-*` / `review-gate-install-goal-*`
  tickets. Until those land, a CPR account's `target_cost_per_result` / `pause_cost_per_result_above`
  are authoritative as **documented policy** (and honored by the agent), with code enforcement
  catching up. `evaluation_grace_days` / `min_spend_before_pause` are currently read from the global
  constants; per-account overrides are captured here as intent.
