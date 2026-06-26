description: A swappable "reader" layer now sits in front of every place the app reads from Meta, so reads can later come from a different backend (e.g. an MCP server) and tests never touch the live Meta API.
prereq:
files: src/meta_ads_analysis/reader_provider.py, src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md
difficulty: medium
----
## What shipped

A provider seam for **reads** (`reader_provider.py`):

- `MetaReaderProvider` (ABC) — one abstract method per read the app actually performs (14, in
  `READ_METHODS`), signatures mirroring `MetaMarketingApiClient` exactly. Writes
  (`create_*`/`update_*`/`upload_*`/`get_video`) are deliberately not abstracted.
- `DirectMetaReader` — 1:1 pass-through to a live client (current behavior, byte-for-byte);
  `from_env()` builds the client lazily; wrapped client is private (no read→write back-door).
- `FakeMetaReader` — test double; canned values/callables by method name, `NotImplementedError`
  on unstubbed reads, `ValueError` on unknown stub name, records `.calls`, re-iterable
  `iter_paginated`.
- `as_reader()` — normalizes a reader-or-client into a provider; `None` passes through.

Call sites migrated: read-only entry points renamed `client:`→`reader:` (accept either, normalized
at top); mixed read+write functions (`apply_ops_plan`, rotation `apply_*`) keep the concrete
`client` for the write and gained an optional `reader` for the live re-read (defaulting to reading
through the same client); write-only functions untouched. CLI keyword call sites updated.

## Review findings

**Scope of review:** read the full implement diff (commit `28f72cd`) before the handoff, then
re-derived completeness, defaulting logic, caller compatibility, docs, and test coverage from the
code itself.

### Checked — and clear
- **Read-surface completeness.** Enumerated every method on `MetaMarketingApiClient`: the 14 in
  `READ_METHODS` are exactly its read methods. `get_video` is a read but is correctly excluded —
  it belongs to the authoring/write flow, not the read seam.
- **Migration completeness.** `grep` confirms zero `client.<read>` calls remain anywhere in `src/`
  outside `reader_provider.py`; every read now flows through `reader.<read>`.
- **Reader-defaulting logic.** Walked all four reader/client combinations through `apply_ops_plan`
  and the three rotation mixed-apply functions. All correct. `client` is a **required positional**
  in the rotation apply functions, so `as_reader(reader) or as_reader(client)` can never collapse
  to `None`. In `apply_ops_plan` the lazy `client_from_env()` → `as_reader(effective_client)`
  ordering is sound.
- **Caller compatibility.** The `client:`→`reader:` rename is breaking for keyword callers; swept
  `src/`, `tess/`, `conftest.py`, docs, and README — the only remaining `client=` keyword call is
  `apply_action_plan` (write-only, intentionally untouched). No broken callers.
- **Docs.** `META_API_SETUP.md` gained an accurate one-line pointer to the seam (full hybrid docs
  are deferred to `hybrid-model-docs-and-tool-catalog`). No doc/README code example references the
  renamed parameters.
- **Error handling / resource cleanup.** The reader layer is a pure pass-through; it opens no
  resources and adds no swallowed error paths. Existing `MetaApiError` handling at call sites is
  unchanged. N/A by construction.

### Found — and fixed inline (minor)
- **Hybrid distinct-reader path was untested.** Only the default-derive path (`reader is None` →
  wrap the write client) was exercised. Added
  `test_apply_ops_plan_routes_read_through_reader_and_write_through_client`: passes a
  `FakeMetaReader` for the read and a **write-only** recording client (no read methods) for the
  write, and asserts the live re-read hit the reader while the write hit the client — if the read
  leaked to the client it would `AttributeError`. This is the seam's central novel behavior and the
  one the MCP backend will rely on. Test suite: 192 → **193 passing**.

### Found — accepted (documented, not defects)
- **`experiment.py:read_experiment` not converted.** Still types its first param
  `client: MetaMarketingApiClient` and passes it positionally to `fetch_entity_metrics`, which
  normalizes via `as_reader` — so it works correctly through the seam. It is outside the ticket's
  file list and is a consistency gap, not a bug; renaming would touch a public signature + its CLI
  caller for marginal benefit. Left as-is. Fold into a future cleanup if a full "every read entry
  point is first-class reader" pass is wanted.
- **`as_reader` wraps any non-provider object**, so a typo'd object fails at first read call rather
  than at wrap time. Intentional — it's what keeps the existing `FakeClient` doubles working
  without a static checker. No runtime type check added.
- **No static type checker configured** (no mypy/pyright/ruff). The `MetaReaderProvider |
  MetaMarketingApiClient` unions are only runtime-normalized; the signature-pinning test
  (`test_reader_signatures_match_client_exactly`) is the drift guard. Pre-existing repo condition.
- **`FakeMetaReader.iter_paginated` is single-pass if seeded with a one-shot generator** — seed
  with a list (documented in the docstring).

### Major findings
None. No new fix/plan/backlog tickets filed.

## Validation
- `.venv/bin/python -m pytest tests/ -q` → **193 passed**.
- No live Meta calls in any test (MOCKS ONLY rule upheld).

## Out of scope (next tickets)
- The MCP read backend itself (`community-mcp-read-server`).
- Full hybrid-model docs + tool catalog (`hybrid-model-docs-and-tool-catalog`).
