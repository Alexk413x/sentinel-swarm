from __future__ import annotations

import asyncio

from fastmcp import Client

from swarm_ledger import __version__
from swarm_ledger.server import mcp


async def _list_tool_names() -> set[str]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        return {tool.name for tool in tools}


async def _call_ledger_info() -> dict[str, str]:
    async with Client(mcp) as client:
        result = await client.call_tool("ledger_info", {})
        return result.data


def test_ledger_info_is_registered():
    assert asyncio.run(_list_tool_names()) == {"ledger_info"}


def test_ledger_info_returns_the_package_version_and_skeleton_status():
    data = asyncio.run(_call_ledger_info())
    assert data == {"name": "swarm-ledger", "version": __version__, "status": "skeleton"}
