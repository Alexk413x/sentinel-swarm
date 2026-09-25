from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from swarm_ledger import setup, shared


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_running", [])
    monkeypatch.setattr(shared, "_stopping", False)
    monkeypatch.setattr(shared, "_supervisor", None)


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
    return shared.SharedServer("p@m", name, port, process, job)  # type: ignore[arg-type]


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
    server = shared.SharedServer("p@m", "tree", port, process, job)
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
        shared.stop_tree(shared.SharedServer("p@m", "tree", port, process, None))


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


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Launches:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.processes: list[FakeProcess] = []
        self.stopped: list[shared.SharedServer] = []
        monkeypatch.setattr(shared, "launch", self.launch)
        monkeypatch.setattr(shared, "stop_tree", self.stopped.append)

    def launch(self, root: Path, plugin_id: str, name: str, port: int, log: Any):
        self.calls.append((plugin_id, name, port))
        process = FakeProcess(pid=5000 + len(self.calls))
        self.processes.append(process)
        server = shared.SharedServer(plugin_id, name, port, process)  # type: ignore[arg-type]
        shared._running.append(server)
        return server


class Reports:
    def __init__(self) -> None:
        self.recorded: list[dict[str, str]] = []
        self.reported: list[tuple[str, str]] = []

    def record(self, urls: dict[str, str]) -> None:
        self.recorded.append(urls)

    def report(self, name: str, detail: str) -> None:
        self.reported.append((name, detail))


KG_URL = shared.server_url(5001)
A11Y_URL = shared.server_url(5002)


def _server(name: str, port: int, process: FakeProcess) -> shared.SharedServer:
    return shared.SharedServer(f"{name}@m", name, port, process)  # type: ignore[arg-type]


def _supervisor(
    host: Path,
    servers: list[shared.SharedServer],
    reports: Reports,
    clock: Clock | None = None,
    policy: shared.RestartPolicy = shared.DEFAULT_POLICY,
) -> shared.Supervisor:
    shared._running.extend(servers)
    urls = {server.name: server.url for server in servers}
    return shared.Supervisor(
        host, servers, urls, reports.record, reports.report, policy, clock or Clock()
    )


def _always(answer: bool):
    return lambda url, timeout=0: answer


def test_the_supervisor_restarts_a_dead_server_on_the_same_port(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = Launches(monkeypatch)
    monkeypatch.setattr(shared, "is_answering", _always(True))
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    live = _server("a11y-kg", 5002, FakeProcess())
    reports = Reports()
    supervisor = _supervisor(host, [dead, live], reports)

    supervisor.tick()

    assert launches.calls == [("codebase-kg@m", "codebase-kg", 5001)]
    assert launches.stopped == [dead]
    restarted = supervisor.server("codebase-kg")
    assert restarted is not None and restarted is not dead and restarted.url == KG_URL
    assert shared._running == [live, restarted]
    assert supervisor.server("a11y-kg") is live
    assert supervisor.urls == {"codebase-kg": KG_URL, "a11y-kg": A11Y_URL}
    assert reports.recorded == [] and reports.reported == []
    assert (host / ".sentinel-swarm" / "server.log").is_file()


def test_the_supervisor_backs_off_then_gives_up(
    host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    launches = Launches(monkeypatch)
    monkeypatch.setattr(shared, "is_answering", _always(True))
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    live = _server("a11y-kg", 5002, FakeProcess())
    reports = Reports()
    clock = Clock()
    supervisor = _supervisor(host, [dead, live], reports, clock)

    supervisor.tick()
    assert len(launches.calls) == 1

    for delay in (5.0, 15.0):
        launches.processes[-1].returncode = 1
        clock.now += 1
        supervisor.tick()
        due = clock.now + delay
        clock.now = due - 0.1
        supervisor.tick()
        count = len(launches.calls)
        clock.now = due
        supervisor.tick()
        assert len(launches.calls) == count + 1

    launches.processes[-1].returncode = 1
    clock.now += 1
    supervisor.tick()

    assert launches.calls == [("codebase-kg@m", "codebase-kg", 5001)] * 3
    assert supervisor.server("codebase-kg") is None
    assert supervisor.urls == {"a11y-kg": A11Y_URL}
    assert reports.recorded == [{"a11y-kg": A11Y_URL}]
    assert reports.reported == [
        ("codebase-kg", "exited with code 1 after 3 restart(s) in 5 minutes")
    ]
    clock.now += 1000
    supervisor.tick()
    assert len(launches.calls) == 3
    log = capsys.readouterr().err
    assert "codebase-kg exited with code 1; restarting it on port 5001 in 5s" in log
    assert "codebase-kg restarted on port 5001" in log
    assert "not restarting it again, and new sessions use its stdio entry" in log


def test_restarts_older_than_the_window_do_not_count(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = Launches(monkeypatch)
    monkeypatch.setattr(shared, "is_answering", _always(True))
    clock = Clock()
    policy = shared.RestartPolicy(delays_s=(0.0,), window_s=60.0)
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    supervisor = _supervisor(host, [dead], Reports(), clock, policy)

    supervisor.tick()
    launches.processes[-1].returncode = 1
    clock.now = 61.0
    supervisor.tick()

    assert len(launches.calls) == 2
    assert supervisor.server("codebase-kg") is not None


def test_the_supervisor_restarts_a_server_that_stops_answering(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = Launches(monkeypatch)
    answering = {"now": False}
    monkeypatch.setattr(shared, "is_answering", lambda url, timeout=0: answering["now"])
    fake_launch = launches.launch

    def launch_answering(*args: Any):
        answering["now"] = True
        return fake_launch(*args)

    monkeypatch.setattr(shared, "launch", launch_answering)
    hung = _server("codebase-kg", 5001, FakeProcess())
    clock = Clock()
    supervisor = _supervisor(host, [hung], Reports(), clock)

    supervisor.tick()
    clock.now = 29.9
    supervisor.tick()
    assert launches.calls == []

    clock.now = 30.0
    supervisor.tick()
    assert launches.stopped == [hung]
    assert launches.calls == [("codebase-kg@m", "codebase-kg", 5001)]


def test_a_restart_that_does_not_answer_counts_and_is_stopped(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = Launches(monkeypatch)
    monkeypatch.setattr(shared, "is_answering", _always(False))
    fake_launch = launches.launch

    def launch_dying(*args: Any):
        server = fake_launch(*args)
        server.process.returncode = 1
        return server

    monkeypatch.setattr(shared, "launch", launch_dying)
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    reports = Reports()
    policy = shared.RestartPolicy(delays_s=(0.0, 0.0))
    supervisor = _supervisor(host, [dead], reports, Clock(), policy)

    supervisor.tick()
    assert len(launches.calls) == 1 and reports.reported == []
    supervisor.tick()

    assert len(launches.calls) == 2
    assert launches.stopped[0] is dead and len(launches.stopped) == 3
    assert reports.reported == [
        ("codebase-kg", "did not answer after a restart after 2 restart(s) in 5 minutes")
    ]
    assert shared._running == []


def test_no_restart_once_the_ledger_is_stopping(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = Launches(monkeypatch)
    monkeypatch.setattr(shared, "is_answering", _always(True))
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    shared._running.append(dead)
    reports = Reports()
    policy = shared.RestartPolicy(check_every_s=60.0)
    urls = {"codebase-kg": KG_URL}
    supervisor = shared.supervise(host, urls, reports.record, reports.report, policy)
    assert supervisor is not None and supervisor.alive

    shared.stop_all()
    supervisor.tick()

    assert not supervisor.alive
    assert launches.stopped == [dead]
    assert launches.calls == []
    assert reports.recorded == [] and reports.reported == []
    assert shared.supervise(host, urls, reports.record, reports.report) is None


def test_a_restart_that_races_the_stop_is_stopped_and_not_retried(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[shared.SharedServer] = []
    monkeypatch.setattr(shared, "stop_tree", stopped.append)
    monkeypatch.setattr(shared, "server_command", lambda *args: ["unused"])
    new_process = FakeProcess(pid=6000)

    def spawn_while_stopping(command: list[str], cwd: Path, log: Any):
        monkeypatch.setattr(shared, "_stopping", True)
        return new_process, None

    monkeypatch.setattr(shared, "spawn_tree", spawn_while_stopping)
    dead = _server("codebase-kg", 5001, FakeProcess(exit_code=1))
    reports = Reports()
    supervisor = _supervisor(host, [dead], reports)

    supervisor.tick()
    supervisor.tick()

    assert [server.process for server in stopped] == [dead.process, new_process]
    assert shared._running == []
    assert reports.recorded == [] and reports.reported == []


def _crash_tree(server: shared.SharedServer) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(server.process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        os.killpg(server.process.pid, signal.SIGKILL)


async def _call(url: str, tool: str) -> Any:
    async with Client(url) as client:
        return (await client.call_tool(tool, {})).data


async def _one_client_across_a_crash(
    url: str, supervisor: shared.Supervisor, first: shared.SharedServer, tool: str
) -> tuple[Any, Any]:
    async with Client(url) as client:
        before = (await client.call_tool(tool, {})).data
        _crash_tree(first)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = supervisor.server(first.name)
            if current is not None and current is not first:
                if await asyncio.to_thread(shared.is_answering, url):
                    break
            await asyncio.sleep(0.2)
        after = (await client.call_tool(tool, {})).data
    return before, after


def _crash_and_recover(host: Path, name: str, tool: str) -> None:
    reports = Reports()
    processes: list[subprocess.Popen[bytes]] = []
    policy = shared.RestartPolicy(check_every_s=0.2, start_timeout_s=120)
    supervisor = None
    url = ""
    try:
        urls = shared.start_shared(host, reports.record, timeout=120)
        url = urls[name]
        supervisor = shared.supervise(host, urls, reports.record, reports.report, policy)
        assert supervisor is not None
        first = supervisor.server(name)
        assert first is not None

        before, after = asyncio.run(_one_client_across_a_crash(url, supervisor, first, tool))

        restarted = supervisor.server(name)
        assert restarted is not None and restarted is not first
        assert restarted.url == url
        assert first.process.poll() is not None
        assert before and after == before
        assert asyncio.run(_call(url, tool)) == before
        assert reports.recorded == [urls] and reports.reported == []
        processes = [server.process for server in shared._running]
    finally:
        shared.stop_all()
    assert supervisor is not None and not supervisor.alive
    assert processes and all(process.poll() is not None for process in processes)
    assert _wait_until(lambda: not shared.is_answering(url, timeout=0.5), 10)


@pytest.mark.integration
def test_a_crashed_codebase_kg_comes_back_on_the_same_url(kg_only: Path) -> None:
    _crash_and_recover(kg_only, "codebase-kg", "kg_stats")


@pytest.mark.integration
def test_a_crashed_a11y_kg_comes_back_on_the_same_url(
    a11y_kg_host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = [("a11y@accessibility-tools", "a11y-kg")]
    monkeypatch.setattr(shared, "planned_servers", lambda root: plan)
    _crash_and_recover(a11y_kg_host, "a11y-kg", "kg_graph_stats")


LEGACY_PROTOCOL = "2025-06-18"


class LegacyClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._next_id = 0

    def post(self, body: dict[str, Any]) -> tuple[int, str]:
        request = urllib.request.Request(
            self.url, json.dumps(body).encode("utf-8"), self.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                session = response.headers.get("mcp-session-id")
                if session:
                    self.headers["mcp-session-id"] = session
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def request(self, method: str, params: dict[str, Any] | None = None) -> tuple[int, str]:
        self._next_id += 1
        return self.post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )

    def initialize(self) -> None:
        info = {"name": "legacy-probe", "version": "0"}
        params = {"protocolVersion": LEGACY_PROTOCOL, "capabilities": {}, "clientInfo": info}
        status, _ = self.request("initialize", params)
        assert status == 200
        self.headers["mcp-protocol-version"] = LEGACY_PROTOCOL
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def _serve_ledger_through_the_wrapper(port: int, cwd: Path) -> shared.SharedServer:
    command = [
        sys.executable,
        str(setup.SHIM_TEMPLATE),
        "mcp-entry",
        str(port),
        str(os.getpid()),
        "swarm-ledger",
    ]
    process, job = shared.spawn_tree(command, cwd, subprocess.DEVNULL)
    server = shared.SharedServer("p@m", "swarm-ledger", port, process, job)
    assert _wait_until(lambda: shared.is_answering(server.url))
    return server


@pytest.mark.integration
def test_the_http_wrapper_keeps_a_client_working_across_a_restart(tmp_path: Path) -> None:
    port = shared.free_port(None, set())
    servers = [_serve_ledger_through_the_wrapper(port, tmp_path)]
    try:
        client = LegacyClient(servers[0].url)
        client.initialize()
        status, body = client.request("tools/list")
        assert status == 200 and "ledger_info" in body
        assert "mcp-session-id" not in client.headers

        shared.stop_tree(servers[0])
        servers.append(_serve_ledger_through_the_wrapper(port, tmp_path))

        status, body = client.request("tools/list")
        assert status == 200 and "ledger_info" in body
    finally:
        for server in servers:
            shared.stop_tree(server)
