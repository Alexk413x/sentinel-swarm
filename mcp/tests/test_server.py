from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from swarm_ledger import __version__, front, rubric, sessions
from swarm_ledger.server import _TOOL_NAMES, configure, mcp


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def host(tmp_path: Path, repo_root: Path, check_in) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    configure(root)
    check_in(root, "sess-1", "stamped-1", "mgr-1")
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


async def _list_tools() -> list[Any]:
    async with Client(mcp) as client:
        return list(await client.list_tools())


def test_no_tool_schema_lists_agent_id_and_every_root_is_an_object():
    tools = asyncio.run(_list_tools())
    assert {tool.name for tool in tools} == set(_TOOL_NAMES)
    for tool in tools:
        assert tool.input_schema.get("type") == "object", tool.name
        assert "agent_id" not in tool.input_schema.get("properties", {}), tool.name


def test_the_read_only_tools_carry_the_read_only_hint():
    read_only = {
        tool.name
        for tool in asyncio.run(_list_tools())
        if tool.annotations is not None and tool.annotations.read_only_hint
    }
    assert read_only == {
        "ledger_info",
        "brief_get",
        "who_owns",
        "issue_list",
        "cr_list",
        "run_status",
        "status_tree",
        "guidelines_get",
        "plan_unlocked",
        "events",
        "analytics_query",
        "test_run_get",
    }


def test_caller_targeted_and_finding_ids_are_described():
    by_name = {tool.name: tool.input_schema["properties"] for tool in asyncio.run(_list_tools())}
    assert by_name["run_status"]["caller"]["description"]
    assert by_name["return_work"]["targeted"]["description"]
    assert by_name["score_record"]["targeted"]["description"]
    assert "Driver finding ids" in json.dumps(by_name["brief_create"]["finding_ids"])


def test_score_record_schema_carries_every_rubric_key():
    tool = next(t for t in asyncio.run(_list_tools()) if t.name == "score_record")
    properties = tool.input_schema["properties"]
    pairs = {
        branch["properties"]["dimension"]["const"]: tuple(branch["properties"]["criterion"]["enum"])
        for branch in properties["ratings"]["items"]["oneOf"]
    }
    assert pairs == {key: tuple(c for c, _ in criteria) for key, _, criteria in rubric.DIMENSIONS}
    assert properties["applicable"]["required"] == list(rubric.DIMENSION_KEYS)
    assert rubric.schema_help() not in (tool.description or "")
    assert len(tool.description or "") < 300


async def _call_with_agent_id() -> dict[str, Any]:
    async with Client(mcp) as client:
        return (
            await client.call_tool(
                "run_start", {"prd": "Build X", "session_id": "sess-1", "agent_id": "stamped-1"}
            )
        ).data


def test_a_stamped_agent_id_reaches_the_handler(host: Path):
    assert asyncio.run(_call_with_agent_id())["oracle"]["agent_id"] == "stamped-1"


async def _score_with_an_unknown_criterion() -> str:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        try:
            await client.call_tool(
                "score_record",
                {
                    "caller": "oracle",
                    "agent_id": "sess-1",
                    "file_id": 1,
                    "ratings": [{"dimension": "testing", "criterion": "nope", "value": 9}],
                    "applicable": {key: None for key in rubric.DIMENSION_KEYS},
                    "kind": "self",
                },
            )
        except ToolError as exc:
            return str(exc)
        raise AssertionError("expected score_record to raise a ToolError")


def test_score_record_arguments_pass_the_schema_to_the_ledger(host: Path):
    message = asyncio.run(_score_with_an_unknown_criterion())
    assert message == "the oracle role may not call score_record"


async def _brief_manager(client: Client) -> None:
    await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
    oracle = {"caller": "oracle", "agent_id": "sess-1"}
    phase = (await client.call_tool("phase_add", {**oracle, "name": "phase-1"})).data
    await client.call_tool(
        "phase_update", {**oracle, "phase_id": phase["phase_id"], "state": "unlocked"}
    )
    await client.call_tool(
        "brief_create",
        {
            **oracle,
            "child_name": "mgr-p1-phase-1",
            "child_role": "manager",
            "model": "opus",
            "body": "Own phase-1.",
            "phase_id": phase["phase_id"],
        },
    )


async def _manager_calls(tools: list[str]) -> list[str]:
    results: list[str] = []
    async with Client(mcp) as client:
        await _brief_manager(client)
        await client.call_tool("brief_ack", {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"})
        for tool in tools:
            try:
                await client.call_tool(tool, {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"})
                results.append("allowed")
            except ToolError as exc:
                results.append(str(exc))
    return results


def test_a_role_calls_only_its_own_ledger_tools(host: Path):
    assert asyncio.run(_manager_calls(["plan_unlocked", "guidelines_get"])) == [
        "the manager role may not call plan_unlocked",
        "allowed",
    ]


async def _registered_manager_calls() -> list[str]:
    calls = [
        ("guidelines_get", {"caller": "mgr-1", "agent_id": "mgr-1"}),
        ("ledger_info", {"agent_id": "mgr-1"}),
        ("who_owns", {"path": "src/a.py", "agent_id": "mgr-1"}),
        (
            "brief_get",
            {"caller_name": "mgr-1", "child_name": "mgr-p1-phase-1", "agent_id": "mgr-1"},
        ),
        ("brief_ack", {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"}),
        ("guidelines_get", {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"}),
    ]
    results: list[str] = []
    async with Client(mcp) as client:
        await _brief_manager(client)
        front._ledger().agent_register_start("mgr-1", "manager", parent_agent_id="sess-1")
        for tool, arguments in calls:
            try:
                await client.call_tool(tool, arguments)
                results.append("allowed")
            except ToolError as exc:
                results.append(str(exc))
    return results


def test_a_registered_session_reaches_only_brief_ack_and_the_identity_free_tools(host: Path):
    assert asyncio.run(_registered_manager_calls()) == [
        "you are not bound to the ledger yet: call brief_ack(caller='mgr-1'). "
        "No other ledger tool works until it succeeds",
        "allowed",
        "allowed",
        "allowed",
        "allowed",
        "allowed",
    ]


async def _events_for(targets: list[str]) -> list[list[dict[str, Any]]]:
    async with Client(mcp) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        return [
            (await client.call_tool("events", {"target_agent_id": target})).data
            for target in targets
        ]


def test_events_filters_on_target_agent_id(host: Path):
    nobody, oracle = asyncio.run(_events_for(["nobody", "sess-1"]))
    assert nobody == []
    assert oracle and all(event["agent_id"] == "sess-1" for event in oracle)


def test_ledger_info_reports_ready_status_and_tool_count(host: Path):
    data = asyncio.run(_call_tool("ledger_info", {}))
    assert data == {
        "name": "swarm-ledger",
        "version": __version__,
        "status": "ready",
        "tools": len(_TOOL_NAMES),
        "repo_root": str(host),
    }


async def _run_start_as(session_id: str) -> str:
    async with Client(mcp) as client:
        try:
            await client.call_tool("run_start", {"prd": "Build X", "session_id": session_id})
        except ToolError as exc:
            return str(exc)
    raise AssertionError("expected run_start to raise a ToolError")


def test_run_start_refuses_a_session_the_mod_never_checked_in_for(host: Path):
    message = asyncio.run(_run_start_as("main"))
    assert message.startswith(
        "run_start refuses session 'main': the sentinel-swarm mod never checked in for it"
    )


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
        await _brief_manager(client)
        brief = (
            await client.call_tool(
                "brief_get", {"caller_name": "mgr-p1-phase-1", "child_name": "mgr-p1-phase-1"}
            )
        ).data
        acked = (
            await client.call_tool("brief_ack", {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"})
        ).data
        return {"brief": brief, "acked": acked}


def test_brief_get_and_brief_ack_flow(host: Path):
    result = asyncio.run(_brief_get_and_ack_flow())
    assert result["brief"]["child_name"] == "mgr-p1-phase-1"
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
    monkeypatch.setattr(front, "on_run_finish", finished.append)
    assert asyncio.run(_start_and_finish())["state"] == "finished"
    assert len(finished) == 1
    assert finished[0] is not None
