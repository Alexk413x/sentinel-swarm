from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from swarm_ledger import env, front, lock, serve, sessions, wake
from swarm_ledger.db import connect
from swarm_ledger.pool import Pool, WorkerError, worker_command
from swarm_ledger.server import configure, mcp
from swarm_ledger.settings import Settings, load_settings

ORACLE = {"caller": "oracle", "agent_id": "sess-1"}
MANAGER = {"caller": "mgr-p1-phase-1", "agent_id": "mgr-1"}


def _fake_claude(folder: Path) -> Path:
    if sys.platform == "win32":
        path = folder / "fake-claude.cmd"
        path.write_text("@echo []\r\n", encoding="ascii")
    else:
        path = folder / "fake-claude"
        path.write_text("#!/bin/sh\necho '[]'\n", encoding="ascii")
        path.chmod(0o755)
    return path


def _fake_notifier(folder: Path) -> Path:
    # A worker process builds the OS notification command itself, and on Linux it needs
    # notify-send on PATH; the front's test runner records the command without running it.
    bin_dir = folder / "notifier-bin"
    bin_dir.mkdir()
    path = bin_dir / "notify-send"
    path.write_text("#!/bin/sh\n", encoding="ascii")
    path.chmod(0o755)
    return bin_dir


@pytest.fixture
def workers(
    host: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claude_sessions: list[dict],
    check_in,
) -> Iterator[Pool]:
    del claude_sessions
    monkeypatch.setenv(sessions.CLAUDE_VAR, str(_fake_claude(tmp_path)))
    monkeypatch.setenv("PATH", f"{_fake_notifier(tmp_path)}{os.pathsep}{os.environ['PATH']}")
    check_in(host, "sess-1", MANAGER["agent_id"])
    pool = _pool(host)
    configure(host)
    try:
        yield pool
    finally:
        pool.close()
        configure(host)


def _pool(host: Path, limit: int = 4) -> Pool:
    snapshot = load_settings(host).snapshot()
    return Pool(limit, worker_command(host, env.db_path_for(host), os.getpid(), snapshot))


def _use(host: Path, pool: Pool | None) -> None:
    configure(host, pool)


async def _call(name: str, arguments: dict[str, Any]) -> Any:
    async with Client(mcp) as client:
        return (await client.call_tool(name, arguments)).data


def call(name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(_call(name, arguments))


def _seed_manager() -> int:
    started = call("run_start", {"prd": "Build X", "session_id": "sess-1"})
    phase = call("phase_add", {**ORACLE, "name": "phase-1"})
    call("phase_update", {**ORACLE, "phase_id": phase["phase_id"], "state": "unlocked"})
    call(
        "brief_create",
        {
            **ORACLE,
            "child_name": "mgr-p1-phase-1",
            "child_role": "manager",
            "model": "opus",
            "body": "Own phase-1.",
            "phase_id": phase["phase_id"],
        },
    )
    call("brief_ack", MANAGER)
    return started["run"]["run_id"]


def test_max_workers_defaults_to_8_and_0_turns_the_pool_off(host: Path) -> None:
    settings = host / ".claude" / "sentinel-swarm.local.md"
    assert load_settings(host).max_workers == 8
    assert serve.start_pool(host, load_settings(host)) is not None
    for raw, expected in (("0", 0), ("-3", 0), ("3", 3), ("lots", 8)):
        text = settings.read_text(encoding="utf-8").replace("max_workers: 8", f"max_workers: {raw}")
        settings.write_text(text, encoding="utf-8")
        assert load_settings(host).max_workers == expected
        settings.write_text(text.replace(f"max_workers: {raw}", "max_workers: 8"), encoding="utf-8")
    settings.write_text(
        settings.read_text(encoding="utf-8").replace("max_workers: 8", "max_workers: 0"),
        encoding="utf-8",
    )
    assert serve.start_pool(host, load_settings(host)) is None


def test_a_settings_snapshot_rebuilds_the_same_settings(host: Path) -> None:
    settings = load_settings(host)
    settings.rubric.target = 77
    settings.models["coder"] = ["haiku"]
    assert Settings.from_snapshot(settings.snapshot()) == settings


def test_a_profile_set_in_one_worker_reaches_tests_run_in_another(
    host: Path, workers: Pool
) -> None:
    _seed_manager()
    other = _pool(host, 1)
    try:
        _use(host, workers)
        call("profile_set", {**ORACLE, "test_command": "echo 3 passed"})
        _use(host, other)
        ran = call("tests_run", {**MANAGER, "scope": "phase"})
    finally:
        other.close()
    assert ran["command"].startswith("echo 3 passed")
    assert ran["passed"] == 3


READS: list[tuple[str, dict[str, Any]]] = [
    ("run_status", ORACLE),
    ("status_tree", ORACLE),
    ("issue_list", MANAGER),
    ("who_owns", {"path": "src/a.py"}),
    ("events", {}),
    ("ledger_info", {}),
    ("guidelines_get", MANAGER),
    ("brief_get", {"caller_name": "mgr-p1-phase-1", "child_name": "mgr-p1-phase-1"}),
]


def test_a_pooled_and_an_in_process_ledger_return_equal_results(host: Path, workers: Pool) -> None:
    _seed_manager()
    in_process = [call(name, args) for name, args in READS]
    _use(host, workers)
    pooled = [call(name, args) for name, args in READS]
    assert pooled == in_process
    assert workers.size() >= 1


def test_a_pooled_refusal_is_the_same_tool_error(host: Path, workers: Pool) -> None:
    _seed_manager()
    messages = []
    for pool in (None, workers):
        _use(host, pool)
        with pytest.raises(ToolError) as refused:
            call("plan_unlocked", MANAGER)
        messages.append(str(refused.value))
    assert messages[0] == messages[1] == "the manager role may not call plan_unlocked"


def test_a_pooled_write_lands_in_the_shared_ledger(host: Path, workers: Pool) -> None:
    _seed_manager()
    _use(host, workers)
    posted = call("message_post", {**ORACLE, "to_name": "mgr-p1-phase-1", "body": "pooled"})
    _use(host, None)
    inbox = call("message_inbox", MANAGER)
    assert [m["message_id"] for m in inbox["messages"]] == [posted["message_id"]]


def test_a_pooled_message_owes_one_wake_up(host: Path, workers: Pool) -> None:
    run_id = _seed_manager()
    conn = connect(env.db_path_for(host))
    conn.execute("UPDATE agents SET session_name = 'mgr-session' WHERE agent_id = 'mgr-1'")
    _use(host, workers)
    posted = call("message_post", {**ORACLE, "to_name": "mgr-p1-phase-1", "body": "hello"})
    rows = conn.execute("SELECT * FROM wakeups WHERE run_id = ?", (run_id,)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["to_session_name"] == "mgr-session"
    assert posted["next"] == wake.mod_step(dict(rows[0]))


def test_the_front_shows_each_pooled_notification_once(
    host: Path, workers: Pool, os_notifications: list[list[str]]
) -> None:
    _seed_manager()
    _use(host, workers)
    result = call("drive_unavailable", {**ORACLE, "reason": "the driver plugin failed"})
    assert result["notification"] is not None
    assert len(os_notifications) == 1


def test_concurrent_writers_across_workers_all_land(host: Path, workers: Pool) -> None:
    _seed_manager()
    _use(host, workers)
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for i in range(5):
                call("message_post", {**ORACLE, "to_name": "mgr-p1-phase-1", "body": f"{n}-{i}"})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert workers.size() > 1
    conn = connect(env.db_path_for(host))
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    assert count == 40


def test_a_pooled_run_start_records_the_front_as_the_lock_holder(host: Path, workers: Pool) -> None:
    _use(host, workers)
    call("run_start", {"prd": "Build X", "session_id": "sess-1"})
    held = lock.read_lock(host)
    assert held is not None
    assert held["server_pid"] == os.getpid()


def test_a_pooled_hook_returns_what_the_in_process_hook_returns(host: Path, workers: Pool) -> None:
    payload = json.dumps(
        {"session_id": "sess-x", "tool_name": "mcp__swarm-ledger__ledger_info", "tool_input": {}}
    )
    expected = front.run_hook(host, "pre_ledger", payload)
    _use(host, workers)
    assert front.run_hook(host, "pre_ledger", payload) == expected


def test_a_dead_worker_fails_only_its_own_call(host: Path, workers: Pool) -> None:
    request = {"tool": None, "method": "repo_root", "args": {}, "agent_id": None}
    assert workers.call(request)["result"] == str(host)
    for pid in workers.pids():
        os.kill(pid, 9)
    time.sleep(0.2)
    try:
        workers.call(request)
    except WorkerError:
        pass
    assert workers.call(request)["result"] == str(host)


def test_the_file_lock_lets_one_holder_in_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "kg.lock"
    inside: list[int] = []
    overlaps: list[int] = []

    def hold() -> None:
        with lock.file_lock(path):
            inside.append(1)
            if len(inside) > 1:
                overlaps.append(1)
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=hold) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert overlaps == []
