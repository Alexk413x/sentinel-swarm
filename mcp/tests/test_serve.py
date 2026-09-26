from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from swarm_ledger import serve, shared
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


async def _start_and_finish(url: str) -> dict[str, Any]:
    async with Client(url) as client:
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
    assert set(info) == {"url", "port", "pid", "started_at", "servers"}
    assert info["servers"] == {}
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

    finished = asyncio.run(_start_and_finish(url))
    assert finished["state"] == "finished"
    assert _wait_for(lambda: not serve.server_info_path(host).exists())
    assert serve.is_answering(info, host) is False


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


def test_an_oracle_in_its_own_tab_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"sessionId": "sess-o", "pid": 7, "kind": "interactive", "status": "idle"}
    entry["name"] = "host-oracle-0926-120000"
    stopped_sessions: list[str] = []
    _fake_sessions(monkeypatch, [[entry]])
    monkeypatch.setattr(serve.sessions, "stop_session", stopped_sessions.append)
    outcome = serve.stop_finished_oracle("sess-o", tab_name="host-oracle-0926-120000")
    assert outcome == "Oracle session host-oracle-0926-120000 stopped"
    assert stopped_sessions == ["sess-o"]


def test_no_recorded_oracle_stops_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = _fake_sessions(monkeypatch, [[_oracle("idle")]])
    assert serve.stop_finished_oracle(None) == "no Oracle session recorded"
    assert stopped == []


def test_write_server_info_replaces_the_record(tmp_path: Path) -> None:
    host = tmp_path
    path = serve.server_info_path(host)
    path.parent.mkdir(parents=True)
    serve.write_server_info(path, {"url": "http://127.0.0.1:5/mcp", "port": 5, "servers": None})
    serve.write_server_info(path, {"url": "http://127.0.0.1:5/mcp", "port": 5, "servers": {}})
    assert serve.read_server_info(host) == {
        "url": "http://127.0.0.1:5/mcp",
        "port": 5,
        "servers": {},
    }
    assert [p.name for p in path.parent.iterdir()] == ["server.json"]


@pytest.mark.parametrize(
    ("servers", "waits"),
    [({"servers": None}, True), ({"servers": {}}, False), ({}, False)],
)
def test_ensure_server_waits_only_while_shared_servers_start(
    tmp_path: Path, servers: dict, waits: bool
) -> None:
    host = tmp_path
    path = serve.server_info_path(host)
    path.parent.mkdir(parents=True)
    serve.write_server_info(path, {"url": "http://127.0.0.1:5/mcp", "port": 5, **servers})
    started = time.monotonic()
    serve._wait_for_shared(host, 0.5)
    assert (time.monotonic() - started >= 0.5) is waits


def _stop_ledger_hard(pid: int) -> None:
    if sys.platform == "win32":
        os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGKILL)


async def _graph_stats(url: str) -> Any:
    async with Client(url) as client:
        return (await client.call_tool("kg_graph_stats", {})).data


@pytest.mark.integration
@pytest.mark.parametrize("ending", ["run_finish", "killed"])
def test_the_ledger_shares_a11y_and_stops_it_on_exit(a11y_kg_host: Path, ending: str) -> None:
    kg_host = a11y_kg_host
    url = serve.ensure_server(kg_host, timeout=60, shared_wait=150)
    info = serve.read_server_info(kg_host)
    assert info is not None
    try:
        assert "codebase-kg" not in info["servers"]
        kg_url = info["servers"]["a11y-kg"]
        assert kg_url.startswith("http://127.0.0.1:") and kg_url.endswith("/mcp")
        assert asyncio.run(_graph_stats(kg_url))
        ports = json.loads(shared.ports_path(kg_host).read_text(encoding="utf-8"))
        assert ports["a11y-kg"] == int(kg_url.split(":")[2].split("/")[0])

        if ending == "run_finish":
            asyncio.run(_start_and_finish(url))
        else:
            _stop_ledger_hard(int(info["pid"]))
        assert _wait_for(lambda: not shared.is_answering(kg_url, timeout=0.5))
    finally:
        try:
            os.kill(int(info["pid"]), signal.SIGTERM)
        except OSError:
            pass
