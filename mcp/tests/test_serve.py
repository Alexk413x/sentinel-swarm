from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.requests import Request
from starlette.types import Message

from swarm_ledger import auth, serve, wake
from swarm_ledger import hooks as hooks_package
from swarm_ledger.hooks import run_event
from swarm_ledger.identity import LedgerError


@pytest.fixture
def host(tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))
    monkeypatch.delenv("SENTINEL_SWARM_LEDGER_DB", raising=False)
    yield root
    info = serve.read_server_info(root)
    if info is not None:
        try:
            os.kill(int(info["pid"]), signal.SIGTERM)
        except OSError:
            pass


def _wait_for(condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def _client(url: str, root: Path) -> Client:
    return Client(StreamableHttpTransport(url, headers=auth.auth_headers(root)))


async def _start_and_finish(url: str, root: Path) -> dict[str, Any]:
    async with _client(url, root) as client:
        await client.call_tool("run_start", {"prd": "Build X", "session_id": "sess-1"})
        return (
            await client.call_tool(
                "run_finish", {"caller": "oracle", "agent_id": "sess-1", "outcome": "success"}
            )
        ).data


def test_read_server_info_ignores_a_missing_or_malformed_file(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert serve.read_server_info(tmp_path) is None
    path = serve.server_info_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert serve.read_server_info(tmp_path) is None
    with pytest.raises(LedgerError, match="no ledger server is recorded"):
        serve.server_url(tmp_path)


def test_a_record_for_a_dead_server_is_not_answering(tmp_path: Path) -> None:
    info = {"url": "http://127.0.0.1:9/mcp", "port": 9, "pid": 1}
    assert serve.is_answering(info, tmp_path) is False


@pytest.mark.integration
def test_ensure_server_starts_one_server_per_repo_and_it_exits_after_run_finish(
    host: Path,
) -> None:
    url = serve.ensure_server(host, timeout=60)
    info = serve.read_server_info(host)
    assert info is not None
    assert set(info) == {"url", "port", "pid", "started_at"}
    assert url == info["url"] == f"http://127.0.0.1:{info['port']}/mcp"

    assert serve.ensure_server(host) == url
    assert serve.read_server_info(host) == info

    second = subprocess.run(
        [sys.executable, "-m", "swarm_ledger.serve", "--repo", str(host)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert second.returncode == 0
    assert second.stdout.strip() == url

    finished = asyncio.run(_start_and_finish(url, host))
    assert finished["state"] == "finished"
    assert _wait_for(lambda: not serve.server_info_path(host).exists())
    assert _wait_for(lambda: not serve.is_answering(info, host))


def _replace_command(monkeypatch: pytest.MonkeyPatch, code: str) -> list[subprocess.Popen]:
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs: Any) -> subprocess.Popen:
        del command
        process = real_popen([sys.executable, "-c", code], **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)
    return started


def test_ensure_server_reports_a_server_that_exits(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_command(monkeypatch, "import sys; sys.exit(4)")
    with pytest.raises(LedgerError, match=r"exited with code 4 .*server\.log"):
        serve.ensure_server(host, timeout=30)


def test_ensure_server_reports_a_server_that_never_answers(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _replace_command(monkeypatch, "import time; time.sleep(30)")
    try:
        with pytest.raises(LedgerError, match="did not answer within 1s"):
            serve.ensure_server(host, timeout=1)
    finally:
        for process in started:
            process.kill()
            process.wait()


def test_server_json_lives_in_the_records_folder(host: Path) -> None:
    assert serve.server_info_path(host) == host / ".sentinel-swarm" / "server.json"
    (host / ".sentinel-swarm").mkdir()
    serve.server_info_path(host).write_text(
        json.dumps({"url": "http://127.0.0.1:5/mcp", "port": 5, "pid": 1}), encoding="utf-8"
    )
    assert serve.server_url(host) == "http://127.0.0.1:5/mcp"


def _fake_sessions(monkeypatch: pytest.MonkeyPatch, listing: list[list[dict]]) -> list[str]:
    stopped: list[str] = []
    snapshots = iter(listing)
    last: list[dict] = []

    def list_sessions() -> list[dict]:
        nonlocal last
        last = next(snapshots, last)
        return last

    monkeypatch.setattr(serve.sessions, "list_sessions", list_sessions)
    monkeypatch.setattr(serve.sessions, "stop", stopped.append)
    monkeypatch.setattr(serve, "_POLL_S", 0)
    return stopped


def _oracle(status: str, kind: str = "background") -> dict:
    return {"id": "bg1", "sessionId": "sess-o", "pid": 7, "kind": kind, "status": status}


def test_a_finished_background_oracle_is_stopped_once_its_turn_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = _fake_sessions(monkeypatch, [[_oracle("busy")], [_oracle("busy")], [_oracle("idle")]])
    assert serve.stop_finished_oracle("sess-o") == "Oracle session bg1 stopped"
    assert stopped == ["bg1"]


def test_an_oracle_still_busy_at_the_deadline_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = _fake_sessions(monkeypatch, [[_oracle("busy")]])
    assert serve.stop_finished_oracle("sess-o", wait_s=0) == "Oracle session bg1 stopped"
    assert stopped == ["bg1"]


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        ([_oracle("idle", kind="interactive")], "Oracle session is interactive; left running"),
        ([], "Oracle session already ended"),
    ],
)
def test_an_interactive_or_ended_oracle_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, listing: list[dict], message: str
) -> None:
    stopped = _fake_sessions(monkeypatch, [listing])
    assert serve.stop_finished_oracle("sess-o") == message
    assert stopped == []


def test_no_recorded_oracle_stops_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = _fake_sessions(monkeypatch, [[_oracle("idle")]])
    assert serve.stop_finished_oracle(None) == "no Oracle session recorded"
    assert stopped == []


def test_write_server_info_replaces_the_record(tmp_path: Path) -> None:
    host = tmp_path
    path = serve.server_info_path(host)
    path.parent.mkdir(parents=True)
    serve.write_server_info(path, {"url": "http://127.0.0.1:5/mcp", "port": 5})
    serve.write_server_info(path, {"url": "http://127.0.0.1:6/mcp", "port": 6})
    assert serve.read_server_info(host) == {"url": "http://127.0.0.1:6/mcp", "port": 6}
    assert [p.name for p in path.parent.iterdir()] == ["server.json"]


def _hook_request(
    event: str,
    repo: Path | None,
    body: bytes = b"{}",
    client: tuple[str, int] = ("127.0.0.1", 50000),
    host_header: str = "127.0.0.1:8123",
) -> Request:
    headers = [(b"host", host_header.encode())]
    if repo is not None:
        quoted = urllib.parse.quote(str(repo)).encode()
        headers.append((serve.REPO_HEADER.lower().encode(), quoted))
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"{serve.HOOK_PATH}/{event}",
        "path_params": {"event": event},
        "headers": headers,
        "client": client,
        "server": ("127.0.0.1", 8123),
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _answer(request: Request, root: Path) -> Any:
    return asyncio.run(asyncio.wait_for(serve.answer_hook(request, root), 30))


_LEDGER_INFO = json.dumps(
    {"session_id": "sess-x", "tool_name": "mcp__swarm-ledger__ledger_info", "tool_input": {}}
).encode()


def test_the_hook_route_returns_what_the_hook_prints(host: Path) -> None:
    response = _answer(_hook_request("pre_ledger", host, _LEDGER_INFO), host)
    expected, _ = run_event("pre_ledger", _LEDGER_INFO.decode(), host)
    assert response.status_code == 200
    assert response.body == expected.encode()
    assert json.loads(response.body)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert urllib.parse.unquote(response.headers[serve.REPO_HEADER]) == str(host)


def test_the_hook_route_refuses_an_unknown_event(host: Path) -> None:
    assert _answer(_hook_request("nope", host), host).status_code == 404


@pytest.mark.parametrize(
    ("client", "host_header"),
    [(("10.0.0.5", 50000), "127.0.0.1:8123"), (("127.0.0.1", 50000), "evil.example:8123")],
)
def test_the_hook_route_answers_only_local_callers(
    host: Path, client: tuple[str, int], host_header: str
) -> None:
    request = _hook_request("pre_ledger", host, client=client, host_header=host_header)
    assert _answer(request, host).status_code == 403


def test_the_hook_route_refuses_a_caller_from_another_repo(host: Path, tmp_path: Path) -> None:
    assert _answer(_hook_request("pre_ledger", tmp_path / "other"), host).status_code == 409
    assert _answer(_hook_request("pre_ledger", None), host).status_code == 409


def test_the_hook_route_does_not_wait_for_the_tool_call_lock(host: Path) -> None:
    from swarm_ledger import server

    with server._CALL_LOCK:
        response = _answer(_hook_request("pre_ledger", host, _LEDGER_INFO), host)
    assert response.status_code == 200


def test_the_hook_route_runs_each_hook_on_its_own_ledger_and_an_empty_hub(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path | None, Any]] = []

    def fake_run_event(
        event: str, raw: str, root: Path | None = None, hub: Any = None
    ) -> tuple[str, str]:
        calls.append((root, hub))
        return '{"x": 1}', "a stderr line\n"

    monkeypatch.setattr(hooks_package, "run_event", fake_run_event)
    assert serve.run_hook(host, "stop", b"{}") == '{"x": 1}'
    assert serve.run_hook(host, "stop", b"{}") == '{"x": 1}'
    (root, hub), (_, second_hub) = calls
    assert root == host
    assert isinstance(hub, wake.EventHub) and hub is not wake.HUB and hub is not second_hub


def _load_shim(repo_root: Path) -> Any:
    path = repo_root / "templates" / "hook_shim.py"
    spec = importlib.util.spec_from_file_location("sentinel_swarm_hook_shim_serve", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_the_shim_answers_a_hook_through_the_running_server(
    host: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve.ensure_server(host, timeout=60)
    shim = _load_shim(repo_root)
    monkeypatch.setattr(shim, "repo_root", lambda: host)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(host))

    def no_fallback(*args: Any) -> bytes:
        raise AssertionError("the shim fell back to uv run")

    monkeypatch.setattr(shim, "run_ledger_hook", no_fallback)
    expected, _ = run_event("pre_ledger", _LEDGER_INFO.decode(), host)
    assert shim.run_hook("pre_ledger", _LEDGER_INFO) == expected.encode()


_TOKEN = "k" * 43
_PORT = 8123


def _guard_call(
    path: str = "/mcp",
    headers: dict[str, str | None] | None = None,
    client: tuple[str, int] = ("127.0.0.1", 50000),
) -> tuple[int, bytes, bool]:
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    merged: dict[str, str | None] = {
        "host": f"127.0.0.1:{_PORT}",
        "authorization": f"Bearer {_TOKEN}",
        **(headers or {}),
    }
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [
            (name.encode(), value.encode()) for name, value in merged.items() if value is not None
        ],
        "client": client,
        "server": ("127.0.0.1", _PORT),
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
    }
    sent: list[Message] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(serve.Guard(app, _TOKEN, _PORT)(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body, bool(reached)


_GUARDED = ["/mcp", "/hook/pre_ledger", "/events"]
_ALL_PATHS = [*_GUARDED, "/health"]


@pytest.mark.parametrize("path", _ALL_PATHS)
def test_the_guard_passes_a_local_caller_with_the_token(path: str) -> None:
    assert _guard_call(path) == (200, b"ok", True)


@pytest.mark.parametrize("path", _GUARDED)
@pytest.mark.parametrize("authorization", [None, "", f"Bearer {_TOKEN}x", _TOKEN, "Bearer "])
def test_the_guard_refuses_a_missing_or_wrong_token_with_403(
    path: str, authorization: str | None
) -> None:
    status, body, reached = _guard_call(path, {"authorization": authorization})
    assert (status, reached) == (403, False)
    assert b"bearer token" in body


def test_the_health_check_needs_no_token() -> None:
    assert _guard_call("/health", {"authorization": None})[0] == 200


@pytest.mark.parametrize("path", _ALL_PATHS)
@pytest.mark.parametrize(
    "host_header", ["evil.example", f"evil.example:{_PORT}", "127.0.0.1:9", "127.0.0.2:8123", ""]
)
def test_the_guard_refuses_a_wrong_host_with_403(path: str, host_header: str) -> None:
    status, body, reached = _guard_call(path, {"host": host_header})
    assert (status, reached) == (403, False)
    assert body == f"the ledger answers only requests addressed to 127.0.0.1:{_PORT}".encode()


def test_the_guard_takes_localhost_as_the_host() -> None:
    assert _guard_call("/mcp", {"host": f"localhost:{_PORT}"})[0] == 200


@pytest.mark.parametrize("path", _ALL_PATHS)
@pytest.mark.parametrize(
    "origin", ["http://evil.example", "http://localhost:3000", f"https://127.0.0.1:{_PORT}", "null"]
)
def test_the_guard_refuses_a_foreign_origin_with_403(path: str, origin: str) -> None:
    status, body, reached = _guard_call(path, {"origin": origin})
    assert (status, reached) == (403, False)
    assert body == b"the ledger refuses requests from a web page"


def test_the_guard_takes_the_ledgers_own_origin() -> None:
    assert _guard_call("/mcp", {"origin": f"http://127.0.0.1:{_PORT}"})[0] == 200


def test_the_guard_refuses_a_remote_caller_with_403() -> None:
    status, body, reached = _guard_call("/mcp", client=("10.0.0.2", 50000))
    assert (status, reached) == (403, False)
    assert body == b"the ledger answers only local callers"


def test_ensure_token_creates_one_token_and_keeps_it(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    token = auth.ensure_token(tmp_path)
    path = auth.token_path(tmp_path)
    assert path == tmp_path / ".sentinel-swarm" / "http-token"
    assert auth.ensure_token(tmp_path) == token == auth.read_token(tmp_path)
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("not a token", "utf-8")
    replaced = auth.ensure_token(tmp_path)
    assert replaced != token
    assert auth.read_token(tmp_path) == replaced


def _status(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> int:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def _ledger_info(url: str, root: Path) -> dict[str, Any]:
    async with _client(url, root) as client:
        return (await client.call_tool("ledger_info", {})).data


@pytest.mark.integration
def test_the_live_ledger_refuses_callers_without_the_token_and_keeps_it_across_a_restart(
    host: Path,
) -> None:
    url = serve.ensure_server(host, timeout=60)
    token = auth.read_token(host)
    info = serve.read_server_info(host)
    assert info is not None
    base = f"http://127.0.0.1:{info['port']}"
    repo_header = {serve.REPO_HEADER: urllib.parse.quote(str(host))}
    assert _status(f"{base}/health") == 200
    assert _status(f"{base}/mcp") == 403
    assert _status(f"{base}/mcp", {"Authorization": "Bearer wrong"}) == 403
    assert _status(f"{base}/events?session=s") == 403
    assert _status(f"{base}/hook/pre_ledger", repo_header, b"{}") == 403
    assert _status(f"{base}/health", {"Host": "evil.example"}) == 403
    assert _status(f"{base}/health", {"Origin": "http://evil.example"}) == 403
    assert asyncio.run(_ledger_info(url, host))["name"] == "swarm-ledger"

    os.kill(int(info["pid"]), signal.SIGTERM)
    assert _wait_for(lambda: not serve.is_answering(info, host))
    assert serve.ensure_server(host, timeout=60) == url
    assert auth.read_token(host) == token
    assert asyncio.run(_ledger_info(url, host))["name"] == "swarm-ledger"
