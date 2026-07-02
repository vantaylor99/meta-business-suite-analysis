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

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Any

from .meta_api import MetaApiError, MetaMarketingApiClient, client_from_env

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


# Type of the injected MCP call surface: given a tool name and its argument dict, return that
# tool's raw result (a dict, a list, or a JSON string). This is the agent-SDK MCP call surface —
# ``MCPMetaReader`` never constructs or connects a transport itself.
ToolExecutor = Callable[[str, dict[str, Any]], Any]

# Default reader-method -> MCP tool-name map, derived from the *candidate* community package
# ``meta-ads-mcp-server`` (npm, token-based, read-only by default). ``None`` marks a read the
# candidate does NOT expose: calling it raises ``NotImplementedError`` naming the read, so the
# operator can fall back to ``META_READER_BACKEND=direct`` for that one method. The map is
# overridable at construction so a different vetted package can be slotted in without touching
# any call site.
DEFAULT_MCP_TOOL_MAP: dict[str, str | None] = {
    "fetch_insights": "meta_ads_get_adaccount_insights",
    "fetch_ads": "meta_ads_get_ads_by_adaccount",
    "list_campaigns": "meta_ads_get_campaigns_by_adaccount",
    "get_campaign": "meta_ads_get_campaign_by_id",
    "list_adsets": "meta_ads_get_adsets_by_adaccount",
    "get_adset": "meta_ads_get_adset_by_id",
    "get_ad": "meta_ads_get_ad_by_id",
    "get_account": "meta_ads_get_ad_account_details",
    # Not exposed by the candidate read server -> fall back to DirectMetaReader for these reads:
    "list_custom_audiences": None,
    "get_delivery_estimate": None,
    "search_targeting": None,
    "list_pixels": None,
    "list_custom_conversions": None,
    # Raw Graph-path escape hatch: no MCP tool equivalent (see iter_paginated below).
    "iter_paginated": None,
}

# MCP utility tool that follows a Graph ``paging.next`` cursor (the candidate package's
# ``meta_ads_fetch_pagination_url``). Used to drain multi-page list results so a non-auto-paginating
# server never silently truncates. Set to ``None`` to make a paged result raise instead of paging.
DEFAULT_MCP_PAGINATION_TOOL: str | None = "meta_ads_fetch_pagination_url"


class MCPMetaReader(MetaReaderProvider):
    """Routes each read to a token-based community Meta MCP server's equivalent tool.

    The agent-SDK MCP call surface is injected as ``tool_executor(tool_name, arguments) -> raw``;
    this class never constructs or connects a transport. Arguments are translated to the tool's
    input schema (notably ``fields=[...]`` -> a comma-joined string) and the raw tool result is
    translated back into the exact dict/list shapes :class:`DirectMetaReader` returns, so every
    downstream parser is identical regardless of backend.

    **Reads only.** Writes never travel through a reader; they always use the direct
    ``MetaMarketingApiClient`` (the existing token only needs ``ads_read`` for these reads). Reads
    the configured server does not expose raise :class:`NotImplementedError` naming the read, so a
    caller can fall back to ``META_READER_BACKEND=direct`` for that one method.

    **MOCKS ONLY in tests** — the executor is a fake; no live MCP / Meta call is ever made here.
    """

    MAX_PAGES = 1000  # runaway guard while draining paging.next cursors

    def __init__(
        self,
        tool_executor: ToolExecutor,
        *,
        tool_map: dict[str, str | None] | None = None,
        pagination_tool: str | None = DEFAULT_MCP_PAGINATION_TOOL,
    ) -> None:
        self._execute = tool_executor
        self._tool_map = {**DEFAULT_MCP_TOOL_MAP, **(tool_map or {})}
        self._pagination_tool = pagination_tool

    # -- translation helpers -------------------------------------------------

    @staticmethod
    def _join_fields(fields: list[str]) -> str:
        # fields=[...] -> "f1,f2,...". A dropped field silently blanks a downstream metric (the exact
        # failure mode the confidence engine punishes), so this is a 1:1 join with no filtering:
        # round-tripping the result via ``.split(",")`` must return the input unchanged.
        return ",".join(fields)

    def _tool_for(self, method: str) -> str:
        tool = self._tool_map.get(method)
        if tool is None:
            raise NotImplementedError(
                f"MCPMetaReader: read '{method}' is not exposed by the configured MCP server. "
                f"Fall back to META_READER_BACKEND=direct for this read."
            )
        return tool

    @staticmethod
    def _decode(raw: Any) -> Any:
        # Some MCP servers return tool output as a JSON string; decode once so the rest of the
        # translation works on native dict/list shapes.
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MetaApiError(f"MCP tool returned non-JSON text: {exc}") from exc
        return raw

    @classmethod
    def _split_page(
        cls, raw: Any, method: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Return ``(items, envelope)`` where ``envelope`` (if any) may carry ``paging.next``."""
        decoded = cls._decode(raw)
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, dict)], None
        if isinstance(decoded, dict):
            data = decoded.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)], decoded
        raise MetaApiError(
            f"MCPMetaReader.{method}: expected a list or a {{'data': [...]}} envelope from the "
            f"MCP tool, got {type(decoded).__name__}."
        )

    def _call_list(self, method: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        tool = self._tool_for(method)
        items, envelope = self._split_page(self._execute(tool, arguments), method)
        pages = 0
        while envelope is not None:
            next_url = (envelope.get("paging") or {}).get("next")
            if not next_url:
                break
            if self._pagination_tool is None:
                raise MetaApiError(
                    f"MCPMetaReader.{method}: the MCP tool returned a paged result (paging.next "
                    f"present) but no pagination tool is configured; refusing to silently "
                    f"truncate. Configure pagination_tool or use META_READER_BACKEND=direct."
                )
            pages += 1
            if pages > self.MAX_PAGES:
                raise MetaApiError(
                    f"MCPMetaReader.{method}: exceeded {self.MAX_PAGES} pages while draining "
                    f"paging.next; aborting to avoid a runaway loop."
                )
            more, envelope = self._split_page(
                self._execute(self._pagination_tool, {"url": next_url}), method
            )
            items.extend(more)
        return items

    def _call_node(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tool_for(method)
        decoded = self._decode(self._execute(tool, arguments))
        if isinstance(decoded, dict):
            inner = decoded.get("data")
            # Unwrap a single-object {"data": {...}} envelope; otherwise the node is the dict itself.
            return inner if isinstance(inner, dict) else decoded
        raise MetaApiError(
            f"MCPMetaReader.{method}: expected a single object from the MCP tool, got "
            f"{type(decoded).__name__}."
        )

    # -- reads (signatures mirror MetaReaderProvider exactly) ----------------

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
        arguments: dict[str, Any] = {
            "act_id": ad_account_id,
            "fields": self._join_fields(fields),
            "time_range": {"since": date_from, "until": date_to},
            "level": level,
            "time_increment": time_increment,
        }
        if breakdowns:
            arguments["breakdowns"] = list(breakdowns)
        return self._call_list("fetch_insights", arguments)

    def fetch_ads(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._call_list(
            "fetch_ads", {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        )

    def list_campaigns(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        if effective_status:
            arguments["effective_status"] = list(effective_status)
        return self._call_list("list_campaigns", arguments)

    def get_campaign(self, campaign_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._call_node(
            "get_campaign", {"campaign_id": campaign_id, "fields": self._join_fields(fields)}
        )

    def list_adsets(
        self, ad_account_id: str, *, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        if effective_status:
            arguments["effective_status"] = list(effective_status)
        return self._call_list("list_adsets", arguments)

    def get_adset(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._call_node(
            "get_adset", {"adset_id": adset_id, "fields": self._join_fields(fields)}
        )

    def get_ad(self, ad_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._call_node("get_ad", {"ad_id": ad_id, "fields": self._join_fields(fields)})

    def list_custom_audiences(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._call_list(
            "list_custom_audiences", {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        )

    def get_account(self, ad_account_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._call_node(
            "get_account", {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        )

    def get_delivery_estimate(self, adset_id: str, *, fields: list[str]) -> dict[str, Any]:
        return self._call_node(
            "get_delivery_estimate", {"adset_id": adset_id, "fields": self._join_fields(fields)}
        )

    def search_targeting(
        self, *, query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]:
        return self._call_list(
            "search_targeting", {"query": query, "type": search_type, "limit": limit}
        )

    def list_pixels(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._call_list(
            "list_pixels", {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        )

    def list_custom_conversions(self, ad_account_id: str, *, fields: list[str]) -> list[dict[str, Any]]:
        return self._call_list(
            "list_custom_conversions", {"act_id": ad_account_id, "fields": self._join_fields(fields)}
        )

    def iter_paginated(
        self, path_or_url: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        # Decision (ticket edge case): iter_paginated is a raw Graph-path/params escape hatch with no
        # MCP tool equivalent. Rather than silently truncate to one page, it raises; the high-level
        # reads above drain pagination internally, and callers needing the raw path use `direct`.
        raise NotImplementedError(
            "MCPMetaReader does not expose iter_paginated: it is a raw Graph-path escape hatch with "
            "no MCP tool equivalent. Use the high-level reads (which drain pagination internally) "
            "or META_READER_BACKEND=direct."
        )


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


# Env var that selects the read backend at every ``*.from_env()`` construction point.
READER_BACKEND_ENV = "META_READER_BACKEND"


def reader_backend_from_env() -> str:
    """Return the configured read backend string, normalized (lowercased, trimmed).

    Token-free and construction-free: reads only ``META_READER_BACKEND`` (default ``"direct"``)
    and returns the raw normalized value **without validating it**. This is the single source of
    the backend-name normalization rule; :func:`reader_from_env` calls it and is the only place
    that validates/raises on an unknown backend. A health probe (the MCP ``server_info`` tool)
    reports this string verbatim, so it must not raise on an unrecognized value.
    """
    return (os.environ.get(READER_BACKEND_ENV) or "direct").strip().lower()


def reader_from_env(
    api_version: str | None = None,
    *,
    tool_executor: ToolExecutor | None = None,
) -> MetaReaderProvider:
    """Build the reader the environment selects: ``META_READER_BACKEND`` = ``direct`` | ``mcp``.

    This is the **single selection point** for the read backend. With the var unset or ``direct``
    (the default), it is a pure :meth:`DirectMetaReader.from_env` — byte-for-byte today's behavior,
    so production reads do not change unless an operator explicitly opts in. With ``mcp``, an
    :class:`MCPMetaReader` is built around the injected ``tool_executor`` (the agent-SDK MCP call
    surface). The pure-Python CLI cannot synthesize that surface, so selecting ``mcp`` without an
    executor **raises** rather than silently degrading. Reads-only either way — writes always use
    the direct client.
    """
    backend = reader_backend_from_env()
    if backend in ("", "direct"):
        return DirectMetaReader.from_env(api_version)
    if backend == "mcp":
        if tool_executor is None:
            raise RuntimeError(
                "META_READER_BACKEND=mcp selected but no MCP tool-executor was provided. The MCP "
                "read server is consumed by the agent runtime, which injects its tool-call surface "
                "via MCPMetaReader(tool_executor=...). The pure-Python CLI cannot construct it; set "
                "META_READER_BACKEND=direct (the default) for CLI/sync runs."
            )
        return MCPMetaReader(tool_executor)
    raise ValueError(
        f"Unknown {READER_BACKEND_ENV}={backend!r}; expected 'direct' (default) or 'mcp'."
    )
