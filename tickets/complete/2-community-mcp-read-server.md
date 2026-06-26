description: Reviewed the new "read Meta ads data through an MCP server" option — the translation layer between our app and the server, the disabled config pointing at a community server, and the docs for switching to Meta's official login-based server later.
prereq:
files: src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/rotation.py, .mcp.json, docs/META_API_SETUP.md, AGENTS.md, tests/test_meta_ads_analysis.py
difficulty: medium
----
## What shipped

A swappable **read seam** so Meta reads can come from an MCP server instead of the direct Graph
client, defaulting to `direct` so nothing changes until an operator opts in. Config + translation
code + mock-only tests; **no live MCP/Meta call is made anywhere.**

- `MCPMetaReader(MetaReaderProvider)` — drop-in reader that translates each read to a named MCP tool
  via `DEFAULT_MCP_TOOL_MAP`, normalizes the executor's raw return (bare list / `{"data":[...]}`
  envelope / single node / JSON string) back to `DirectMetaReader`'s shapes, drains `paging.next`
  rather than silently truncating, and raises `NotImplementedError` (naming the read) for reads the
  candidate doesn't expose.
- `reader_from_env()` — single selection point on `META_READER_BACKEND` (`direct` default | `mcp`),
  wired into `actions.py:enrich_action_plan_with_live_state` and `rotation.py:fetch_active_adsets`.
  `mcp` without an injected executor raises (the CLI can't synthesize the agent MCP surface).
- `.mcp.json` — community candidate parked under non-launched `_candidateMcpServers`; `code-search`
  untouched.
- Docs in `docs/META_API_SETUP.md` + pointer in `AGENTS.md`.

## Review findings

### Checked
- **Implementation diff** (`git show a269876`) read first, fresh, before the handoff summary.
- **Drop-in / ABC parity** — `MCPMetaReader` implements every `MetaReaderProvider` abstract method;
  `test_mcp_reader_signatures_match_client_exactly` enforces signature parity against the real
  client via the existing `_sig_params` helper (a meaningful check, not a tautology).
- **Arg translation** — `fields` list → comma string (1:1, no drop), insights window → `time_range`,
  breakdowns → list, account id → `act_id`. Traced each read method.
- **Result translation** — list/envelope/node/JSON-string branches in `_split_page` / `_call_node` /
  `_decode`. Pagination drain loop and the `pagination_tool is None` refusal traced by hand.
- **Provider selection + call-site rewiring** — both `*.from_env()` construction points now route
  through `reader_from_env()`; `grep`/`find_references` confirmed no other reader-construction site
  was missed; supplied readers still short-circuit `client_from_env` (laziness preserved).
- **`.mcp.json`** — parses; only `code-search` is under `mcpServers`; candidate is under the
  non-launched `_candidateMcpServers` key; no literal token committed (`${META_ACCESS_TOKEN}`).
- **Docs** — `META_API_SETUP.md` and `AGENTS.md` sections match the shipped behavior (default-direct,
  reads-only, covered/uncovered read lists, pagination, official-OAuth-as-config-only-later).
- **Lint + tests** — no ruff/mypy configured in this repo (only `pytest`); byte-compiled all four
  touched files clean. Full suite: **214 passed** (was 208; +6 added this pass). No pre-existing
  failures.

### Found and fixed inline (minor)
- **Untested translation/error branches.** The implementer's suite covered the happy paths but left
  several branches unexercised. Added 6 mock-only tests in `tests/test_meta_ads_analysis.py`:
  - `test_mcp_reader_node_unwraps_single_object_data_envelope` — the `{"data": {...}}` node-unwrap
    branch of `_call_node` (previously only the bare-dict path was tested).
  - `test_mcp_reader_raises_on_non_json_string_result` — `_decode` malformed-JSON → `MetaApiError`.
  - `test_mcp_reader_list_read_rejects_unexpected_result_shape` — scalar result → `MetaApiError`
    naming the read (no silent coercion to empty).
  - `test_mcp_reader_node_read_rejects_non_object_result` — list-where-object-expected → error.
  - `test_mcp_reader_drains_three_pages_and_passes_each_next_url_to_pagination_tool` — >2-page drain
    and verifies the pagination tool receives each exact `paging.next` URL in order.
  - `test_mcp_reader_aborts_runaway_pagination_at_max_pages` — the `MAX_PAGES` runaway guard fires
    (instance override keeps it cheap).

### Found, not blocking (documented, no ticket filed)
- **`_call_node` `{"data": {...}}` unwrap is ambiguous** if a node read ever legitimately carries a
  dict field literally named `data`. Low risk for the node reads actually mapped
  (`get_campaign`/`get_adset`/`get_ad`/`get_account` — none of these Graph nodes carry a `data`
  object), and `get_delivery_estimate` is mapped to `None` (unsupported) so its real `{"data":[...]}`
  shape never reaches this path. Left as-is; flagged here for whoever vets the real package.
- **Translation arg-key correctness against the real, unvetted `meta-ads-mcp-server` package** is the
  genuine live-risk (tool names, `act_id`, `META_ADS_ACCESS_TOKEN`, `time_increment` acceptance all
  came from web/npm docs, not from running the package). This is **operator-gated by design**: the
  candidate sits disabled in `.mcp.json`, there is no working `mcp` caller yet, and enabling requires
  manual vetting per the `_TODO`/docs. Not a defect in this seam and not actionable until vetting —
  so no new `fix/` ticket; the `_candidateMcpServers._TODO` note and `docs/META_API_SETUP.md` already
  capture it, and the broader hybrid-model auth/catalog work lives in
  `hybrid-model-docs-and-tool-catalog`.
- **No production caller constructs `MCPMetaReader` yet** (agent-runtime tool-executor injection is a
  separate concern downstream in the hybrid-model ticket chain). Intentional; the seam defaults off
  and the CLI raises loudly on `mcp`. Noted, not filed.

### Empty categories
- **No major findings** → no new `fix/`/`plan/`/`backlog/` ticket created. The two real risks above
  are either out-of-scope-until-vetting (operator-gated) or already-tracked downstream, not defects
  in the delivered seam.
- **No regressions** — the default-off path is covered by
  `test_entry_point_default_reads_through_direct_when_backend_unset` and the full suite is green.

## Validation
- `.venv/bin/python -m pytest tests/ -q` → **214 passed**.
- `python -m py_compile` on all four touched files → clean.
- `.mcp.json` parses; only `code-search` launches.
