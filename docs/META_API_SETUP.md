# Meta API Setup

This repo can fetch Meta ads data directly from the Marketing API with:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22
```

## What You Need

- A Meta app with access to the ad accounts you manage
- A working access token:
  - `ads_read` is enough for reporting sync, dry runs, and live-state reads
  - `ads_management` is required to *execute* any write with `--execute` — across all pipelines:
    `apply-actions`, `apply-ops` (status / budget / targeting / creative), `apply-authoring`
    (`create_*`), `apply-rotation`, `apply-disable-advantage`, and `apply-renames`. The full
    guarded-write catalog is in [`../AGENTS.md`](../AGENTS.md) under **Hybrid Meta integration**.
- Real ad account IDs in `config/meta_ads_accounts.json`

## Getting an access token

There are two ways to get a token. For anyone other than yourself running this long-term (a
specialist, a second machine), use the **System User** path — it doesn't expire on a schedule and
isn't tied to a personal Facebook login that can log out or lose access.

### Recommended: a Business Manager System User token (for a specialist / long-term use)

1. Go to [Meta Business Settings](https://business.facebook.com/settings) for the Business Manager
   that owns the ad account(s) in question.
2. **Users → System Users → Add.** Name it for what it's for (e.g. `seattle-mission-mcp-server`),
   role can be **Employee** (not Admin — don't over-grant).
3. On that System User, click **Add Assets** and assign **only the specific ad account(s)** it
   needs (e.g. just Seattle Mission), with **Manage campaigns** permission (this is what maps to
   the `ads_management` scope). This is how you scope a token to exactly one account — the token
   itself has no account restriction, the System User's asset assignment does.
4. Click **Generate New Token** on that System User. Select the app (or create a minimal one under
   **Accounts → Apps** first if none exists yet — it just needs to exist, no App Review required
   for this token to work for assets the System User already has explicit access to). Check
   **`ads_management`** (covers `ads_read` too) in the permission list.
5. Copy the token immediately — Meta shows it once. System User tokens don't expire on the usual
   60-day user-token schedule (they're revoked by deleting the System User, deleting the token, or
   removing its asset assignment).

### Quicker but shorter-lived: Graph API Explorer (fine for your own ad-hoc testing)

1. [Graph API Explorer](https://developers.facebook.com/tools/explorer/) → select your app → select
   your user → check `ads_management`/`ads_read` → **Generate Access Token**.
2. This is a **short-lived** (~1 hour) user token by default. Exchange it for a 60-day long-lived
   token via the `/oauth/access_token?grant_type=fb_exchange_token...` endpoint (see
   [Meta's long-lived token docs](https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived)) if you need it to last.
3. This token rides on *your* personal Facebook login and ad-account permissions — don't hand this
   kind of token to someone else; generate them a System User token instead.

### Handing a token to someone else securely

Whatever the token, send it through something that doesn't leave it sitting in plaintext (chat,
email, a ticket) — a password manager's one-time share link (e.g. 1Password's or Bitwarden's
"share via link" — these produce a URL the recipient opens in a browser and do **not** require
them to have an account with that service) works well and matches what
[SPECIALIST_ONBOARDING.md](SPECIALIST_ONBOARDING.md) assumes.

### Sanity-check a token before handing it off

```bash
curl -s "https://graph.facebook.com/v21.0/me/adaccounts?access_token=<token>"
```
Should list the ad account(s) it has access to. An empty list or an error means the asset
assignment (System User path) or the permission grant (Explorer path) didn't take.

The MCP server exposes this same check as the **`list_ad_accounts`** tool (it takes no `account`
argument), so once the server is running you can discover every reachable account from inside
Cowork — with a human-readable status for each — instead of hand-running this curl.

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

## Read backend: direct (default) vs MCP

Reads flow through a swappable provider seam — `MetaReaderProvider` in
`src/meta_ads_analysis/reader_provider.py`. The backend is chosen by one env var:

```powershell
$env:META_READER_BACKEND="direct"   # default — the live Graph API client (today's behavior)
$env:META_READER_BACKEND="mcp"      # route reads through a Meta MCP server (opt-in)
```

**Default is `direct`.** Unset or `direct` is byte-for-byte today's behavior; nothing changes unless
you explicitly opt in. **Writes never go through the read backend** — they always use the direct
Graph API client, so the MCP read path is *reads-only* and the existing `ads_read` token is enough
for it (writes still need `ads_management` and the `--execute` flag).

### Community token-based MCP read server (candidate, UNVETTED)

A second server is recorded in `.mcp.json` under `_candidateMcpServers` so it is **present but not
launched** — only servers under `mcpServers` are started, and the only active one is `code-search`.
This is deliberate: nothing in the build runs the community server.

- **Candidate package:** `meta-ads-mcp-server@1.5.1` (npm) — **candidate, unvetted; the operator
  must review the package and pin a known-good version before enabling.** Chosen because it
  authenticates with a long-lived user/system-user token (no OAuth) and registers a **read-only**
  tool set by default.
- **Auth:** it reads the token from `META_ADS_ACCESS_TOKEN`; the candidate entry maps that from the
  existing `${META_ACCESS_TOKEN}` so the secret stays in the environment (the committed `.mcp.json`
  never embeds a literal token). Confirm the env-var name when you vet the package.
- **To enable (operator, after vetting):** move the `meta-ads-read` object from
  `_candidateMcpServers` into `mcpServers`, then set `META_READER_BACKEND=mcp`. No code change is
  required — both backends satisfy the same `MetaReaderProvider` seam.
- **Covered reads (mapped to MCP tools):** `fetch_insights`, `fetch_ads`, `list_campaigns`,
  `get_campaign`, `list_adsets`, `get_adset`, `get_ad`, `get_account`.
- **NOT covered (fall back to `direct` for these):** `list_custom_audiences`,
  `get_delivery_estimate`, `search_targeting`, `list_pixels`, `list_custom_conversions`,
  `get_activity_log`, and the raw `iter_paginated` escape hatch. Each raises a clear
  `NotImplementedError` naming the read.
- **Pagination:** the candidate does not auto-paginate; `MCPMetaReader` follows `paging.next` via the
  server's `meta_ads_fetch_pagination_url` tool and **refuses to silently truncate** (it raises if a
  page is dropped and no pagination tool is configured).

The MCP backend is consumed by the **agent runtime**, which injects the MCP tool-call surface into
`MCPMetaReader(tool_executor=...)`. The pure-Python CLI cannot synthesize that surface, so running a
CLI command with `META_READER_BACKEND=mcp` raises a clear error rather than silently degrading — keep
CLI/sync runs on `direct`.

### Our custom Meta MCP server (local)

Separate from the community `meta-ads-read` **read** candidate above, this repo also ships **our own**
custom Meta MCP server — the long-term home for reads *and* guarded writes behind one connector. It now
exposes the full live Meta **read** surface: the `server_info` health tool plus one tool per read (14
tools — `fetch_insights`, `fetch_ads`, `list_campaigns`, `get_account`, `search_targeting`,
`list_pixels`, `get_activity_log`, … — a superset of what the parked community candidate could serve).
Each read tool is a
1:1 wrapper over the direct reader; a bad token or insufficient scope comes back as a clean tool error,
not a crash. It **also now exposes the guarded write surface**: `propose_*` (grounded, reviewed,
persisted as a proposal returning only a `plan_id`), `preview_plan` (write-free dry run), and
`execute_plan` (the only writer — validate-then-execute, refuses a plan with zero approved ops).
Every write routes through the same propose → human-approve → validate → execute → verify gate as the
CLI; the guardrail is enforced *in the server*, not by prompt. It runs as its own HTTP process, distinct
from the parked community candidate. (Writes still need an `ads_management`-scoped token; the read-only
`ads_read` token fails the mandatory `validate_only` pass with a clear scope error.)

Install the server extra (kept optional so the CSV/analysis install stays lean) and launch it. A valid
`META_ACCESS_TOKEN` (with the `ads_read` scope) must be set — the server builds its reader at startup
and exits with an actionable message if the token is missing:

```powershell
pip install -e .[server]
$env:META_ACCESS_TOKEN="<your token>"
meta_mcp_server --host 127.0.0.1 --port 8765
```

Host/port precedence is **explicit flag > env var > local default**: `--host` / `--port` win, else
`MCP_SERVER_HOST` / `MCP_SERVER_PORT`, else `127.0.0.1` / `8765`.

```powershell
$env:MCP_SERVER_HOST="127.0.0.1"
$env:MCP_SERVER_PORT="8765"
meta_mcp_server
```

An MCP client then connects at the streamable-http URL **`http://127.0.0.1:8765/mcp`** and can call
`server_info` (server name/version, configured Meta API version, selected read backend,
`live_calls_enabled: true`, and `write_tools_enabled: true` now that reads and gated writes are live)
plus any of the 14 read tools, the seven discovery tools (`list_ad_accounts`,
`cross_account_spend_summary`, `cross_account_performance`, `account_benchmark`,
`flag_accounts_needing_attention`, `pacing_report`, and `rank_accounts` — none takes an
`account` argument), and the
guarded write tools (`propose_*` / `preview_plan` / `execute_plan`). If the `server` extra is not installed, launching prints an actionable error
(`pip install -e .[server]`) rather than a traceback.

`cross_account_spend_summary` fans out over the target accounts **concurrently** (a bounded thread
pool over the synchronous reader), so an all-accounts call over a large fleet (hundreds of accounts)
finishes in tens of seconds instead of timing out on a serial walk. The pool size is
`META_FANOUT_MAX_WORKERS` (default `8`, clamped to `1`–`32`); raise it for a very large fleet, lower it
to be gentler on rate limits. It never silently drops accounts — every resolved account is covered,
and any it could not read is reported in `errors`.

`cross_account_performance` rides the same fan-out engine but reports **efficiency, not just raw
totals**: per-account CPM, CPC, CTR, cost-per-result, and ROAS, each **recomputed from summed base
components** (never an averaged ratio — Simpson's-paradox-safe). It also **normalizes money metrics
into a single `reporting_currency`** (default `USD`) so accounts billing in different currencies are
comparable; `ctr` and `roas` are currency-invariant and get no normalized twin. Conversion rates come
from a **static table checked into `config/fx_rates.json`** — a committed reference file (unlike the
gitignored `config/meta_ads_accounts.json`), seeded with USD/EUR/GBP/BRL/MXN/CAD/AUD. **These rates
are approximate and NOT live FX** — do not use them for billing or precise financial reporting; the
tool surfaces the table's `as_of` date (`fx_as_of`) and its caveat (`fx_note`) in every response so no
consumer mistakes them for live rates. **Live/Meta FX is deliberately deferred.** An account whose
currency is absent from the table keeps its native figures and native efficiency metrics, is reported
in `errors`, and is excluded from `normalized_total` (counted in `excluded_no_fx`). The primary-result
event per account comes from the config registry's `primary_result_action_type` when the account is
configured, and is otherwise inferred from the account's own `actions` — so the tool works before any
account is added to the config file.

`account_benchmark` is the **specialist-facing** counterpart to that manager-facing ranking view: it
answers "how does *this one* account stack up?" — e.g. "is this account's cost-per-lead good or bad
compared to its peers?". It is a pure post-processor over `cross_account_performance` (it re-reads
nothing from Meta): it calls that tool once for the cohort (the target account always included) and
ranks the target's efficiency metrics (CPM, CPC, cost-per-result, CTR, ROAS) as **percentiles within
the cohort**. A **high percentile always means "good"** for *both* cost metrics (a low CPM ranks high)
and quality metrics (a high ROAS ranks high), so the verdict ("better than most peers" … "worse than
most peers") reads the same direction everywhere. The cohort defaults to every account the token can
reach, or you can pass an explicit `cohort_ids` list. Money metrics are compared in one
`reporting_currency` (default USD) via the same static FX table, so a USD account benchmarks correctly
against peers billing in other currencies; ratio metrics (CTR/ROAS) are currency-invariant. Volume
metrics (spend/impressions/clicks/results) are deliberately **not** benchmarked — a "good" spend
percentile is ambiguous. It surfaces the cohort size and any excluded accounts (unreadable, or in a
currency with no FX rate), and a cohort with fewer than `MIN_COHORT_FOR_PERCENTILE` (5) readable
accounts is **flagged** (`too_small` / per-metric `unreliable`) rather than hidden — the numbers are
still returned, just labeled as thin.

`flag_accounts_needing_attention` turns a full-fleet review into a short **attention list** — "which of
my 200 accounts changed and need me *now*?". Like `account_benchmark` it is a pure post-processor over
`cross_account_performance`, but it calls it **twice**: once for a current window and once for the
immediately-preceding **equal-length** baseline window (override with `baseline_from` / `baseline_to`;
supplying exactly one of the two is an error). It joins the per-account rows by account and flags the
ones that *moved or breached a threshold*: `spend_spike` / `spend_collapse` (default a **50%** move),
`cost_per_result_degraded` / `cpc_degraded` / `ctr_dropped` (default a **30%** degradation),
`stalled_delivery` (an account that was delivering but now shows ~zero spend **and** impressions —
fired only when the account still reads `ACTIVE`, so a deliberately DISABLED account is not a false
stall), and `account_status_alert` (DISABLED / UNSETTLED / PENDING_RISK_REVIEW / … straight off each
row's status label). Low-volume windows are gated out (both windows must clear the material-spend floor,
and a cost-per-result flag needs enough results in both) so a 2→1 result swing on trivial spend never
trips an alarm; a brand-new account with no baseline reads `insufficient_history` or `newly_active`
(info), never a false ∞% spike. Output is bucketed **worst-first**: `flagged` (medium+ severity, sorted
by severity then absolute normalized-spend move then account id), `informational` (info-only), and a
`clean_count`; per-account read failures are isolated into `errors` (tagged with the window). Money
floors compare in one `reporting_currency` (default USD) via the same static FX table, and percent
moves use native figures (currency-invariant for a single account across two windows). Because it runs
two fan-outs it issues **~2× the per-account reads** of a single `cross_account_performance`
(~400 reads for a 200-account scope) — acceptable and documented. **Budget pacing is deliberately NOT
here:** spend-to-date vs. the configured budget is a different question over a different surface, owned
by the `pacing_report` tool; ad-level creative/disapproval detection (a heavier per-ad fan-out) is
parked for a later ticket.

`pacing_report` answers the manager's month-end question — "will each account land **over**, **under**,
or **on** its budget?" — across every account the token can reach (or an explicit list). Unlike
`account_benchmark` and `flag_accounts_needing_attention` (pure post-processors that add no new read
shape), pacing is a **two-source join**, because the budget configuration is not in the insights row.
Step 1 calls `cross_account_performance` once over `[date_from, effective_as_of]` for spend-to-date +
FX + scope; step 2 fans out a **second** read over the accounts that read OK — each reading
`list_campaigns` + `list_adsets` (budget fields only) + `get_account` (spend cap / lifetime spend) —
and joins the two by account. `date_from`/`date_to` are the **full reporting period** (e.g. a month);
`as_of` is the day spend is measured **through** (defaults to today, UTC) — the tool projects
end-of-period spend as `spend_to_date ÷ elapsed_fraction`. The pacing denominator is the sum of each
account's **ACTIVE daily budgets, CBO-deduplicated** (a campaign-budget-optimization campaign
contributes its campaign budget and its ad-set budgets are ignored — the double-count guard) × the
period length. The account **spend cap is a lifetime ceiling, reported as context but never the
denominator**; **lifetime budgets are reported but not projected** against an arbitrary period, so a
lifetime-only account is `budget_not_projectable`. Uncapped → `no_budget_set`, paused/closed →
`account_inactive`, and a per-account budget read that fails → `budget_unread` (distinct from a
genuinely uncapped account, so a read failure is never silently reported as "no budget"); none of
these are counted as under-pacing. Money is normalized into one `reporting_currency` (default USD) for
the rollup (status counts + worst over/under shortlists). Because step 2 issues **3 extra reads per
readable account** on top of step 1's `1 + N`, the whole call costs **~1 + 4N** reads for an
N-account scope — the same accepted posture as the attention tool's 2× note; a single combined
per-account read is a future optimization. Cents→major-unit conversion divides by 100, exact for
2-decimal currencies; **zero-decimal currencies (JPY, KRW) and 3-decimal currencies are a known 100×
inaccuracy** flagged for a follow-up.

`rank_accounts` answers the manager's "who's top/bottom?" — it ranks the whole reachable fleet (or an
explicit `account_ids` subset) by a **single** metric and returns the top or bottom `limit` (default
10). Like `account_benchmark` and `flag_accounts_needing_attention` it is a **pure post-processor over
`cross_account_performance`** (one call, no new read shape). Accepted metrics are `spend`, `cpm`, `cpc`,
`ctr`, `cost_per_result` (aliases `cpl`/`cpa` resolve to it; the canonical name appears in the output),
`roas`, `impressions`, `clicks`, and `results`; an unknown metric, a bad `order` (must be `asc`/`desc`),
or a non-positive `limit` is a fail-fast `ValueError` raised **before** any Meta read. Money metrics
(`spend`/`cpm`/`cpc`/`cost_per_result`) are ranked on their **`reporting_currency`-normalized twin** so
cross-currency accounts compare directly — `value` carries the normalized figure and `value_native` the
account's own-currency figure; ratio and count metrics (CTR/ROAS/impressions/clicks/results) are
currency-invariant and ranked as-is (no `value_native`). Ranks are 1-based with the strictly-better + 1
tie convention (ties share a rank, tiebroken by `ad_account_id` ascending for run-to-run determinism),
and the returned list is truncated to `limit` while `ranked_total` reports the full rankable count. An
account that lacks the metric — no delivery in range, or a money metric in a currency missing from the
FX table — is not sorted as a misleading `0`/`∞`; it lands in a separate `unranked` bucket tagged with
its reason (`metric unavailable` vs `no FX rate for <currency>`).

Its config lives in `.mcp.json` under `mcpServers` as the **`meta-suite`** entry — **promoted** so Claude
Code connects to it. Because it is an HTTP server, Claude Code only *connects*; you must **start the
process first** (`meta_mcp_server --mock` for mock mode, or with a real `META_ACCESS_TOKEN` for live), so
the entry shows a connection error until it is running. See
[**Run the Meta MCP server locally**](#run-the-meta-mcp-server-locally) below for the step-by-step launch,
`.mcp.json` wiring, and a scripted first session. Its tools carry
the `mcp__meta-suite__*` prefix, deliberately distinct from the community server's `mcp__meta-ads__*`
prefix (whose write tools are deny-listed in `.claude/settings.json`). Multi-user/hosted role headers
are a later concern; local single-operator use needs no header.

### Official Meta hosted MCP server (OAuth) — drop-in, optional

Meta also offers an **official hosted MCP server that authenticates with OAuth** (a remote/URL
server, not a long-lived token). It is **not required and not wired here** — single-operator use with
the current token is the supported path now; OAuth/multi-user is a documented later concern. Adopting
it later needs **no code change**, only config:

```jsonc
// remote/URL form (OAuth handled by the MCP client), added under "mcpServers"
"meta-ads-read": {
  "type": "http",
  "url": "https://<official-meta-hosted-mcp-endpoint>"
  // OAuth is negotiated by the MCP client; no token is stored in this file
}
```

Then point `META_READER_BACKEND=mcp` at it. Because both the community token server and the official
OAuth server satisfy the same `MetaReaderProvider` seam, swapping is config-only. **It is not wired or
tested here** — only the seam is proven to support it. The single-operator-now vs multi-user/OAuth-later
auth posture is documented in [`../AGENTS.md`](../AGENTS.md) under **Hybrid Meta integration → Auth
posture**.

## Run the Meta MCP server locally

This is the copy-paste path for trying our own custom server (the `meta-suite` connector) end-to-end:
**connect → read → propose → approve → execute**. It defaults to **mock mode** — no real Meta account,
no `META_ACCESS_TOKEN`, and zero live Meta calls — so you can exercise the whole guarded loop safely.
Going live is an explicit opt-in at the end.

Mock mode fakes only the reads and the write itself: it seeds one fake account (`act_mock001`) whose
reads return canned data, and routes `execute_plan` through a no-op write client that records the op and
returns success. **The guardrail pipeline is real and unchanged** — propose → human-approve → validate →
execute → verify, PAUSED-by-default, grounded, reviewed. In particular you still need an HMAC approval
secret to execute a write; without one the fail-closed gate refuses every write (reads still work).

### 1. Install the server extra

```powershell
pip install -e .[server]
```

### 2. Generate an HMAC approval secret (one-time)

Approval is an out-of-band HMAC signature (see [META_ACTION_WORKFLOW.md → Approval seam](META_ACTION_WORKFLOW.md#approval-seam--the-human-signs-the-agent-cannot)).
Generate a secret once and keep it **out of the repo**:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
# -> copy the output; you will use it as META_APPROVAL_SECRET in BOTH shells below
```

### 3. Launch in mock mode (no real token, no live calls)

```powershell
$env:META_APPROVAL_SECRET="<hex from step 2>"
meta_mcp_server --mock
# Server starts at http://127.0.0.1:8765/mcp
# [mock mode] No live Meta calls will be made. Account: act_mock001
```

`--mock` is also enabled by `META_MCP_MOCK=1`. Host/port precedence is unchanged (`--host`/`--port` >
`MCP_SERVER_HOST`/`MCP_SERVER_PORT` > `127.0.0.1`/`8765`). Leave this terminal running.

### 4. Connect Claude Code

The `meta-suite` entry is already in `.mcp.json` under `mcpServers`, so no edit is needed — Claude Code
connects to the running process at `http://127.0.0.1:8765/mcp`. Confirm the connection with `/mcp`, or by
asking Claude to call `server_info`. (Because it is an HTTP server, the entry shows a **connection error**
whenever the process in step 3 is not running — that is expected, not a misconfiguration.)

### 4b. Connect Claude Desktop instead (local stdio server)

On **Claude Desktop** — especially a **managed/corporate workspace where custom connectors are disabled
by org policy** — connect a *local* server over **stdio** rather than the HTTP URL. Claude Desktop launches
the process on demand from your machine: no port, no tunnel, no connector UI, and it is governed by the
local Developer config rather than the org connector policy. Use the `--stdio` flag.

1. Open **Settings → Developer → Edit Config** and add, at the **top level** of the JSON (a sibling of any
   existing keys — do not nest it inside another object):

   ```json
   {
     "mcpServers": {
       "meta-ads-mock": {
         "command": "/abs/path/to/.venv/bin/meta_mcp_server",
         "args": ["--stdio", "--mock"],
         "env": { "META_APPROVAL_SECRET": "<hex from step 2>" }
       }
     }
   }
   ```

   The `env` block passes the approval secret to the launched process (Claude Desktop does not read your
   shell env). Use the **same** secret when you `approve_plan` in the scripted session below. `--stdio` is
   also enabled by `MCP_STDIO=1`; `--host`/`--port` are ignored on this transport.

2. Fully **quit and reopen** Claude Desktop. The server appears under **Settings → Developer → Local MCP
   servers** with a `running` badge, and its tools are available in chat. Everything else — the guarded
   loop, out-of-band `approve_plan`, mock safety — is identical to the HTTP path; only the transport differs.

   Going live over stdio is the same opt-in as §7: drop `--mock` from `args` and add
   `"META_ACCESS_TOKEN": "<token>"` to the `env` block.

**Alternative: a wrapper-launcher pattern (what this account actually runs).** Instead of putting
`META_APPROVAL_SECRET`/`META_ACCESS_TOKEN` inline in the Desktop config's `env` block, `mcpServers` can
point `command`/`args` at a small Python launcher script (e.g. `~/.mcp-cowork-test/stdio_server_live.py`)
that loads the secret itself — typically from a local file like `~/.mcp-cowork-test/secret` — and sets
`os.environ["META_APPROVAL_SECRET"]` before building the server. **If you inspect the Desktop config and
don't see `META_APPROVAL_SECRET` in an `env` block, this is why — it's not missing, it's just loaded
inside the launcher instead.** Check the launcher script (`cat` the path in `command`/`args`) to find
where it reads the secret from before assuming you need to generate a new one.

When this pattern is used, there is usually a matching `approve.sh` next to the launcher (e.g.
`~/.mcp-cowork-test/approve.sh`) that loads that same secret and calls `approve_plan` for you — use it
instead of manually exporting `META_APPROVAL_SECRET` and calling `approve_plan` yourself:
```bash
~/.mcp-cowork-test/approve.sh --plan-id <uuid from step 3> --all
```

### 5. Scripted first session

Run these tool calls in order. The `→` lines show representative output.

```text
# Step 1 — health check
server_info()
# → {"name":"meta-ads-mcp","live_calls_enabled":true,"write_tools_enabled":true,
#    "approval_required":true,"approval_configured":true, ...}
#   (live_calls_enabled is a capability flag — it stays true in mock mode; approval_configured is
#    true only because you set META_APPROVAL_SECRET in step 3.)

# Step 2 — a read tool
list_campaigns(ad_account_id="act_mock001", fields=["id","name","status"])
# → [{"id":"campaign_mock001","name":"Demo Campaign","status":"ACTIVE", ...}]

# Step 3 — propose a write (returns a plan_id reference + a per-op summary, never an approvable body)
propose_set_status(account="act_mock001", id="ad_mock001", level="ad", status="PAUSED")
# → {"plan_id":"<uuid>","plan_type":"ops","ops":[
#      {"op":"set_status","id":"ad_mock001","status":"proposed", ...},
#      {"op":"set_status","id":"adset_mock001","status":"proposed", ...}]}
#   Note the SECOND op: ad_mock001 is the only ACTIVE ad in adset_mock001, so a companion ad-set pause
#   is appended (pausing the last live ad leaves the set live-but-not-delivering). Each op is
#   independently approvable.

# Step 4 — approve, OUT OF BAND, in a SEPARATE shell (this is the human's step, not a tool call)
#   Give the second shell the SAME secret, then sign the plan. `approve_plan` is a console script
#   installed into THIS project's venv, not your system PATH — use the venv path or activate it first,
#   e.g. `.venv/bin/approve_plan` (macOS/Linux) or activate then plain `approve_plan`. If a wrapper
#   launcher + `approve.sh` is in play (see §4b), use that instead — it already has the secret loaded:
#   `~/.mcp-cowork-test/approve.sh --plan-id <uuid from step 3> --all`
$env:META_APPROVAL_SECRET="<same hex from step 2>"
approve_plan --plan-id <uuid from step 3> --all
# → Approved 2 ops. Signature written to the proposal.

# Step 5 — preview: a local, write-free dry run of what execute would send (no Meta call)
preview_plan(plan_id="<uuid from step 3>")
# → shows would_send (the exact PATCH request) for each APPROVED op.
#   preview only renders the request for ops that are already approved — run it AFTER step 4, not before
#   (before approval it reports "not approved — would be skipped").

# Step 6 — execute (the ONLY tool that writes; validate pass first, then apply, then verify)
execute_plan(plan_id="<uuid from step 3>")
# → {"executed":true,"plan_id":"<uuid>","ops":[...],"follow_ups":[...]}
#   In mock mode the no-op write client recorded the writes and outcome verification re-read the fake
#   reader — no Meta call was made. A verify_next_day_spend follow-up is emitted for the pause.
```

### 6. Troubleshooting

- **"Connection refused" / server not found:** make sure `meta_mcp_server --mock` (step 3) is running in
  a terminal *before* you connect or call a tool.
- **`execute_plan` refused with an approval message:** you did not set `META_APPROVAL_SECRET` before
  launching (step 3), or you approved with a different secret than the server holds. `server_info` shows
  `approval_configured: false` when no usable secret is set.
- **Wrong port:** the URL in `.mcp.json` is `http://127.0.0.1:8765/mcp`; if you launched with a different
  `--port` / `MCP_SERVER_PORT`, they must match.
- **`zsh: command not found: approve_plan` (or `python`):** these are console scripts inside this
  project's `.venv`, not on your system `PATH`. Use `.venv/bin/approve_plan` (from the repo root) or
  activate the venv first (`source .venv/bin/activate`).
- **Can't find `META_APPROVAL_SECRET` in the Claude Desktop config:** if `mcpServers` points at a
  wrapper launcher script instead of `meta_mcp_server` directly (see §4b), the secret is loaded inside
  that script (often from a local file), not from an inline `env` block. Read the launcher script to find
  where it loads the secret from, and use the matching `approve.sh` helper if one exists next to it —
  don't generate a fresh secret, since it won't match what the running server already holds.

### 7. Go live (opt-in)

Only after the mock loop above behaves as expected:

- **Use a sandbox / test ad account for the first live run** — Meta's Ads Sandbox, or a low-budget real
  account you fully control. Drop `--mock` and set a real token:
  ```powershell
  $env:META_APPROVAL_SECRET="<hex>"
  $env:META_ACCESS_TOKEN="<token>"
  meta_mcp_server
  ```
- **`ads_read` is enough to read and to *validate*.** With a read-only token, reads work and
  `execute_plan` fails its mandatory `validate_only` pass with a clear `ads_management` scope error —
  **zero spend risk**. To actually execute a write, the token also needs `ads_management`.
- **Verify next-day spend = $0** after the first pause (a `PAUSED` write *registering* is necessary but
  not sufficient proof delivery stopped — same-day spend can still post; `execute_plan` emits a
  `verify_next_day_spend` follow-up for exactly this). This is the repo's build-safety rule.

### 8. Single-operator note

This is a **single-operator, local** setup. Multi-user auth, roles, and server-side (Azure-hosted)
approval state are a separate backlog item (`mcp-role-based-access-tiers`) that drops in behind the same
`ApprovalGate` seam. Do not treat this local rig as the production shape.

To hand a *second* person their own machine (Cowork-only, scoped to one account) ahead of that
real multi-user work landing, see [SPECIALIST_ONBOARDING.md](SPECIALIST_ONBOARDING.md) and
`scripts/onboard_specialist.sh`.

## Notes

- Reads now flow through a swappable provider seam (`MetaReaderProvider` in `reader_provider.py`) so an MCP read backend can supply reads without touching call sites; writes stay on the direct Graph API client. See **Read backend: direct vs MCP** above; the read model, auth posture, and full guarded-write catalog are in [`../AGENTS.md`](../AGENTS.md) under **Hybrid Meta integration**.
- The reporting sync (`sync-api`) is read-only. It does not modify account settings or ads.
- Writes (action execution and audience rotation) go through the same Graph API client but only run with an explicit `--execute` flag and an `ads_management`-scoped token.
- V1 runs one account at a time.
- V1 preserves the same raw CSV contract the manual export workflow uses today.
