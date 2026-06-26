description: Put a thin, swappable "reader" layer in front of every place the app reads from Meta, so the same code can read through today's direct API client or, later, a different read backend, without touching dozens of call sites — and so tests never need to talk to Meta.
prereq:
files: src/meta_ads_analysis/reader_provider.py (new), src/meta_ads_analysis/meta_api.py, src/meta_ads_analysis/sync_api.py, src/meta_ads_analysis/control.py, src/meta_ads_analysis/monitor.py, src/meta_ads_analysis/actions.py, src/meta_ads_analysis/authoring.py, src/meta_ads_analysis/rotation.py, tests/test_meta_ads_analysis.py, docs/META_API_SETUP.md
difficulty: medium
----
## Why

Today ~37 read call sites across `sync_api.py`, `control.py`, `monitor.py`, `actions.py`,
`authoring.py`, and `rotation.py` call `MetaMarketingApiClient` read methods directly
(`fetch_insights`, `fetch_ads`, `list_campaigns`, `get_campaign`, `list_adsets`, `get_adset`,
`get_ad`, `list_custom_audiences`, `get_account`, `get_delivery_estimate`, `search_targeting`,
`list_pixels`, `list_custom_conversions`, plus the low-level `iter_paginated`). The hybrid plan is
to make the **read side provider-agnostic** so a community token-based Meta MCP read server (next
ticket) — or the official hosted OAuth server later — can supply reads without rewriting call sites.
This ticket builds the abstraction and routes existing call sites through it. It does NOT add the MCP
backend (that is `community-mcp-read-server`).

## What to build — `src/meta_ads_analysis/reader_provider.py`

A small module defining a provider interface that mirrors the read surface of
`MetaMarketingApiClient`, plus the direct (current behavior) and fake (test) implementations.

```python
class MetaReaderProvider(ABC):
    # one abstractmethod per read method actually used in src/, keyword-only args matching
    # MetaMarketingApiClient signatures exactly (see meta_api.py line refs in the research):
    #   fetch_insights(ad_account_id, *, fields, date_from, date_to, level='ad',
    #                  time_increment=1, breakdowns=None) -> list[dict]
    #   fetch_ads(ad_account_id, *, fields) -> list[dict]
    #   list_campaigns(ad_account_id, *, fields, effective_status=None) -> list[dict]
    #   get_campaign(campaign_id, *, fields) -> dict
    #   list_adsets(ad_account_id, *, fields, effective_status=None) -> list[dict]
    #   get_adset(adset_id, *, fields) -> dict
    #   get_ad(ad_id, *, fields) -> dict
    #   list_custom_audiences(ad_account_id, *, fields) -> list[dict]
    #   get_account(ad_account_id, *, fields) -> dict
    #   get_delivery_estimate(adset_id, *, fields) -> dict
    #   search_targeting(*, query, search_type='adinterest', limit=25) -> list[dict]
    #   list_pixels(ad_account_id, *, fields) -> list[dict]
    #   list_custom_conversions(ad_account_id, *, fields) -> list[dict]
    #   iter_paginated(path_or_url, *, params=None) -> Iterator[dict]
    ...

class DirectMetaReader(MetaReaderProvider):
    """Delegates 1:1 to a wrapped MetaMarketingApiClient (current behavior, byte-for-byte)."""
    def __init__(self, client: MetaMarketingApiClient): ...
    @classmethod
    def from_env(cls) -> "DirectMetaReader":  # wraps client_from_env()
        ...

class FakeMetaReader(MetaReaderProvider):
    """Test double seeded with canned return values; raises on any unstubbed method."""
```

**Critical constraint — MOCKS ONLY:** every test for this ticket and every ticket downstream uses
`FakeMetaReader` (or the existing `FakeClient` wrapped in `DirectMetaReader`). NO test may make a
live Meta call. State this in the test docstrings.

### Enumerate the surface from real usage, not guesswork

Before writing the ABC, grep `src/meta_ads_analysis/` for `client.<readmethod>(` to confirm the
exact set and signatures in use (the list above is from the research bundle; verify it). Only abstract
methods that are actually called — do not mirror write methods (`create_*`, `update_*`, `upload_*`)
or `get_video`; writes stay on the concrete client (the write tickets handle those separately).
`iter_paginated` IS used directly (e.g. `control.build_enable_ads_plan`) so it must be on the ABC.

### Backward-compat: do not break the existing `client=` parameter

`sync_account_from_api(..., client=None)` and the apply/build functions accept a
`MetaMarketingApiClient | None` today. Choose ONE migration shape and apply it consistently:

- Rename the read-consuming parameters from `client:` to `reader: MetaReaderProvider` at the entry
  points (`sync_api.sync_account_from_api`, `control.build_account_snapshot`,
  `control.build_enable_ads_plan`, `monitor.build_watch_report`, the read helpers in
  `actions.py`/`authoring.py`/`rotation.py`), and at each entry point accept EITHER a reader or a raw
  `MetaMarketingApiClient` and wrap the latter in `DirectMetaReader` (so existing callers/tests that
  pass a `FakeClient` keep working). The default-construction path (`client or client_from_env()`)
  becomes `reader or DirectMetaReader.from_env()`.
- **Do not touch write call sites.** Functions that both read and write (e.g. `rotation` re-reads
  live targeting before a write, `control._build_request` re-reads via `_get_entity`) should take the
  reader for the read and keep the concrete client for the write. Where a single function needs both,
  pass both. Do NOT expose `reader.client` as a back-door for writes (it would defeat the
  abstraction) — keep the existing concrete client param for writes and add a reader param for reads.
  Document the split in each touched function's docstring.

Keep each call-site change a pure 1:1 rename (`client.fetch_insights(...)` →
`reader.fetch_insights(...)`); no logic changes.

## TODO

- Grep `src/meta_ads_analysis/` to confirm the exact read-method set + signatures; reconcile against
  the list above.
- Write `reader_provider.py`: `MetaReaderProvider` ABC, `DirectMetaReader` (1:1 delegation +
  `from_env`), `FakeMetaReader` (seeded canned values, raises on unstubbed).
- Route `sync_api.sync_account_from_api` reads through the reader; accept reader-or-client at the
  entry point.
- Route the read call sites in `control.py`, `monitor.py`, `actions.py`, `authoring.py`,
  `rotation.py` through the reader, keeping write paths on the concrete client.
- Update `tests/test_meta_ads_analysis.py`: adapt the existing `FakeClient` monkeypatch tests to the
  reader shape (wrap `FakeClient` in `DirectMetaReader`, or pass a `FakeMetaReader`). Add a unit test
  that `DirectMetaReader` delegates each method 1:1 and that `FakeMetaReader` raises on an unstubbed
  method. All tests mock-only.
- Run `.venv/bin/python -m pytest tests/ -q` green before handoff.
- Note in `docs/META_API_SETUP.md` (one line) that reads now flow through a provider seam; full
  hybrid-model docs land in `hybrid-model-docs-and-tool-catalog`.

## Edge cases & interactions

- **`iter_paginated` returns an iterator, not a list** — the ABC method must preserve laziness;
  `DirectMetaReader.iter_paginated` returns the underlying iterator unchanged. A `FakeMetaReader`
  must yield from a seeded list so callers that iterate twice / call `list()` behave identically.
- **Keyword-only signatures must match exactly** — `fetch_insights` has positional `ad_account_id`
  then keyword-only fields; a signature drift will surface as a `TypeError` only at a call site that
  passes an arg the wrapper dropped. Pin signatures with a test.
- **Mixed read+write functions** (`rotation.apply_rotation_plan`, `control._build_request`'s
  `_get_entity` re-read, `actions.enrich_action_plan_with_live_state`) — the live re-read before a
  write must go through the reader while the write stays on the client; verify drift-detection still
  reads fresh state (the reader is not a cache).
- **`from_env` constructs a real client** — must be lazy (only when no reader supplied) so tests that
  inject a fake never trigger env/token lookup. Confirm no module-level `client_from_env()`.
- **Existing monkeypatch tests** patch `sync_api.MetaMarketingApiClient`; after the change the
  construction path is `DirectMetaReader.from_env()` which calls `client_from_env()` — ensure the
  monkeypatch target still intercepts (patch `reader_provider`'s client constructor or keep
  `sync_api`'s import path patchable). Run the existing pipeline test to confirm.
- **Type checking** — `reader: MetaReaderProvider | MetaMarketingApiClient` union at entry points
  must be normalized to a `MetaReaderProvider` immediately; don't let the union leak into the body.