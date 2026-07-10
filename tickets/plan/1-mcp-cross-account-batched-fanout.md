description: Make the "summarize across all my ad accounts" feature actually work when there are hundreds of accounts, instead of timing out. Also settle how a caller says which accounts a request covers.
prereq:
files: src/meta_ads_analysis/account_discovery.py, src/meta_ads_analysis/mcp_server.py, src/meta_ads_analysis/reader_provider.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md, README.md
difficulty: medium
----
## Problem

`cross_account_spend_summary` fans out per-account reads **sequentially** and, with no
`account_ids`, targets **every** reachable account. In practice the token reaches ~792 accounts
(≈200+ actively managed), and the all-accounts call **times out** (observed MCP `-32001`). A tool
whose whole point is "look across all my accounts" is unusable at the scale the admin persona
actually has. It only returns when handed a small explicit list (~20).

This ticket makes the cross-account fan-out scale to the real account count, and defines the
**scope contract** that every future multi-account tool will share.

## Scope contract (shared by all multi-account tools)

We are deliberately **not** building account groups, roles, or per-token scoping yet — that
decision (dynamic groups vs. purpose-scoped tokens) is unresolved and lives in
`mcp-role-based-access-tiers` (backlog). Until then:

- The default scope is **one implicit group = every account the token can reach**.
- Scope is still expressed as an explicit, first-class parameter (`account_ids: list | None`,
  `None` → all reachable) so the same tool serves a specialist pointing at ~15 accounts and the
  WWFT pointing at all of them — and so a real grouping layer can slot in later without changing
  any tool's signature.
- A single resolution seam (e.g. a `resolve_scope(reader, account_ids)` helper in
  `account_discovery.py`) turns the parameter into a concrete, de-duplicated account list. Every
  multi-account tool calls this seam rather than re-implementing "None means all." Grouping later
  becomes a change to this one seam.

## What this ticket must deliver

- **Concurrent / batched fan-out** for the cross-account read path so a request over hundreds of
  accounts completes well within the MCP client timeout. Bounded concurrency (a small fixed worker
  pool or chunked batches), cooperating with the client's existing 429 retry rather than
  hammering the Graph API.
- **Partial results over all-or-nothing.** A slow or failing account must not sink the whole call:
  per-account failures/timeouts are recorded in the existing `errors` channel and the call returns
  what succeeded, exactly as the current per-account `MetaApiError` handling does.
- **Deterministic output** regardless of completion order — rows and per-currency subtotals must
  not depend on which worker finished first.
- The existing `cross_account_spend_summary` output shape (per-account rows, per-currency subtotals,
  no cross-currency grand total, `errors`, `account_count`/`reachable_count`) is preserved. This is
  an engine swap under the same contract, plus the shared `resolve_scope` seam.

## Behavior / interface (proposal — plan stage to finalize)

- `resolve_scope(reader, account_ids=None) -> list[str]` in `account_discovery.py`: normalizes
  (`act_` prefix), de-duplicates order-preserving, `None` → all reachable via `list_ad_accounts`.
- Fan-out helper that maps a per-account read over the resolved list with bounded concurrency and
  collects `(row | error)` per account.
- MCP surface unchanged in signature; only faster and safe at scale.

## Edge cases & interactions

- Very large scope (700+ accounts): must stay under the client timeout; if genuinely unbounded,
  define and document a ceiling + explicit truncation signal rather than silently dropping accounts.
- Mixed currencies across the fleet — subtotals stay grouped by currency; never summed across.
- Per-account 429 / transient error / permission error → recorded in `errors`, others still return.
- Duplicate ids in an explicit list (`["1","act_1"]`) → counted once (regression: this was fixed in
  `cross_account_spend_summary`; keep it fixed through the shared seam).
- Concurrency must not reorder or double-count subtotals; `account_count` vs `len(accounts)` still
  distinguishes attempted vs. succeeded.
- Mock reader path (`build_mock_reader`) must still make zero live calls and stay deterministic.

## Use cases

- WWFT / manager: one call summarizing all ~200 managed accounts returns instead of timing out.
- Specialist: same call scoped to their ~15 accounts (explicit ids) returns quickly.
- Foundation for tickets `mcp-cross-account-performance`, `mcp-rank-accounts`,
  `mcp-flag-accounts-attention`, `mcp-account-benchmark`, `mcp-pacing-report`, which all consume the
  fan-out engine and the `resolve_scope` seam.
