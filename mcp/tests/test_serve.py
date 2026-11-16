from __future__ import annotations

import asyncio
import contextlib
import http.client
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from helpers import ledger_for, make_host

from swarm_ledger import __version__, auth, front, procs, serve
from swarm_ledger import hooks as hooks_package
from swarm_ledger.hooks import run_event
from swarm_ledger.http_front import LedgerHttpServer
from swarm_ledger.identity import LedgerError


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = make_host(tmp_path / "host")
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))
    monkeypatch.delenv("SENTINEL_SWARM_LEDGER_DB", raising=False)
    yield root
    info = serve.read_server_info(root)
    if info is not None:
        with contextlib.suppress(OSError):
            os.kill(int(info["pid"]), signal.SIGTERM)


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
    host: Path, check_in
) -> None:
    url = serve.ensure_server(host, timeout=60)
    info = serve.read_server_info(host)
    assert info is not None
    assert set(info) == {"url", "port", "pid", "started_at", "version", "plugin_root"}
    assert info["version"] == __version__
    assert info["plugin_root"] == str(serve.PLUGIN_ROOT)
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

    check_in(host, "sess-1")
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


@pytest.fixture
def lean(host: Path) -> Iterator[int]:
    token = auth.ensure_token(host)
    sock = serve.bind_socket(host)
    port = sock.getsockname()[1]
    front.configure(host)
    httpd = LedgerHttpServer(
        sock,
        host,
        lambda client, headers, path: serve.refusal(client, headers, path, token, port),
        serve.health_info(host),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(
    port: int,
    path: str,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


def _hook(port: int, root: Path, event: str, repo: Path | None, body: bytes = b"{}") -> Any:
    headers = auth.auth_headers(root)
    if repo is not None:
        headers[serve.REPO_HEADER] = urllib.parse.quote(str(repo))
    return _post(port, f"{serve.HOOK_PATH}/{event}", body, headers)


_LEDGER_INFO = json.dumps(
    {"session_id": "sess-x", "tool_name": "mcp__swarm-ledger__ledger_info", "tool_input": {}}
).encode()


def test_the_hook_route_returns_what_the_hook_prints(host: Path, lean: int) -> None:
    status, body, headers = _hook(lean, host, "pre_ledger", host, _LEDGER_INFO)
    expected, _ = run_event("pre_ledger", _LEDGER_INFO.decode(), host)
    assert status == 200
    assert body == expected.encode()
    assert json.loads(body)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert urllib.parse.unquote(headers[serve.REPO_HEADER]) == str(host)


def test_the_hook_route_refuses_an_unknown_event(host: Path, lean: int) -> None:
    assert _hook(lean, host, "nope", host)[0] == 404


def test_the_hook_route_refuses_a_caller_from_another_repo(
    host: Path, tmp_path: Path, lean: int
) -> None:
    assert _hook(lean, host, "pre_ledger", tmp_path / "other")[0] == 409
    assert _hook(lean, host, "pre_ledger", None)[0] == 409


def test_the_hook_route_does_not_wait_for_the_tool_call_lock(host: Path, lean: int) -> None:
    with front._CALL_LOCK:
        assert _hook(lean, host, "pre_ledger", host, _LEDGER_INFO)[0] == 200


def test_the_hook_route_runs_each_hook_on_the_repo_root(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path | None] = []

    def fake_run_event(event: str, raw: str, root: Path | None = None) -> tuple[str, str]:
        calls.append(root)
        return '{"x": 1}', "a stderr line\n"

    monkeypatch.setattr(hooks_package, "run_event", fake_run_event)
    front.configure(host)
    assert front.run_hook(host, "stop", "{}") == ('{"x": 1}', "a stderr line\n")
    assert calls == [host]


def test_the_lean_front_answers_health_without_a_token(host: Path, lean: int) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", lean, timeout=30)
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        data = json.loads(response.read())
    finally:
        conn.close()
    assert response.status == 200
    assert data == {
        "name": "swarm-ledger",
        "repo_root": str(host),
        "pid": os.getpid(),
        "version": __version__,
        "plugin_root": str(serve.PLUGIN_ROOT),
    }


@pytest.mark.parametrize("path", ["/mcp", "/hook/pre_ledger", "/unrouted"])
def test_the_lean_front_refuses_a_missing_token_with_403(host: Path, lean: int, path: str) -> None:
    repo = {serve.REPO_HEADER: urllib.parse.quote(str(host))}
    status, body, _ = _post(lean, path, headers=repo)
    assert status == 403
    assert b"bearer token" in body


class Servers:
    """In-process stand-ins for ledger servers, started on the repo's saved port."""

    def __init__(self, host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.host = host
        self.monkeypatch = monkeypatch
        self.shutdowns = 0
        self.spawned = 0
        self.running: list[LedgerHttpServer] = []

    def start(
        self, health: dict[str, Any] | None = None, *, shutdown_route: bool = True, pid: int = 0
    ) -> dict[str, Any]:
        token = auth.ensure_token(self.host)
        sock = serve.bind_socket(self.host)
        port = sock.getsockname()[1]
        front.configure(self.host)
        body = serve.health_info(self.host) if health is None else health
        index = len(self.running)
        httpd = LedgerHttpServer(
            sock,
            self.host,
            lambda client, headers, path: serve.refusal(client, headers, path, token, port),
            {**body, "pid": pid or os.getpid()},
            (lambda: self._on_shutdown(index)) if shutdown_route else None,
        )
        self.running.append(httpd)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        info = {
            "url": f"http://127.0.0.1:{port}/mcp",
            "port": port,
            "pid": pid or os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        serve.write_server_info(serve.server_info_path(self.host), info)
        return info

    def _on_shutdown(self, index: int) -> None:
        self.shutdowns += 1
        threading.Thread(target=self.stop, args=(index,), daemon=True).start()

    def stop(self, index: int) -> None:
        httpd = self.running[index]
        httpd.shutdown()
        httpd.server_close()

    def replace_launch(self) -> None:
        real_popen = subprocess.Popen

        def fake_popen(command: list[str], **kwargs: Any) -> Any:
            if str(serve.LAUNCHER) not in command:
                return real_popen(command, **kwargs)
            self.spawned += 1
            self.start()
            return types.SimpleNamespace(poll=lambda: None)

        self.monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)

    def close(self) -> None:
        for index in range(len(self.running)):
            with contextlib.suppress(OSError):
                self.stop(index)
        serve.server_info_path(self.host).unlink(missing_ok=True)


@pytest.fixture
def servers(host: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Servers]:
    stand_ins = Servers(host, monkeypatch)
    yield stand_ins
    stand_ins.close()


def _set_run_state(host: Path, state: str | None) -> None:
    ledger = ledger_for(host)
    if state is not None:
        ledger.conn.execute("INSERT INTO runs (state) VALUES (?)", (state,))
    ledger.conn.close()


def _old(host: Path, **changes: str) -> dict[str, str]:
    health = {
        "name": "swarm-ledger",
        "repo_root": str(host),
        "version": "0.0.1",
        "plugin_root": "/old/install",
    }
    return health | changes


def test_a_server_of_the_same_version_and_plugin_root_is_reused(
    host: Path, servers: Servers
) -> None:
    info = servers.start()
    servers.replace_launch()
    _set_run_state(host, "active")
    assert serve.ensure_server(host, timeout=5) == info["url"]
    assert (servers.spawned, servers.shutdowns) == (0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"version": "0.0.1"},
        {"plugin_root": "/old/install"},
        {"version": None},
        {"plugin_root": None},
        {"version": None, "plugin_root": None},
    ],
    ids=["old version", "other root", "no version", "no root", "reports neither"],
)
def test_a_server_that_differs_in_version_or_plugin_root_is_replaced(
    host: Path, servers: Servers, change: dict[str, str | None]
) -> None:
    health = {**serve.health_info(host), **change}
    servers.start({key: value for key, value in health.items() if value is not None})
    servers.replace_launch()
    _set_run_state(host, "paused")
    url = serve.ensure_server(host, timeout=10)
    assert (servers.spawned, servers.shutdowns) == (1, 1)
    info = serve.read_server_info(host)
    assert info is not None and info["url"] == url


def test_a_mismatched_server_with_no_run_is_replaced(host: Path, servers: Servers) -> None:
    servers.start(_old(host))
    servers.replace_launch()
    _set_run_state(host, None)
    serve.ensure_server(host, timeout=10)
    assert (servers.spawned, servers.shutdowns) == (1, 1)


def test_a_mismatched_server_with_an_active_run_is_refused(host: Path, servers: Servers) -> None:
    servers.start(_old(host))
    servers.replace_launch()
    _set_run_state(host, "active")
    with pytest.raises(LedgerError) as raised:
        serve.ensure_server(host, timeout=5)
    message = str(raised.value)
    for fact in ("0.0.1", __version__, "/old/install", str(serve.PLUGIN_ROOT)):
        assert fact in message
    assert "ledger.py pause --reason" in message
    assert (servers.spawned, servers.shutdowns) == (0, 0)


def test_a_refusal_names_a_server_that_reports_neither_version_nor_root_as_unknown(
    host: Path, servers: Servers
) -> None:
    servers.start({"name": "swarm-ledger", "repo_root": str(host)})
    _set_run_state(host, "active")
    with pytest.raises(LedgerError, match="unknown"):
        serve.ensure_server(host, timeout=5)


def test_a_launch_may_replace_a_mismatched_server_for_a_run_it_just_resumed(
    host: Path, servers: Servers
) -> None:
    servers.start(_old(host))
    servers.replace_launch()
    _set_run_state(host, "active")
    serve.ensure_server(host, timeout=10, replace_active=True)
    assert (servers.spawned, servers.shutdowns) == (1, 1)


def test_a_server_without_the_shutdown_route_is_terminated_by_its_recorded_pid(
    host: Path, servers: Servers, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers.start(_old(host), shutdown_route=False, pid=4242)
    servers.replace_launch()
    terminated: list[procs.Proc] = []

    def terminate(proc: procs.Proc) -> bool:
        terminated.append(proc)
        servers.stop(0)
        return True

    monkeypatch.setattr(procs, "created_of", lambda pid: 1_000)
    monkeypatch.setattr(procs, "created_epoch_ms", lambda created: 1)
    monkeypatch.setattr(procs, "terminate", terminate)
    serve.ensure_server(host, timeout=10)
    assert [(proc.pid, proc.created) for proc in terminated] == [(4242, 1_000)]
    assert servers.spawned == 1


def test_a_recorded_pid_that_started_after_the_server_is_not_terminated(
    host: Path, servers: Servers, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers.start(_old(host), shutdown_route=False, pid=4242)
    monkeypatch.setattr(procs, "created_of", lambda pid: 1_000)
    monkeypatch.setattr(procs, "created_epoch_ms", lambda created: 4_000_000_000_000)
    monkeypatch.setattr(procs, "terminate", lambda proc: pytest.fail("killed another process"))
    with pytest.raises(LedgerError, match="cannot stop"):
        serve.ensure_server(host, timeout=5)


def test_the_shutdown_route_needs_the_token_and_stops_the_server(
    host: Path, servers: Servers
) -> None:
    info = servers.start()
    assert _post(info["port"], serve.SHUTDOWN_PATH)[0] == 403
    assert servers.shutdowns == 0
    assert _post(info["port"], serve.SHUTDOWN_PATH, headers=auth.auth_headers(host))[0] == 200
    assert _wait_for(lambda: not serve.is_answering(info, host))
    assert servers.shutdowns == 1


def test_a_server_without_the_shutdown_route_answers_404(host: Path, servers: Servers) -> None:
    info = servers.start(shutdown_route=False)
    assert _post(info["port"], serve.SHUTDOWN_PATH, headers=auth.auth_headers(host))[0] == 404


def test_serve_main_refuses_a_mismatched_server_while_a_run_is_active(
    host: Path, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    servers.start(_old(host))
    _set_run_state(host, "active")
    assert serve.main(["--repo", str(host)]) == 1
    err = capsys.readouterr().err
    assert "0.0.1" in err and "ledger.py pause --reason" in err


def test_serve_main_prints_the_url_of_a_current_server(
    host: Path, servers: Servers, capsys: pytest.CaptureFixture[str]
) -> None:
    info = servers.start()
    assert serve.main(["--repo", str(host)]) == 0
    assert capsys.readouterr().out.strip() == info["url"]


def test_serve_main_replaces_a_mismatched_server_before_it_serves(
    host: Path, servers: Servers, monkeypatch: pytest.MonkeyPatch
) -> None:
    servers.start(_old(host))
    served: list[Path] = []
    monkeypatch.setattr(serve, "serve", served.append)
    _set_run_state(host, None)
    assert serve.main(["--repo", str(host)]) == 0
    assert servers.shutdowns == 1
    assert served == [host.resolve()]


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


def _refusal(
    path: str = "/mcp",
    headers: dict[str, str | None] | None = None,
    client: str = "127.0.0.1",
) -> str | None:
    merged: dict[str, str | None] = {
        "host": f"127.0.0.1:{_PORT}",
        "authorization": f"Bearer {_TOKEN}",
        **(headers or {}),
    }
    present = {name: value for name, value in merged.items() if value is not None}
    return serve.refusal(client, present, path, _TOKEN, _PORT)


_GUARDED = ["/mcp", "/hook/pre_ledger", "/unrouted"]
_ALL_PATHS = [*_GUARDED, "/health"]


@pytest.mark.parametrize("path", _ALL_PATHS)
def test_the_guard_passes_a_local_caller_with_the_token(path: str) -> None:
    assert _refusal(path) is None


@pytest.mark.parametrize("path", _GUARDED)
@pytest.mark.parametrize("authorization", [None, "", f"Bearer {_TOKEN}x", _TOKEN, "Bearer "])
def test_the_guard_refuses_a_missing_or_wrong_token(path: str, authorization: str | None) -> None:
    reason = _refusal(path, {"authorization": authorization})
    assert reason is not None
    assert "bearer token" in reason


def test_the_health_check_needs_no_token() -> None:
    assert _refusal("/health", {"authorization": None}) is None


@pytest.mark.parametrize("path", _ALL_PATHS)
@pytest.mark.parametrize(
    "host_header", ["evil.example", f"evil.example:{_PORT}", "127.0.0.1:9", "127.0.0.2:8123", ""]
)
def test_the_guard_refuses_a_wrong_host(path: str, host_header: str) -> None:
    assert _refusal(path, {"host": host_header}) == (
        f"the ledger answers only requests addressed to 127.0.0.1:{_PORT}"
    )


def test_the_guard_takes_localhost_as_the_host() -> None:
    assert _refusal("/mcp", {"host": f"localhost:{_PORT}"}) is None


@pytest.mark.parametrize("path", _ALL_PATHS)
@pytest.mark.parametrize(
    "origin", ["http://evil.example", "http://localhost:3000", f"https://127.0.0.1:{_PORT}", "null"]
)
def test_the_guard_refuses_a_foreign_origin(path: str, origin: str) -> None:
    assert _refusal(path, {"origin": origin}) == "the ledger refuses requests from a web page"


def test_the_guard_takes_the_ledgers_own_origin() -> None:
    assert _refusal("/mcp", {"origin": f"http://127.0.0.1:{_PORT}"}) is None


@pytest.mark.parametrize("client", ["10.0.0.2", None])
def test_the_guard_refuses_a_remote_caller(client: str | None) -> None:
    assert serve.refusal(client, {}, "/health", _TOKEN, _PORT) == (
        "the ledger answers only local callers"
    )


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
    assert _status(f"{base}/unrouted") == 403
    assert _status(f"{base}/hook/pre_ledger", repo_header, b"{}") == 403
    assert _status(f"{base}/health", {"Host": "evil.example"}) == 403
    assert _status(f"{base}/health", {"Origin": "http://evil.example"}) == 403
    assert asyncio.run(_ledger_info(url, host))["name"] == "swarm-ledger"

    os.kill(int(info["pid"]), signal.SIGTERM)
    assert _wait_for(lambda: not serve.is_answering(info, host))
    assert serve.ensure_server(host, timeout=60) == url
    assert auth.read_token(host) == token
    assert asyncio.run(_ledger_info(url, host))["name"] == "swarm-ledger"
