# Meta API Setup

This repo can fetch Meta ads data directly from the Marketing API with:

```powershell
python -m meta_ads_analysis sync-api --account pollen_sense --run-date 2026-04-22
```

## What You Need

- A Meta app with access to the ad accounts you manage
- A working access token:
  - `ads_read` is enough for reporting sync, dry runs, and live-state reads
  - `ads_management` is required to *execute* actions (`apply-actions --execute`, `apply-rotation --execute`)
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
  `get_delivery_estimate`, `search_targeting`, `list_pixels`, `list_custom_conversions`, and the raw
  `iter_paginated` escape hatch. Each raises a clear `NotImplementedError` naming the read.
- **Pagination:** the candidate does not auto-paginate; `MCPMetaReader` follows `paging.next` via the
  server's `meta_ads_fetch_pagination_url` tool and **refuses to silently truncate** (it raises if a
  page is dropped and no pagination tool is configured).

The MCP backend is consumed by the **agent runtime**, which injects the MCP tool-call surface into
`MCPMetaReader(tool_executor=...)`. The pure-Python CLI cannot synthesize that surface, so running a
CLI command with `META_READER_BACKEND=mcp` raises a clear error rather than silently degrading — keep
CLI/sync runs on `direct`.

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
OAuth server satisfy the same `MetaReaderProvider` seam, swapping is config-only. (The single-operator
vs multi-user / OAuth auth model is cross-referenced in the auth note that lands with the
`hybrid-model-docs-and-tool-catalog` ticket.)

## Notes

- Reads now flow through a swappable provider seam (`MetaReaderProvider` in `reader_provider.py`) so an MCP read backend can supply reads without touching call sites; writes stay on the direct Graph API client. See **Read backend: direct vs MCP** above; full hybrid-model docs land in the `hybrid-model-docs-and-tool-catalog` ticket.
- The reporting sync (`sync-api`) is read-only. It does not modify account settings or ads.
- Writes (action execution and audience rotation) go through the same Graph API client but only run with an explicit `--execute` flag and an `ads_management`-scoped token.
- V1 runs one account at a time.
- V1 preserves the same raw CSV contract the manual export workflow uses today.
