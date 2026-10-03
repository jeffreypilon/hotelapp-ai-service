"""stdio entry point -- the public tool surface only.

Launched as a local subprocess by an MCP client (Claude Desktop), with no authenticated user and
therefore no credential of any kind -- the trust boundary is the local machine, per
ai-enablement-overview.md §6's Design Decision and security-implementation.md's "stdio: safe by
construction, not by configuration". This is what makes the `hotelapp-ai mcp-stdio` command real,
already named in environment-setup-guide.md §5.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from hotelapp_ai.config.settings import get_settings
from hotelapp_ai.gateways.hotelapp import HotelAppGateway
from hotelapp_ai.transport.mcp.tools import ToolDependencies, register_public_tools


async def _serve() -> None:
    settings = get_settings()
    async with HotelAppGateway.open(settings.hotelapp_api_base_url) as gateway:
        deps = ToolDependencies(
            gateway=gateway, frontend_base_url=settings.hotelapp_frontend_base_url
        )

        mcp: FastMCP = FastMCP("hotelapp")
        register_public_tools(mcp, deps)

        await mcp.run_stdio_async()


def run() -> None:
    """`uv run hotelapp-ai mcp-stdio` -- blocks, serving JSON-RPC over stdio until the client
    disconnects or stdin closes.
    """
    asyncio.run(_serve())
