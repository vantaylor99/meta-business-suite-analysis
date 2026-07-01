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
import os

# Mirror the requests-missing pattern in meta_api.py: import guarded at module load,
# actionable SystemExit raised at use site — never a bare ImportError traceback.
try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - exercised only without the `server` extra
    FastMCP = None

from . import __version__
from .meta_api import meta_api_version_from_env
from .reader_provider import reader_backend_from_env

SERVER_NAME = "meta-ads-mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_server_info() -> dict:
    """Pure, token-free health/info payload. Unit-testable without binding a socket.

    Reports the server identity, the configured Meta API version, the selected read backend,
    and whether live Meta calls are enabled (always ``False`` in the scaffold). Uses the
    token-free ``*_from_env`` helpers, so it never touches ``META_ACCESS_TOKEN`` and never
    raises on a missing token or an unrecognized backend — it is a health probe, not a
    constructor.
    """
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "meta_api_version": meta_api_version_from_env(),   # no token required
        "read_backend": reader_backend_from_env(),         # "direct" | "mcp" (verbatim-normalized)
        "live_calls_enabled": False,   # scaffold makes ZERO live Meta calls; later tickets own this flag
    }


def build_server(host: str, port: int):
    """Construct the FastMCP server exposing the single ``server_info`` tool.

    Raises an actionable ``SystemExit`` if the ``mcp`` SDK (the ``server`` extra) is not
    installed, rather than surfacing a bare ``ImportError``.
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
        live Meta calls are enabled (always false in the scaffold)."""
        return build_server_info()

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
