description: Review the new "reader" layer that sits in front of every place the app reads from Meta, so reads can later come from a different backend and tests never call Meta.
prereq:
files: src/meta_ads_analysis/reader_provider.py (new), src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, src/meta_ads_analysis/cli.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md
difficulty: medium
----
## What was built

A provider seam for **reads**. New module `src/meta_ads_analysis/reader_provider.py`:

- `MetaReaderProvider` (ABC) — one `@abstractmethod` per read method actually called in `src/`,
  signatures mirroring `MetaMarketingApiClient` exactly (keyword-only split + defaults). The 14
  methods are listed in the module constant `READ_METHODS`: `fetch_insights`, `fetch_ads`,
  `list_campaigns`, `get_campaign`, `list_adsets`, `get_adset`, `get_ad`, `list_custom_audiences`,
  `get_account`, `get_delivery_estimate`, `search_targeting`, `list_pixels`,
  `list_custom_conversions`, `iter_paginated`. Write methods (`create_*`/`update_*`/`upload_*`/
  `get_video`) are deliberately **not** abstracted.
- `DirectMetaReader` — wraps a `MetaMarketingApiClient` and delegates each read 1:1 (current
  behavior, byte-for-byte). `from_env(api_version=None)` classmethod builds a client lazily.
  `iter_paginated` returns the underlying iterator unchanged (laziness preserved). The wrapped
  client is stored privately (`self._client`) with **no public `.client` accessor** — there is no
  reader→client write back-door.
- `FakeMetaReader` — test double seeded by method name with a value or a callable; raises
  `NotImplementedError` on any unstubbed read and `ValueError` on an unknown stub name. Records
  calls in `.calls`. `iter_paginated` returns a fresh iterator per call (re-iterable / `list()`
  safe).
- `as_reader(reader_or_client)` — normalizer: returns a `MetaReaderProvider` unchanged, wraps
  anything else in `DirectMetaReader`, passes `None` through (for lazy-default callers).

### How call sites were migrated (the chosen, consistent shape)

- **Read-only entry points / helpers** — renamed the `client:` param to `reader:`, accept EITHER a
  `MetaReaderProvider` or a raw client (normalized via `as_reader` at the top), and route reads
  through it. Covered: `sync_api.sync_account_from_api`; `control.build_account_snapshot`,
  `build_enable_ads_plan`, `build_copy_library`, `build_pause_plan`, `fetch_entity_metrics`,
  `fetch_breakdown_metrics`, `account_info`, `estimate_adset_audience`, `search_interests`,
  `list_account_pixels`, `list_account_conversions`, `scan_issues`, `list_account_audiences`;
  `monitor.build_watch_report`; `actions.enrich_action_plan_with_live_state` (+ `fetch_live_ad_state`,
  `fetch_live_adset_state`, `_maybe_add_live_adset_state`); `authoring.build_duplicate_ad_plan`;
  `rotation.fetch_active_adsets`.
- **Mixed read+write functions** — kept the concrete `client` param for the write and **added an
  optional `reader` kwarg** for the live re-read; when `reader` is omitted it defaults to reading
  through the same client (`as_reader(reader) or as_reader(client)`). Covered:
  `control.apply_ops_plan` (its `_build_request`/`_get_entity` re-reads now take a reader),
  `rotation.apply_rotation_plan`, `apply_rename_plan`, `apply_advantage_disable_plan`. The reader is
  the live client (or wrapping it), so drift detection still reads **fresh** state — it is not a
  cache.
- **Write-only functions left untouched** — `actions.apply_action_plan` /
  `_execute_api_operation`, `authoring.apply_authoring_plan`, `control._update_entity`. They still
  use the concrete client.
- **sync_api default construction** keeps the inline `MetaMarketingApiClient(...)` call (then wraps
  it) so the existing `monkeypatch.setattr("...sync_api.MetaMarketingApiClient", FakeClient)` tests
  still intercept.
- **CLI** keyword call sites updated: `enrich_action_plan_with_live_state(..., reader=...)` and
  `fetch_active_adsets(..., reader=...)`. All positional CLI calls were left as-is (the function
  wraps the passed client).

## How to validate

- `.venv/bin/python -m pytest tests/ -q` → **192 passed** (floor; treat as a starting point).
- New tests (all MOCKS ONLY — no live Meta call):
  - `test_direct_meta_reader_delegates_each_read_method_one_to_one` — driven by `_READER_CALL_SPECS`,
    guarded by `test_reader_call_specs_cover_every_read_method` so the full surface is exercised.
  - `test_reader_signatures_match_client_exactly` — pins name/kind/default of every read method
    across `MetaMarketingApiClient`, the ABC, `DirectMetaReader`, `FakeMetaReader`.
  - `test_direct_meta_reader_iter_paginated_preserves_lazy_iterator`,
    `test_fake_meta_reader_iter_paginated_is_reiterable_per_call`.
  - `test_fake_meta_reader_returns_canned_values_and_records_calls`,
    `..._raises_on_unstubbed_method`, `..._rejects_unknown_stub_name`.
  - `test_as_reader_wraps_client_and_passes_reader_through`.
  - `test_supplied_reader_short_circuits_from_env` — a supplied reader never triggers
    `client_from_env` (laziness of `from_env`).
  - `test_build_account_snapshot_accepts_a_fake_reader`.
- Existing `enrich_action_plan_with_live_state` tests now pass `reader=` (3 call sites).

## Known gaps / things to scrutinize

- **Distinct-reader path on the mixed apply functions is untested.** Only the default-derive path
  (`reader is None` → wrap the write client) is exercised for `apply_ops_plan` /
  `apply_rotation_plan` / `apply_rename_plan` / `apply_advantage_disable_plan`. The "pass a separate
  MCP reader + a direct write client" path is wired but has no dedicated test. The MCP ticket
  (`community-mcp-read-server`) will exercise it; consider adding a unit test that passes a
  `FakeMetaReader` for the read and a recording client for the write and asserts the read hit the
  reader while the write hit the client.
- **`experiment.py:read_experiment` was intentionally NOT converted.** It still types its first
  param `client: MetaMarketingApiClient` and passes it positionally to `fetch_entity_metrics`, which
  now normalizes it — so it works through the seam without a signature change, but it is not a
  first-class reader entry point. It was outside the ticket's file list. Decide whether to fold it in
  here or leave for a follow-up.
- **`DirectMetaReader` accepts any client-like object** (the test doubles aren't
  `MetaMarketingApiClient` instances). `as_reader` only special-cases `MetaReaderProvider`; anything
  else is wrapped. This is what keeps existing `FakeClient`s working, but it means a typo'd object
  fails only at first read call, not at wrap time.
- **No static type checker is configured** in the repo (no mypy/pyright/ruff in `pyproject.toml`), so
  the `MetaReaderProvider | MetaMarketingApiClient` unions are only runtime-normalized. The
  signature-pinning test is the guard against drift between the client and the provider.
- **`FakeMetaReader.iter_paginated` checks its stub eagerly** and returns `iter(seeded_list)`. A stub
  seeded with a one-shot generator would be single-pass; seed with a list (documented in the
  docstring).
- The ticket's "MOCKS ONLY" rule is stated in the new test section header and in
  `reader_provider.py`'s module/`FakeMetaReader` docstrings — confirm downstream tickets keep it.

## Out of scope (next tickets)

- The MCP read backend itself (`community-mcp-read-server`).
- Full hybrid-model docs + tool catalog (`hybrid-model-docs-and-tool-catalog`) — `META_API_SETUP.md`
  only gained the one-line pointer to the provider seam.
