"""Provider seam for Meta **reads**.

Every read the app performs against Meta flows through a :class:`MetaReaderProvider`
so the read backend is swappable. Today that backend is :class:`DirectMetaReader`, a
1:1 pass-through to a live :class:`~meta_ads_analysis.meta_api.MetaMarketingApiClient`.
Later a token-based MCP read server (the ``community-mcp-read-server`` ticket) — or the
official hosted OAuth server after that — can supply the same reads without rewriting any
call site, because the call sites depend only on this interface.

**Writes are deliberately NOT part of this seam.** ``create_*`` / ``update_*`` /
``upload_*`` / ``get_video`` stay on the concrete ``MetaMarketingApiClient``; functions
that both read and write take a reader for the read and keep the concrete client for the
write (they never reach into ``reader`` for a write).

:class:`FakeMetaReader` is the test double: it returns canned values and raises on any
method that was not stubbed. **MOCKS ONLY** — no test in this repo (this ticket or any
downstream of it) may make a live Meta call; every test seeds a :class:`FakeMetaReader`
(or wraps the existing ``FakeClient`` in :class:`DirectMetaReader`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from .meta_api import MetaMarketingApiClient, client_from_env

# The exact read surface of MetaMarketingApiClient that ``src/`` actually calls. Kept in
# one place so FakeMetaReader can validate its stubs and the delegation test can iterate it.
READ_METHODS: tuple[str, ...] = (
    "fetch_insights",
    "fetch_ads",
    "list_campaigns",
    "get_campaign",
    "list_adsets",
    "get_adset",
    "get_ad",
    "list_custom_audiences",
    "get_account",
    "get_delivery_estimate",
    "search_targeting",
    "list_pixels",
    "list_custom_conversions",
    "iter_paginated",
)


class MetaReaderProvider(ABC):
    """The read surface of ``MetaMarketingApiClient`` as a swappable provider.

    Signatures mirror ``MetaMarketingApiClient`` **exactly** (same keyword-only split, same
    defaults) so a call site can switch from a client to a reader as a pure rename. Only
    methods actually called from ``src/`` are abstracted; write methods stay on the client.
    """

    @abstractmethod
    def fetch_insights(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def fetch_ads(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_campaigns(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_campaign(self, campaign_id: str, *, fields: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def list_adsets(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_adset(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def list_custom_audiences(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def get_account(self, ad_account_id: str, *, fields: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def get_delivery_estimate(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    def search_targeting(
        self, *, query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_pixels(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_custom_conversions(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]: ...

    @abstractmethod
    def iter_paginated(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]: ...


class DirectMetaReader(MetaReaderProvider):
    """Reads through a wrapped ``MetaMarketingApiClient`` (current behavior, byte-for-byte).

    Each method delegates 1:1 to the client; no logic, caching, or transformation is added.
    The wrapped object only needs the read methods, so the existing ``FakeClient`` test
    doubles can be wrapped here directly.
    """

    def __init__(self, client: MetaMarketingApiClient) -> None:
        # Intentionally private: there is no public ``.client`` accessor. Writes never travel
        # through a reader — a function that writes keeps its own explicit client parameter.
        self._client = client

    @classmethod
    def from_env(cls, api_version: str | None = None) -> "DirectMetaReader":
        """Build a reader over a client constructed from env vars. Lazy: the client (and its
        token/env lookup) is only created when this is actually called."""
        return cls(client_from_env(api_version))

    def fetch_insights(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._client.fetch_insights(
            ad_account_id,
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            level=level,
            time_increment=time_increment,
            breakdowns=breakdowns,
        )

    def fetch_ads(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._client.fetch_ads(ad_account_id, fields=fields)

    def list_campaigns(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._client.list_campaigns(ad_account_id, fields=fields, effective_status=effective_status)

    def get_campaign(self, campaign_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._client.get_campaign(campaign_id, fields=fields)

    def list_adsets(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._client.list_adsets(ad_account_id, fields=fields, effective_status=effective_status)

    def get_adset(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._client.get_adset(adset_id, fields=fields)

    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._client.get_ad(ad_id, fields=fields)

    def list_custom_audiences(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._client.list_custom_audiences(ad_account_id, fields=fields)

    def get_account(self, ad_account_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._client.get_account(ad_account_id, fields=fields)

    def get_delivery_estimate(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._client.get_delivery_estimate(adset_id, fields=fields)

    def search_targeting(
        self, *, query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]:
        return self._client.search_targeting(query=query, search_type=search_type, limit=limit)

    def list_pixels(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._client.list_pixels(ad_account_id, fields=fields)

    def list_custom_conversions(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._client.list_custom_conversions(ad_account_id, fields=fields)

    def iter_paginated(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        # Return the underlying iterator unchanged so laziness is preserved exactly.
        return self._client.iter_paginated(path_or_url, params=params)


class FakeMetaReader(MetaReaderProvider):
    """Test double seeded with canned return values; raises on any unstubbed method.

    **MOCKS ONLY** — never makes a live Meta call. Seed each read method by name with either
    a fixed value or a callable ``(*args, **kwargs) -> value``::

        reader = FakeMetaReader(
            list_campaigns=[{"id": "c1"}],
            get_ad=lambda ad_id, *, fields: ads_by_id[ad_id],
            iter_paginated=[{"id": "ad1"}, {"id": "ad2"}],
        )

    Calls are recorded in ``calls`` (a list of ``(method, args, kwargs)``) for assertions.
    Any method not seeded raises ``NotImplementedError`` — a test that hits an unstubbed read
    is surfacing missing coverage, not silently returning empty data.
    """

    def __init__(self, **stubs: Any) -> None:
        unknown = set(stubs) - set(READ_METHODS)
        if unknown:
            raise ValueError(
                f"FakeMetaReader got unknown read method(s): {sorted(unknown)}. "
                f"Valid: {list(READ_METHODS)}"
            )
        self._stubs = stubs
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _result(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if name not in self._stubs:
            raise NotImplementedError(
                f"FakeMetaReader.{name} was called but not stubbed. "
                f"Seed it: FakeMetaReader({name}=<value or callable>)."
            )
        value = self._stubs[name]
        return value(*args, **kwargs) if callable(value) else value

    def fetch_insights(
        self,
        ad_account_id: str,
        *,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._result(
            "fetch_insights",
            ad_account_id,
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            level=level,
            time_increment=time_increment,
            breakdowns=breakdowns,
        )

    def fetch_ads(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._result("fetch_ads", ad_account_id, fields=fields)

    def list_campaigns(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._result("list_campaigns", ad_account_id, fields=fields, effective_status=effective_status)

    def get_campaign(self, campaign_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._result("get_campaign", campaign_id, fields=fields)

    def list_adsets(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._result("list_adsets", ad_account_id, fields=fields, effective_status=effective_status)

    def get_adset(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._result("get_adset", adset_id, fields=fields)

    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._result("get_ad", ad_id, fields=fields)

    def list_custom_audiences(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._result("list_custom_audiences", ad_account_id, fields=fields)

    def get_account(self, ad_account_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._result("get_account", ad_account_id, fields=fields)

    def get_delivery_estimate(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._result("get_delivery_estimate", adset_id, fields=fields)

    def search_targeting(
        self, *, query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]:
        return self._result("search_targeting", query=query, search_type=search_type, limit=limit)

    def list_pixels(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._result("list_pixels", ad_account_id, fields=fields)

    def list_custom_conversions(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._result("list_custom_conversions", ad_account_id, fields=fields)

    def iter_paginated(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        # Re-iterable per call: each call returns a fresh iterator over the seeded list, so a
        # caller that does list(...) or iterates more than once behaves like the real client.
        result = self._result("iter_paginated", path_or_url, params=params)
        return iter(result)


def as_reader(reader_or_client: Any) -> MetaReaderProvider | None:
    """Normalize a reader-or-client into a :class:`MetaReaderProvider`.

    Entry points accept **either** a ``MetaReaderProvider`` or a raw ``MetaMarketingApiClient``
    (or a client-like test double) so existing callers keep working; a non-reader is wrapped in
    :class:`DirectMetaReader`. ``None`` passes through unchanged so callers can supply their own
    lazy default (e.g. ``as_reader(reader) or DirectMetaReader.from_env()``).
    """
    if reader_or_client is None:
        return None
    if isinstance(reader_or_client, MetaReaderProvider):
        return reader_or_client
    return DirectMetaReader(reader_or_client)
