description: Wire up a community Meta "read" server that works with the access token we already have, so the agent can pull ad data through it as an alternative to calling Meta directly — and write down how to swap in Meta's official hosted login-based server later, without requiring that login now.
prereq: meta-read-provider
files: .mcp.json, AGENTS.md, src/meta_ads_analysis/reader_provider.py, docs/META_API_SETUP.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## Why

The LOCKED decision: wire a **COMMUNITY token-based Meta MCP read server now** (it works with the
existing `META_ACCESS_TOKEN` — no OAuth flow), and **DOCUMENT the official Meta hosted OAuth server
as a drop-in** that is NOT required to adopt now. This rides on `meta-read-provider`: the reader ABC
from that ticket is the seam the MCP backend plugs into.

## SAFETY GUARDRAILS FOR THIS TICKET (read first — unattended agent)

- **NEVER run, install-and-execute, or otherwise invoke the community MCP server against live Meta
  during this ticket.** Do NOT execute `npx -y <package>`, do NOT exercise `META_ACCESS_TOKEN`, do
  NOT make any network call to Meta or to a real MCP server. The deliverable is config + code +
  mocked tests only.
- **All tests are mock-only** against a fake tool-executor (see below). No live MCP / Meta calls.
- **If you cannot identify a community package with high confidence**, do NOT guess-install an
  arbitrary one. Land the `.mcp.json` entry as a clearly-marked, commented/placeholder config (with a
  `TODO: vet and pin <package>@<version>` note) and ship the `MCPMetaReader` + mocked tests anyway.
  The substantive deliverable is the seam + the tested translation layer, not a live integration.

## What to build

### 1. `.mcp.json` — add the community server (config only, not executed)

Append a second server alongside the existing `code-search` entry, following the exact shape already
in the file (`type: stdio`, `command`, `args`, `env`):

```json
{
  "mcpServers": {
    "code-search": { "...existing, do not modify..." },
    "meta-ads-read": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "<community-meta-marketing-mcp-package>"],
      "env": { "META_ACCESS_TOKEN": "${META_ACCESS_TOKEN}" }
    }
  }
}
```

Identify (do NOT run) a candidate community token-based Meta Marketing MCP package — one that
authenticates with a long-lived user/system-user token, NOT OAuth. Record the candidate name +
version in the review handoff and in `docs/META_API_SETUP.md` as "candidate, unvetted — operator must
review before enabling." Per the safety guardrails above, if no candidate is confidently
identifiable, leave the `meta-ads-read` entry commented out with a placeholder package token and a
clear TODO. Either way the `.mcp.json` shape and the `MCPMetaReader` (below) must land. The existing
`code-search` entry must be byte-for-byte untouched (it has `env: {}`).

### 2. `MCPMetaReader` in `reader_provider.py`

An implementation of the `MetaReaderProvider` ABC that routes each read to the MCP server's
equivalent tool, translating arguments to the tool's input schema and the tool result back into the
same dict/list shapes `DirectMetaReader` returns (so downstream parsing is identical). It takes a
tool-executor callable injected at construction (the agent-SDK MCP call surface) — do NOT hard-code a
transport, and do NOT construct/connect a real server. Methods the community server does not expose
raise a clear `NotImplementedError` naming the missing tool, so the operator can fall back to
`DirectMetaReader` for those reads.

**Mock-only tests:** test `MCPMetaReader` against a fake tool-executor that returns canned
Meta-shaped dicts; assert the argument translation and the result shape match `DirectMetaReader`'s
contract. NO live MCP / Meta calls.

### 3. Provider selection

Add a single selection point (env var `META_READER_BACKEND=direct|mcp`, default `direct`) read at the
`*.from_env()` / entry-point construction added in `meta-read-provider`. Default stays `direct` so
nothing changes unless explicitly opted in. Document the toggle.

### 4. Document the official OAuth drop-in (do NOT require it, do NOT wire it)

In `docs/META_API_SETUP.md` and the AGENTS.md read-model note, add a section: "Official Meta hosted
MCP server (OAuth) — drop-in, optional." Describe that it is a remote/hosted MCP server using OAuth
(not a long-lived token), how it would be added to `.mcp.json` (a remote/URL form with OAuth), and
that adopting it requires no code change — only a new `.mcp.json` entry plus pointing
`META_READER_BACKEND` at it — because both satisfy the same `MetaReaderProvider` seam. Make explicit:
single-operator with the current token is the supported path NOW; OAuth/multi-user is a documented
later concern, not built here (cross-reference the auth note that lands in the docs ticket).

## TODO

- Identify (do NOT run) a candidate community token-based Meta MCP package; record name + version as
  unvetted. If none is confident, leave a commented placeholder entry.
- Add the `meta-ads-read` server to `.mcp.json` (do not modify `code-search`; do not execute it).
- Implement `MCPMetaReader(MetaReaderProvider)` with injected tool-executor + arg/result translation
  + `NotImplementedError` for unsupported tools.
- Add `META_READER_BACKEND` selection (default `direct`).
- Mock-only tests for `MCPMetaReader` arg translation + result-shape parity with `DirectMetaReader`,
  plus a default-off test proving `META_READER_BACKEND` unset == DirectMetaReader behavior.
- Document the community server setup AND the official OAuth drop-in in `docs/META_API_SETUP.md`;
  add the read-model pointer in AGENTS.md.
- Run `.venv/bin/python -m pytest tests/ -q` green.

## Edge cases & interactions

- **Field-list translation** — Meta read methods take `fields=[...]`; many MCP tools take a comma
  string or a fixed field set. Translate explicitly and assert the round-trip; a dropped field
  silently blanks downstream metrics (the exact failure mode the confidence engine punishes).
- **Pagination semantics** — if the community tool auto-paginates, `MCPMetaReader.iter_paginated`
  must still expose an iterator; if it returns a single page, the wrapper must paginate or raise
  rather than silently truncate. Decide and test.
- **Partial coverage** — the community server likely won't expose every read (e.g.
  `get_delivery_estimate`, `search_targeting`). `NotImplementedError` must name the tool so a caller
  can fall back to `direct` for that one read; document which reads are MCP-covered.
- **Token scoping** — the existing token has `ads_read`; some reads (`get_adset` re-read before
  write) want `ads_management`. The MCP read path is read-only; writes never go through it. State that
  the MCP reader is reads-only and writes always use the direct client.
- **`.mcp.json` is committed** — must not embed a literal token; use `${META_ACCESS_TOKEN}`
  interpolation (or env passthrough) so the secret stays in the environment. Verify the existing
  `code-search` entry is untouched.
- **Default-off safety** — with `META_READER_BACKEND` unset, behavior is byte-identical to today
  (DirectMetaReader). Pin this with a test so adding the server can't accidentally change production
  reads — and because the server is never actually executed in this ticket, this test is the only
  behavioral guarantee that lands.