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
from swarm_ledger.http_front import (
    CLASSIC_VERSIONS,
    MODERN_PROTOCOL,
    SUPPORTED_VERSIONS,
    LedgerHttpServer,
    header_value,
)
from swarm_ledger.server import mcp


@pytest.fixture
def lean(host: Path, claude_sessions: list[dict], check_in) -> Iterator[int]:
    del claude_sessions
    check_in(host, "sess-1")
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
    host: Path, tmp_path: Path, lean: int, monkeypatch: pytest.MonkeyPatch, check_in
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
    check_in(other, "sess-1")
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
    assert reply["result"]["supportedVersions"] == SUPPORTED_VERSIONS
    assert SUPPORTED_VERSIONS == [MODERN_PROTOCOL, *reversed(CLASSIC_VERSIONS)]
    status, reply = _rpc(lean, host, *_modern("tools/list", {}))
    assert {t["name"] for t in reply["result"]["tools"]} == set(catalog.TOOLS)
    body, headers = _modern(
        "tools/call", {"name": "who_owns", "arguments": {"path": "a"}}, "who_owns"
    )
    status, reply = _rpc(lean, host, body, headers)
    assert status == 200
    assert reply["result"]["structuredContent"] == {"path": "a", "owner": None}
    assert reply["result"]["resultType"] == "complete"


_CALL = {"name": "who_owns", "arguments": {"path": "a"}}


def _error(port: int, root: Path, body: dict, headers: dict) -> tuple[int, int]:
    status, reply = _rpc(port, root, body, headers)
    return status, reply["error"]["code"]


def test_an_unserved_version_is_refused_before_any_other_check(host: Path, lean: int) -> None:
    body, headers = _modern("tools/call", _CALL, "some_other_tool")
    headers["MCP-Protocol-Version"] = "2027-01-01"
    headers["Mcp-Method"] = "tools/list"
    del body["params"]["_meta"]

    status, reply = _rpc(lean, host, body, headers)

    assert status == 400
    assert reply["error"]["code"] == -32022
    assert reply["error"]["data"] == {"supported": SUPPORTED_VERSIONS, "requested": "2027-01-01"}
    _, discovered = _rpc(lean, host, *_modern("server/discover", {}))
    assert discovered["result"]["supportedVersions"] == reply["error"]["data"]["supported"]


@pytest.mark.parametrize("params", [None, {}, {"_meta": "x"}, {"_meta": {}}])
def test_a_modern_request_without_the_envelope_is_invalid_params(
    host: Path, lean: int, params: object
) -> None:
    body, headers = _modern("tools/list", {})
    if params is None:
        del body["params"]
    else:
        body["params"] = params

    assert _error(lean, host, body, headers) == (400, -32602)


def test_a_modern_request_missing_one_envelope_key_is_invalid_params(host: Path, lean: int) -> None:
    for key in (
        "io.modelcontextprotocol/protocolVersion",
        "io.modelcontextprotocol/clientCapabilities",
    ):
        body, headers = _modern("tools/list", {})
        del body["params"]["_meta"][key]
        assert _error(lean, host, body, headers) == (400, -32602)


@pytest.mark.parametrize(
    "change",
    [
        {"Mcp-Method": None},
        {"Mcp-Method": "tools/list"},
        {"Mcp-Name": None},
        {"Mcp-Name": "run_status"},
        {"Mcp-Name": "who%5Fowns"},
        {"Mcp-Name": "=?base64?not base64?="},
        {"Mcp-Name": "=?base64?cnVuX3N0YXR1cw==?="},
    ],
)
def test_a_header_that_disagrees_with_the_body_is_a_header_mismatch(
    host: Path, lean: int, change: dict[str, str | None]
) -> None:
    body, headers = _modern("tools/call", _CALL, "who_owns")
    for name, value in change.items():
        if value is None:
            del headers[name]
        else:
            headers[name] = value

    assert _error(lean, host, body, headers) == (400, -32020)


def test_a_version_header_that_disagrees_with_the_envelope_is_a_header_mismatch(
    host: Path, lean: int
) -> None:
    body, headers = _modern("tools/list", {})
    body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2025-11-25"

    assert _error(lean, host, body, headers) == (400, -32020)


@pytest.mark.parametrize("sent", ["  who_owns 	", "=?base64?d2hvX293bnM=?="])
def test_the_name_header_is_read_without_padding_and_from_its_base64_form(
    host: Path, lean: int, sent: str
) -> None:
    status, reply = _rpc(lean, host, *_modern("tools/call", _CALL, sent))

    assert status == 200
    assert reply["result"]["structuredContent"] == {"path": "a", "owner": None}


def test_header_value_decodes_only_the_exact_sentinel_form() -> None:
    assert header_value(None) is None
    assert header_value(" a b ") == "a b"
    assert header_value("=?base64?w6k=?=") == "é"
    assert header_value("=?BASE64?w6k=?=") == "=?BASE64?w6k=?="
    assert header_value("=?base64?/w==?=") is None


def test_ping_is_answered_only_on_the_handshake_protocol(host: Path, lean: int) -> None:
    status, reply = _rpc(lean, host, *_modern("ping", {}))
    assert status == 404
    assert reply["error"]["code"] == -32601

    ping = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
    status, reply = _rpc(lean, host, ping, {"MCP-Protocol-Version": "2025-11-25"})
    assert (status, reply["result"]) == (200, {})


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("prompts/list", "prompts"),
        ("resources/list", "resources"),
        ("resources/templates/list", "resourceTemplates"),
    ],
)
def test_a_list_of_an_undeclared_capability_is_answered_only_on_the_handshake_protocol(
    host: Path, lean: int, method: str, key: str
) -> None:
    status, reply = _rpc(lean, host, *_modern(method, {}))
    assert status == 404
    assert reply["error"]["code"] == -32601
    _, discovered = _rpc(lean, host, *_modern("server/discover", {}))
    assert set(discovered["result"]["capabilities"]) == {"tools"}

    listing = {"jsonrpc": "2.0", "id": 4, "method": method}
    status, reply = _rpc(lean, host, listing, {"MCP-Protocol-Version": "2025-11-25"})
    assert (status, reply["result"]) == (200, {key: []})


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
