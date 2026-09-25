from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from swarm_ledger import setup, shared


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_running", [])
    monkeypatch.setattr(shared, "_stopping", False)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    return root


class FakeProcess:
    def __init__(self, pid: int = 4242, exit_code: int | None = None, waits: int = 0) -> None:
        self.pid = pid
        self.returncode = exit_code
        self._slow_waits = waits
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._slow_waits > 0:
            self._slow_waits -= 1
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def _fake_server(name: str, port: int, process: FakeProcess, job: int | None = None):
    return shared.SharedServer(name, port, process, job)  # type: ignore[arg-type]


def _registry(config_dir: Path, plugins: dict[str, list[dict]]) -> None:
    path = config_dir / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")


def test_planned_servers_add_a11y_only_when_the_host_has_it(
    host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    assert shared.planned_servers(host) == [("codebase-kg@codebase-kg", "codebase-kg")]

    _registry(config_dir, {"a11y@accessibility-tools": [{"scope": "user"}]})
    assert shared.planned_servers(host) == [
        ("codebase-kg@codebase-kg", "codebase-kg"),
        ("a11y@accessibility-tools", "a11y-tools"),
        ("a11y@accessibility-tools", "a11y-kg"),
    ]


def test_free_port_reuses_a_saved_port_and_skips_a_taken_one() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((shared.HOST, 0))
        free = probe.getsockname()[1]
    assert shared.free_port(free, set()) == free
    assert shared.free_port(free, {free}) != free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind((shared.HOST, 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert shared.free_port(taken, set()) != taken


def test_saved_ports_ignore_a_malformed_file(host: Path) -> None:
    path = shared.ports_path(host)
    assert path == host / ".sentinel-swarm" / "shared-ports.json"
    assert shared.saved_ports(host) == {}
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert shared.saved_ports(host) == {}
    path.write_text(json.dumps({"a": 5000, "b": "x", "c": 70000}), encoding="utf-8")
    assert shared.saved_ports(host) == {"a": 5000}


def test_server_command_runs_the_shim_with_the_owner_pid(host: Path) -> None:
    with pytest.raises(shared.SharedServerError, match=r"hook\.py is missing"):
        shared.server_command(host, "p@m", "srv", 5000)
    shim = host / setup.SHIM_PATH
    shim.parent.mkdir(parents=True)
    shim.write_text("", encoding="utf-8")
    assert shared.server_command(host, "p@m", "srv", 5000) == [
        sys.executable,
        str(shim),
        "mcp-http",
        "p@m",
        "srv",
        "5000",
        str(os.getpid()),
    ]


def test_start_shared_records_only_the_servers_that_answer(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = [("kg@m", "codebase-kg"), ("a@m", "a11y-tools"), ("a@m", "a11y-kg"), ("x@m", "gone")]
    monkeypatch.setattr(shared, "planned_servers", lambda root: plan)
    processes = {
        "codebase-kg": FakeProcess(),
        "a11y-tools": FakeProcess(exit_code=1),
        "a11y-kg": FakeProcess(),
    }

    ports: dict[str, int] = {}

    def fake_launch(root: Path, plugin_id: str, name: str, port: int, log: Any):
        if name == "gone":
            raise shared.SharedServerError("not installed")
        ports[name] = port
        return _fake_server(name, port, processes[name])

    stopped: list[str] = []
    monkeypatch.setattr(shared, "launch", fake_launch)
    monkeypatch.setattr(shared, "stop_tree", lambda server: stopped.append(server.name))
    monkeypatch.setattr(
        shared, "is_answering", lambda url: url == shared.server_url(ports["codebase-kg"])
    )
    monkeypatch.setattr(shared, "_POLL_S", 0.01)
    recorded: list[dict[str, str]] = []

    urls = shared.start_shared(host, recorded.append, timeout=0.2)

    assert urls == {"codebase-kg": shared.server_url(ports["codebase-kg"])}
    assert len(set(ports.values())) == 3
    assert recorded == [urls]
    assert sorted(stopped) == ["a11y-kg", "a11y-tools"]
    assert set(shared.saved_ports(host)) == {"codebase-kg", "a11y-tools", "a11y-kg"}
    log = capsys.readouterr().err
    assert "shared MCP server gone did not start: not installed" in log
    assert "shared MCP server a11y-tools exited with code 1 before it answered" in log
    assert "shared MCP server a11y-kg did not answer within 0s" in log


def test_start_shared_without_the_shim_records_no_servers(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shared, "planned_servers", lambda root: [shared.CODEBASE_KG])
    recorded: list[dict[str, str]] = []
    assert shared.start_shared(host, recorded.append) == {}
    assert recorded == [{}]
    assert "shared MCP server codebase-kg did not start" in capsys.readouterr().err


def test_start_shared_records_nothing_once_the_ledger_is_stopping(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared, "planned_servers", lambda root: [])
    monkeypatch.setattr(shared, "_stopping", True)
    recorded: list[dict[str, str]] = []
    shared.start_shared(host, recorded.append)
    assert recorded == []


def test_answering_urls_keep_only_live_string_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "is_answering", lambda url: url.endswith(":1/mcp"))
    servers = {"a": "http://127.0.0.1:1/mcp", "b": "http://127.0.0.1:2/mcp", "c": 3}
    assert shared.answering_urls(servers) == {"a": "http://127.0.0.1:1/mcp"}
    assert shared.answering_urls(None) == {}


def test_posix_stop_terms_the_group_then_kills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, bool]] = []
    monkeypatch.setattr(shared, "_signal_group", lambda pid, force: signals.append((pid, force)))
    process = FakeProcess(pid=77, waits=1)
    shared._stop_posix(_fake_server("kg", 1, process), grace=0)
    assert signals == [(77, False), (77, True)]
    assert process.wait_calls == 2


def test_windows_stop_terminates_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[int] = []
    monkeypatch.setattr(shared, "_terminate_job", jobs.append)
    monkeypatch.setattr(shared.subprocess, "run", lambda *a, **k: pytest.fail("no taskkill"))
    server = _fake_server("kg", 1, FakeProcess(), job=99)
    shared._stop_windows(server, grace=0)
    assert jobs == [99]
    assert server.job is None


def test_windows_stop_without_a_job_kills_the_tree_with_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(shared.subprocess, "run", fake_run)
    shared._stop_windows(_fake_server("kg", 1, FakeProcess(pid=55)), grace=0)
    assert calls == [["taskkill", "/T", "/F", "/PID", "55"]]


def test_stop_all_stops_every_server_and_refuses_later_launches(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []
    monkeypatch.setattr(shared, "stop_tree", lambda server: stopped.append(server.name))
    monkeypatch.setattr(shared, "server_command", lambda *args: ["unused"])
    monkeypatch.setattr(shared, "spawn_tree", lambda command, cwd, log: (FakeProcess(), None))
    shared.launch(host, "p@m", "one", 1, log=None)  # type: ignore[arg-type]
    shared.launch(host, "p@m", "two", 2, log=None)  # type: ignore[arg-type]

    shared.stop_all()

    assert stopped == ["one", "two"]
    with pytest.raises(shared.SharedServerError, match="stopping"):
        shared.launch(host, "p@m", "three", 3, log=None)  # type: ignore[arg-type]
    assert stopped == ["one", "two", "three"]


def _wait_until(condition, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def _tree_command(port: int) -> list[str]:
    grandchild = [sys.executable, "-m", "http.server", str(port), "--bind", shared.HOST]
    return [sys.executable, "-c", f"import subprocess; subprocess.Popen({grandchild!r}).wait()"]


@pytest.mark.integration
def test_stop_tree_stops_the_grandchild_too(tmp_path: Path) -> None:
    port = shared.free_port(None, set())
    url = f"http://{shared.HOST}:{port}/"
    process, job = shared.spawn_tree(_tree_command(port), tmp_path, subprocess.DEVNULL)
    server = shared.SharedServer("tree", port, process, job)
    try:
        assert _wait_until(lambda: shared.is_answering(url))
        if sys.platform == "win32":
            assert job is not None
        shared.stop_tree(server)
        assert process.poll() is not None
        assert _wait_until(lambda: not shared.is_answering(url, timeout=0.5), timeout=10)
    finally:
        shared.stop_tree(server)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="job objects exist only on Windows")
def test_closing_the_job_handle_stops_the_tree(tmp_path: Path) -> None:
    port = shared.free_port(None, set())
    url = f"http://{shared.HOST}:{port}/"
    process, job = shared.spawn_tree(_tree_command(port), tmp_path, subprocess.DEVNULL)
    try:
        assert job is not None
        assert _wait_until(lambda: shared.is_answering(url))
        shared._kernel32().CloseHandle(job)
        assert _wait_until(lambda: process.poll() is not None, timeout=10)
        assert _wait_until(lambda: not shared.is_answering(url, timeout=0.5), timeout=10)
    finally:
        shared.stop_tree(shared.SharedServer("tree", port, process, None))


@pytest.fixture
def kg_only(kg_host: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(shared, "planned_servers", lambda root: [shared.CODEBASE_KG])
    return kg_host


async def kg_stats(url: str) -> Any:
    async with Client(url) as client:
        return (await client.call_tool("kg_stats", {})).data


@pytest.mark.integration
def test_codebase_kg_serves_over_http_and_stops_with_the_ledger(kg_only: Path) -> None:
    recorded: list[dict[str, str]] = []
    try:
        urls = shared.start_shared(kg_only, recorded.append, timeout=120)
        assert set(urls) == {"codebase-kg"}
        assert recorded == [urls]
        stats = asyncio.run(kg_stats(urls["codebase-kg"]))
        assert stats["nodes"] > 0
        processes = [server.process for server in shared._running]
    finally:
        shared.stop_all()
    assert all(process.poll() is not None for process in processes)
    assert _wait_until(lambda: not shared.is_answering(urls["codebase-kg"], timeout=0.5), 10)
