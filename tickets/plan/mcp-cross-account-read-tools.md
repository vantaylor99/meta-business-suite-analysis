description: Add a tool that lists every ad account the access token can reach, plus tools that answer questions across all of those accounts at once — without anyone first having to hand-list the accounts in the config file. Reading is allowed for any account the token sees; changes still go through the configured accounts.
prereq:
files: src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/account_registry.py, docs/META_API_SETUP.md, README.md, tests/
difficulty: medium
----
## Why

Today the custom Meta MCP server (`meta-suite` in `.mcp.json`) can only read an account once
its `ad_account_id` is known to the caller, and cross-account work means the operator hand-lists
each account in `config/meta_ads_accounts.json`. There is no way to ask "what ad accounts does
this token even reach?" from inside the server, and no way to answer a question spanning all of
them in one step. The Meta Graph API already exposes this via the `/me/adaccounts` edge (documented
as a manual `curl` sanity-check in `docs/META_API_SETUP.md:65`); this ticket promotes it into a
first-class read capability and builds cross-account querying on top of it.

## Decisions already made (do not re-litigate)

Locked with the operator on 2026-07-09:

1. **Reads are open to every account the token can reach.** This is the intended behavior, not a
   gap. The existing per-account read tools already accept a raw `ad_account_id` with no registry
   check (`build_read_tools` in `mcp_server.py`), so this direction is consistent with today's read
   path — do **not** add a registry gate to reads.
2. **Writes stay soft / config-preferred (no change in this ticket).** The write/propose tools keep
   resolving through `config/meta_ads_accounts.json` first and keep their existing raw-`act_<id>`
   fallback (`_resolve_account` in `mcp_server.py`). Do **not** tighten writes here. Hard
   write-enforcement is tracked separately in `mcp-registry-account-enforcement` (backlog) and is
   explicitly deferred.
3. **Ship both discovery and aggregation.** A `list_ad_accounts` discovery tool AND cross-account
   aggregate read tool(s) that fan out internally and return combined results in one call.
4. **No hard security boundary is required for the first version.** The token's own Meta permission
   scope remains the real access boundary; this feature intentionally surfaces the full reach.

## What to build

### A. Discovery tool — `list_ad_accounts`
A new read tool that takes **no `account` argument** (the one read tool that works before any
config exists). Calls the Graph `/me/adaccounts` edge with the shared env token and returns one row
per reachable account.

- Client method on `MetaMarketingApiClient` (`meta_api.py`), mirroring the existing `list_*`
  methods and reusing `iter_paginated` (pagination already handled):
  ```python
  def list_ad_accounts(self, *, fields: list[str]) -> list[dict[str, Any]]:
      params = {"fields": ",".join(fields), "limit": 200}
      return list(self.iter_paginated("/me/adaccounts", params=params))
  ```
- Reader passthrough: add to the `MetaReaderProvider` interface, implement on `DirectMetaReader`
  (delegates to the client), and implement on the mock reader (`build_mock_reader` /
  `FakeMetaReader`) so `--mock` mode returns the single seeded mock account.
- Register in `build_read_tools` + `READ_TOOL_DESCRIPTIONS` in `mcp_server.py`.
- Default fields: `account_id`, `name`, `account_status`, `currency`, `timezone_name`,
  `amount_spent`, `business`. Translate `account_status` to a human label alongside the raw code
  (1=ACTIVE, 2=DISABLED, 3=UNSETTLED, 7=PENDING_RISK_REVIEW, 9=IN_GRACE_PERIOD, 101=CLOSED, …) so
  Cowork can relay it in plain language.

### B. Cross-account aggregate read tool(s)
At least one tool that answers a question over all reachable accounts (or an explicit subset of
account ids) in a single call, by listing accounts (A) then fanning out existing per-account reads.

- Start with a **cross-account performance/spend summary**: for each account, pull the account-level
  info (`get_account`) and an insights summary (`fetch_insights` at `level=account` over a supplied
  date range), and return a combined table plus totals.
- Accept an optional explicit list of account ids to scope the fan-out; when omitted, use every
  account from `list_ad_accounts`.
- The plan agent decides the exact tool surface (one summary tool vs. a small family), but keep the
  fan-out logic in the library layer (testable), not inline in the FastMCP closure.

## Edge cases & interactions

- **Empty reach:** `/me/adaccounts` returns no accounts — return an empty list cleanly, never an
  exception; the summary tool returns an empty result with a clear "no accounts reachable" signal.
- **Token lacks permission / Graph error:** surface as a clean FastMCP `ToolError` via the existing
  `_wrap_tool_errors` path, with a message that reads sensibly to a non-technical Cowork user.
- **Mixed currencies:** the aggregate MUST NOT naively sum spend across accounts with different
  `currency` values — group/subtotal by currency (or label each row's currency and omit a single
  grand total). This is a correctness requirement, not a nicety.
- **Partial fan-out failure:** if one account errors mid-fan-out (permission, rate limit), return
  the successful rows plus a per-account error marker rather than failing the whole call; surface
  which accounts failed and why.
- **Many accounts / rate limits:** fan-out over a large token could hit Graph rate limits — the
  existing client already retries `429`; keep the fan-out sequential (or lightly bounded) and rely
  on that, don't add unbounded concurrency.
- **Mock mode:** both new tools must work under `--mock` with zero live calls (single mock account),
  so the mock reader needs a `list_ad_accounts` implementation.
- **`server_info` unaffected:** it stays token-free; do not make it depend on account discovery.
- **Write path untouched:** confirm no change to `build_write_tools` / `_resolve_account`.

## Docs

- Update `docs/META_API_SETUP.md` to point at the new tool instead of (or alongside) the manual
  `/me/adaccounts` curl check.
- Note the new tools in `README.md`'s tool inventory.

## Key tests (TDD targets for the implement stage)
- `list_ad_accounts` returns normalized rows (with status label) from a `FakeMetaReader` seeded with
  multiple accounts; empty-reach case returns `[]`.
- Permission/Graph error maps to a `ToolError` with an operator-readable message.
- Cross-account summary groups spend by currency and never sums across currencies.
- Partial fan-out failure yields successful rows + per-account error markers.
- `--mock` smoke: both tools return the seeded mock account with no live call.
- Regression: write tools and `_resolve_account` behavior unchanged (raw-id fallback still works).
