"""Custom Meta MCP server — thin entrypoint (scaffold; no live Meta tools yet).

This is the foundation of our own Meta MCP server: a process that starts, reports health,
and can be connected to from an MCP client over HTTP. It exposes exactly **one** tool,
``server_info``, and makes **zero live Meta calls** — it has no Meta read/write tools yet.
Those land in the ``mcp-read-tools`` / ``mcp-guarded-write-tools`` follow-on tickets.

The module is a **thin entrypoint over the existing library**: it embeds no Meta/business
logic and only imports and exposes package functions, so the CLI and the server stay two
frontends over one library. The ``mcp`` SDK import is guarded at module load (mirroring the
``requests``-missing pattern in :mod:`meta_ads_analysis.meta_api`) so a missing ``server``
extra produces an actionable ``SystemExit`` at use site, never a bare ``ImportError``.
"""

from __future__ import annotations

import argparse
import functools
import os
from collections.abc import Callable
from typing import Any

# Mirror the requests-missing pattern in meta_api.py: import guarded at module load,
# actionable SystemExit raised at use site — never a bare ImportError traceback.
try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError:  # pragma: no cover - exercised only without the `server` extra
    FastMCP = None
    ToolError = None

from . import __version__
from .meta_api import MetaApiError, meta_api_version_from_env
from .reader_provider import (
    READ_METHODS,
    DirectMetaReader,
    MetaReaderProvider,
    reader_backend_from_env,
)

SERVER_NAME = "meta-ads-mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# The read surface we expose as MCP tools: every read in READ_METHODS **except** the raw
# ``iter_paginated`` escape hatch (a Graph-path/params primitive with no natural tool shape —
# the high-level reads drain pagination internally). This mirrors what ``MCPMetaReader`` omits.
READ_TOOL_METHODS: tuple[str, ...] = tuple(m for m in READ_METHODS if m != "iter_paginated")

# reader-method -> MCP tool name. Identity, because each tool is named **exactly** for its reader
# method (see the plan's decision 1: natural, self-describing names, not the community package's
# dialect). Shipped as a module constant so a future consumer wiring an ``MCPMetaReader`` at our
# server has the full name map for all 13 reads in one import.
SERVER_TOOL_MAP: dict[str, str] = {m: m for m in READ_TOOL_METHODS}

# Short human descriptions surfaced to the MCP client (and the calling LLM) per tool.
READ_TOOL_DESCRIPTIONS: dict[str, str] = {
    "fetch_insights": (
        "Fetch time-series ad insights (spend, results, ROAS, etc.) for an ad account over an "
        "ISO date range, at the given level and time increment."
    ),
    "fetch_ads": "List all ads in an ad account with the requested fields.",
    "list_campaigns": "List campaigns in an ad account, optionally filtered by effective status.",
    "get_campaign": "Fetch a single campaign's current state by id.",
    "list_adsets": "List ad sets in an ad account, optionally filtered by effective status.",
    "get_adset": "Fetch a single ad set's current state by id.",
    "get_ad": "Fetch a single ad's current state by id.",
    "list_custom_audiences": "List the custom audiences available in an ad account.",
    "get_account": (
        "Fetch account-level info (status, currency, spend cap, amount spent, funding)."
    ),
    "get_delivery_estimate": "Estimate audience reach/size for an ad set's current targeting.",
    "search_targeting": (
        "Search Meta's targeting catalog (interests, behaviors, demographics) by free-text query."
    ),
    "list_pixels": "List the ad pixels configured on an ad account.",
    "list_custom_conversions": "List the custom conversions configured on an ad account.",
}


def build_server_info() -> dict:
    """Pure, token-free health/info payload. Unit-testable without binding a socket.

    Reports the server identity, the configured Meta API version, the selected read backend,
    and whether live Meta calls are enabled. Uses the token-free ``*_from_env`` helpers, so it
    never touches ``META_ACCESS_TOKEN`` and never raises on a missing token or an unrecognized
    backend — it is a health probe, not a constructor.
    """
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "meta_api_version": meta_api_version_from_env(),   # no token required
        "read_backend": reader_backend_from_env(),         # "direct" | "mcp" (verbatim-normalized)
        # Capability flag: this server now exposes live Meta **read** tools (see build_read_tools).
        # Independent of whether a token is present — build_server_info stays token-free.
        "live_calls_enabled": True,
    }


def build_read_tools(reader: MetaReaderProvider) -> dict[str, Callable[..., Any]]:
    """Return ``{tool_name: callable}`` for every Meta **read**, bound to ``reader``.

    PURE: no FastMCP import, no socket, no token lookup — unit-testable with a ``FakeMetaReader``.
    Each callable is a thin, zero-translation wrapper over the same-named ``MetaReaderProvider``
    method: it mirrors that method's arguments and returns the identical dict/list the reader
    returns (an empty list stays ``[]``; an API failure lets ``MetaApiError`` propagate unchanged).

    The wrappers take **positional-or-keyword** params (no keyword-only split) with accurate
    annotations, because MCP arguments arrive as a flat object and FastMCP derives each tool's JSON
    schema from the wrapper's own signature. ``fields`` stays a ``list[str]`` (no comma-join) since
    the call goes straight into the reader; ``date_from``/``date_to`` stay separate ISO strings and
    ``search_type`` stays ``search_type`` — the natural surface, not the community dialect.
    """

    def fetch_insights(
        ad_account_id: str,
        fields: list[str],
        date_from: str,
        date_to: str,
        level: str = "ad",
        time_increment: int | str = 1,
        breakdowns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return reader.fetch_insights(
            ad_account_id,
            fields=fields,
            date_from=date_from,
            date_to=date_to,
            level=level,
            time_increment=time_increment,
            breakdowns=breakdowns,
        )

    def fetch_ads(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.fetch_ads(ad_account_id, fields=fields)

    def list_campaigns(
        ad_account_id: str, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return reader.list_campaigns(ad_account_id, fields=fields, effective_status=effective_status)

    def get_campaign(campaign_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_campaign(campaign_id, fields=fields)

    def list_adsets(
        ad_account_id: str, fields: list[str], effective_status: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return reader.list_adsets(ad_account_id, fields=fields, effective_status=effective_status)

    def get_adset(adset_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_adset(adset_id, fields=fields)

    def get_ad(ad_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_ad(ad_id, fields=fields)

    def list_custom_audiences(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_custom_audiences(ad_account_id, fields=fields)

    def get_account(ad_account_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_account(ad_account_id, fields=fields)

    def get_delivery_estimate(adset_id: str, fields: list[str]) -> dict[str, Any]:
        return reader.get_delivery_estimate(adset_id, fields=fields)

    def search_targeting(
        query: str, search_type: str = "adinterest", limit: int = 25
    ) -> list[dict[str, Any]]:
        return reader.search_targeting(query=query, search_type=search_type, limit=limit)

    def list_pixels(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_pixels(ad_account_id, fields=fields)

    def list_custom_conversions(ad_account_id: str, fields: list[str]) -> list[dict[str, Any]]:
        return reader.list_custom_conversions(ad_account_id, fields=fields)

    tools: dict[str, Callable[..., Any]] = {
        "fetch_insights": fetch_insights,
        "fetch_ads": fetch_ads,
        "list_campaigns": list_campaigns,
        "get_campaign": get_campaign,
        "list_adsets": list_adsets,
        "get_adset": get_adset,
        "get_ad": get_ad,
        "list_custom_audiences": list_custom_audiences,
        "get_account": get_account,
        "get_delivery_estimate": get_delivery_estimate,
        "search_targeting": search_targeting,
        "list_pixels": list_pixels,
        "list_custom_conversions": list_custom_conversions,
    }
    # Guard against a read added to READ_TOOL_METHODS but not wired here (or vice versa). The
    # parity test asserts the same, but failing loudly at construction beats a silently-missing tool.
    assert set(tools) == set(READ_TOOL_METHODS), (
        f"build_read_tools drifted from READ_TOOL_METHODS: "
        f"missing={set(READ_TOOL_METHODS) - set(tools)}, extra={set(tools) - set(READ_TOOL_METHODS)}"
    )
    return tools


def _wrap_tool_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a read-tool callable so a ``MetaApiError`` becomes a clean FastMCP ``ToolError``.

    A bad token, an insufficient scope, or a transient Graph failure surfaces to the MCP client as
    a tool error (the server keeps serving) instead of an uncaught traceback. ``functools.wraps``
    preserves ``func``'s signature so FastMCP still derives the correct JSON schema from it.
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except MetaApiError as exc:
            raise ToolError(str(exc)) from exc

    return wrapped


def build_server(host: str, port: int):
    """Construct the FastMCP server exposing ``server_info`` plus the live Meta read tools.

    Raises an actionable ``SystemExit`` if the ``mcp`` SDK (the ``server`` extra) is not
    installed, rather than surfacing a bare ``ImportError``.

    The reader is built **once** as a ``DirectMetaReader.from_env()`` and shared across all tool
    calls. We deliberately do NOT call ``reader_from_env``: with ``META_READER_BACKEND=mcp`` that
    would try to build an ``MCPMetaReader`` requiring an injected tool-executor — i.e. our server
    would recursively try to be its own MCP client. Our server *is* the direct reader an ``mcp``
    backend elsewhere points at; it must never recursively select an ``mcp`` backend. (``server_info``
    still reports ``reader_backend_from_env()`` verbatim as a health string — independent of this.)
    """
    if FastMCP is None:
        raise SystemExit(
            "The 'mcp' package is required to run the Meta MCP server. "
            "Install the server extra with `pip install -e .[server]`."
        )
    mcp = FastMCP(SERVER_NAME, host=host, port=port)

    @mcp.tool()
    def server_info() -> dict:
        """Report server identity, configured Meta API version, selected read backend, and whether
        live Meta calls are enabled."""
        return build_server_info()

    # One shared direct reader (NOT reader_from_env — see docstring). Live reads flow through it.
    reader = DirectMetaReader.from_env()
    for name, func in build_read_tools(reader).items():
        mcp.add_tool(
            _wrap_tool_errors(func),
            name=name,
            description=READ_TOOL_DESCRIPTIONS.get(name) or f"Meta read: {name}",
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the custom Meta MCP server (HTTP).")
    parser.add_argument("--host", default=os.environ.get("MCP_SERVER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_SERVER_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    mcp = build_server(args.host, args.port)
    try:
        mcp.run(transport="streamable-http")
    except OSError as exc:  # port in use / bad host bind
        raise SystemExit(f"Could not start MCP server on {args.host}:{args.port}: {exc}") from exc


if __name__ == "__main__":  # pragma: no cover - module-run convenience
    main()
