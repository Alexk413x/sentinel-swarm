from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from swarm_ledger import __version__, sessions
from swarm_ledger import server as server_module
from swarm_ledger.server import _TOOL_NAMES, configure, mcp


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    configure(root)
    return root


async def _list_tool_names() -> set[str]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        return {tool.name for tool in tools}


async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
        return result.data


def test_ledger_info_is_registered():
    assert asyncio.run(_list_tool_names()) == set(_TOOL_NAMES)


def test_ledger_info_reports_ready_status_and_tool_count(host: Path):
    data = asyncio.run(_call_tool("ledger_info", {}))
    assert data == {
        "name": "swarm-ledger",
        "version": __version__,
        "status": "ready",
        "tools": len(_TOOL_NAMES),
        "repo_root": str(host),
    }


async def _run_start_then_status() -> tuple[dict, dict]:
    async with Client(mcp) as client:
        started = (
            await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        ).data
        status = (
            await client.call_tool("run_status", {"caller": "oracle", "agent_id": "sess-1"})
        ).data
        return started, status


def test_run_start_then_run_status_round_trip(host: Path):
    started, status = asyncio.run(_run_start_then_status())
    assert started["run"]["prd"] == "Build X"
    assert started["oracle"]["agent_id"] == "sess-1"
    assert status["run"]["run_id"] == started["run"]["run_id"]


async def _brief_create_with_wrong_model() -> str:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        try:
            await client.call_tool(
                "brief_create",
                {
                    "caller": "oracle",
                    "agent_id": "sess-1",
                    "child_name": "manager-1",
                    "child_role": "manager",
                    "model": "not-a-model",
                    "body": "Own it.",
                },
            )
        except ToolError as exc:
            return str(exc)
        raise AssertionError("expected brief_create to raise a ToolError")


def test_brief_create_refuses_an_unapproved_model(host: Path):
    message = asyncio.run(_brief_create_with_wrong_model())
    assert "not-a-model" in message
    assert "is not approved for" in message


async def _brief_get_and_ack_flow() -> dict[str, Any]:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        await client.call_tool(
            "brief_create",
            {
                "caller": "oracle",
                "agent_id": "sess-1",
                "child_name": "manager-1",
                "child_role": "manager",
                "model": "opus",
                "body": "Own phase-1.",
            },
        )
        brief = (
            await client.call_tool(
                "brief_get", {"caller_name": "manager-1", "child_name": "manager-1"}
            )
        ).data
        acked = (
            await client.call_tool("brief_ack", {"caller": "manager-1", "agent_id": "mgr-1"})
        ).data
        return {"brief": brief, "acked": acked}


def test_brief_get_and_brief_ack_flow(host: Path):
    result = asyncio.run(_brief_get_and_ack_flow())
    assert result["brief"]["child_name"] == "manager-1"
    assert result["brief"]["model"] == "opus"
    assert result["acked"]["agent_id"] == "mgr-1"
    assert result["acked"]["role"] == "manager"


async def _analytics_query() -> dict[str, Any]:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        return (
            await client.call_tool(
                "analytics_query",
                {"caller": "oracle", "agent_id": "sess-1", "sql": "SELECT prd FROM runs"},
            )
        ).data


def test_analytics_query_with_a_select_returns_rows(host: Path):
    data = asyncio.run(_analytics_query())
    assert data["rows"] == [{"prd": "Build X"}]


def test_the_session_tools_are_registered() -> None:
    assert {"agent_spawn", "agent_resume"} <= asyncio.run(_list_tool_names())


async def _spawn_without_a_brief() -> str:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        try:
            await client.call_tool(
                "agent_spawn", {"caller": "oracle", "agent_id": "sess-1", "child_name": "mgr-9"}
            )
        except ToolError as exc:
            return str(exc)
        raise AssertionError("expected agent_spawn to raise a ToolError")


def test_agent_spawn_reports_a_missing_brief_as_a_tool_error(host: Path) -> None:
    assert "no unacknowledged brief for 'mgr-9'" in asyncio.run(_spawn_without_a_brief())


async def _start_and_finish() -> dict[str, Any]:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        return (
            await client.call_tool(
                "run_finish", {"caller": "oracle", "agent_id": "sess-1", "outcome": "success"}
            )
        ).data


def test_run_finish_calls_the_on_run_finish_hook(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished: list[str | None] = []
    monkeypatch.setattr(server_module, "on_run_finish", finished.append)
    assert asyncio.run(_start_and_finish())["state"] == "finished"
    assert len(finished) == 1
    assert finished[0] is not None
