from __future__ import annotations

import asyncio
import http.client
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from swarm_ledger import auth, catalog, front, serve
from swarm_ledger.http_front import MODERN_PROTOCOL, LedgerHttpServer
from swarm_ledger.server import mcp


@pytest.fixture
def lean(host: Path, claude_sessions: list[dict]) -> Iterator[int]:
    del claude_sessions
    token = auth.ensure_token(host)
    sock = serve.bind_socket(host)
    port = sock.getsockname()[1]
    front.configure(host)
    httpd = LedgerHttpServer(
        sock,
        host,
        lambda client, headers, path: serve.refusal(client, headers, path, token, port),
        {"name": "swarm-ledger", "repo_root": str(host), "pid": os.getpid()},
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_catalog_matches_the_fastmcp_registrations() -> None:
    assert catalog.dumps(catalog.generate()) == catalog.PATH.read_text(encoding="utf-8")


ORACLE = {"caller": "oracle", "agent_id": "sess-1"}
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("run_start", {"prd": "Build X", "session_id": "sess-1"}),
    ("run_status", ORACLE),
    ("issue_list", ORACLE),
    ("events", {"limit": "5"}),
    ("who_owns", {"path": "src/a.py"}),
    ("ledger_info", {}),
    ("plan_unlocked", {"caller": "nobody", "agent_id": "nobody"}),
    (
        "brief_create",
        {**ORACLE, "child_name": "m", "child_role": "manager", "model": "x", "body": "b"},
    ),
    ("analytics_query", {**ORACLE, "sql": "SELECT prd FROM runs"}),
    ("run_finish", {**ORACLE, "outcome": "success"}),
]


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "-" if k == "at" or k.endswith(("_at", "_path")) or k == "repo_root" else _scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _text(block: Any) -> Any:
    try:
        return _scrub(json.loads(block.text))
    except ValueError:
        return block.text


def _shape(result: Any) -> dict[str, Any]:
    return {
        "is_error": result.is_error,
        "content": [_text(c) for c in result.content],
        "structured": _scrub(result.structured_content),
        "data": _scrub(result.data) if not result.is_error else None,
    }


async def _run(client: Client) -> list[dict[str, Any]]:
    async with client:
        return [
            _shape(await client.call_tool(name, args, raise_on_error=False)) for name, args in CALLS
        ]


def test_the_lean_front_returns_what_fastmcp_returns(
    host: Path, tmp_path: Path, lean: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished: list[str | None] = []
    monkeypatch.setattr(front, "on_run_finish", finished.append)
    url = f"http://127.0.0.1:{lean}/mcp"
    over_http = asyncio.run(
        _run(Client(StreamableHttpTransport(url, headers=auth.auth_headers(host))))
    )
    assert finished == ["sess-1"]

    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".claude").mkdir()
    (other / ".claude" / "sentinel-swarm.local.md").write_text(
        (host / ".claude" / "sentinel-swarm.local.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    front.configure(other)
    in_process = asyncio.run(_run(Client(mcp)))
    assert over_http == in_process
    assert over_http[6]["is_error"] is True
    assert over_http[2]["content"] == []


def _rpc(port: int, root: Path, body: dict[str, Any], headers: dict[str, str]) -> Any:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(
            "POST",
            "/mcp",
            body=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **auth.auth_headers(root),
                **headers,
            },
        )
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else None
    finally:
        conn.close()


def _modern(method: str, params: dict[str, Any], name: str | None = None) -> tuple[dict, dict]:
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {"MCP-Protocol-Version": MODERN_PROTOCOL, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}}
    return body, headers


def test_the_lean_front_speaks_the_stateless_protocol(host: Path, lean: int) -> None:
    status, reply = _rpc(lean, host, *_modern("server/discover", {}))
    assert status == 200
    assert reply["result"]["supportedVersions"] == [MODERN_PROTOCOL]
    status, reply = _rpc(lean, host, *_modern("tools/list", {}))
    assert {t["name"] for t in reply["result"]["tools"]} == set(catalog.TOOLS)
    body, headers = _modern(
        "tools/call", {"name": "who_owns", "arguments": {"path": "a"}}, "who_owns"
    )
    status, reply = _rpc(lean, host, body, headers)
    assert status == 200
    assert reply["result"]["structuredContent"] == {"path": "a", "owner": None}
    assert reply["result"]["resultType"] == "complete"


def test_the_lean_front_speaks_the_handshake_protocol(host: Path, lean: int) -> None:
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    status, reply = _rpc(lean, host, init, {})
    assert status == 200
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    status, reply = _rpc(lean, host, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {})
    assert (status, reply) == (202, None)
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "who_owns", "arguments": {"path": "a"}},
    }
    status, reply = _rpc(lean, host, call, {"MCP-Protocol-Version": "2025-06-18"})
    assert reply["result"]["structuredContent"] == {"path": "a", "owner": None}


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("events", {"limit": "7"}, {"target_agent_id": None, "limit": 7}),
        ("events", {"limit": 7.0}, {"target_agent_id": None, "limit": 7}),
        ("run_status", {"caller": "o", "include_prd": "yes"}, {"caller": "o", "include_prd": True}),
    ],
)
def test_validate_coerces_like_pydantic_lax_mode(
    name: str, args: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert catalog.validate(name, args) == expected


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("who_owns", {}, "path\n  Missing required argument"),
        ("who_owns", {"path": 3}, "path\n  Input should be a valid string"),
        ("who_owns", {"path": "a", "extra": 1}, "extra\n  Unexpected keyword argument"),
        ("tests_run", {"caller": "o", "scope": "bad"}, "scope\n  Input should be one of"),
    ],
)
def test_validate_refuses_what_pydantic_refuses(
    name: str, args: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message.split("\n")[0]) as refused:
        catalog.validate(name, args)
    assert message in str(refused.value)


def test_validate_checks_a_display_schema_argument_by_its_python_type() -> None:
    listed = catalog.TOOLS["score_record"]["inputSchema"]["properties"]["ratings"]
    checked = catalog._CHECKS["score_record"]["properties"]["ratings"]
    assert listed != checked
    assert checked == {"items": {"additionalProperties": True, "type": "object"}, "type": "array"}
