"""The launcher fallback restarts a dead ledger server for a hook of an active run."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from helpers import make_host

from swarm_ledger import __version__, env, hooks, serve
from swarm_ledger.clock import stamp
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import __main__ as entry
from swarm_ledger.hooks import restart, run_event
from swarm_ledger.identity import (
    SERVER_RESTART,
    SERVER_RESTART_COOLDOWN_S,
    SERVER_RESTART_LIMIT,
)
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")

PAYLOAD = json.dumps({"session_id": "main", "tool_name": "Read"})


class Spawns:
    def __init__(self) -> None:
        self.roots: list[Path] = []
        self.error: Exception | None = None

    def __call__(self, root: Path) -> object:
        self.roots.append(root)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(poll=lambda: None)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    return make_host(tmp_path / "host")


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> Spawns:
    spawned = Spawns()
    monkeypatch.setattr(serve, "spawn_server", spawned)
    monkeypatch.setattr(serve, "answering", lambda root: None)
    return spawned


@pytest.fixture
def run(host: Path) -> int:
    ledger = env.open_ledger(host)
    try:
        return ledger.run_start(prd="Build X", session_id="main")["run"]["run_id"]
    finally:
        ledger.conn.close()


def _events(host: Path) -> list[tuple[str, str, str]]:
    ledger = env.open_ledger(host)
    try:
        rows = ledger.conn.execute(
            "SELECT agent_id, to_state, reason FROM agent_events WHERE to_state = ? "
            "ORDER BY event_id",
            (SERVER_RESTART,),
        ).fetchall()
    finally:
        ledger.conn.close()
    return [(r["agent_id"], r["to_state"], r["reason"]) for r in rows]


def _add_restarts(host: Path, ages_s: list[float], agent_id: str = "main") -> None:
    ledger = env.open_ledger(host)
    try:
        now = datetime.now(timezone.utc)
        with write_tx(ledger.conn) as conn:
            for age in ages_s:
                conn.execute(
                    "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
                    "VALUES (?, 'working', ?, 'post_any', ?)",
                    (agent_id, SERVER_RESTART, stamp(now - timedelta(seconds=age))),
                )
    finally:
        ledger.conn.close()


def _set_run_state(host: Path, state: str) -> None:
    ledger = env.open_ledger(host)
    try:
        with write_tx(ledger.conn) as conn:
            conn.execute("UPDATE runs SET state = ?", (state,))
    finally:
        ledger.conn.close()


def test_the_constants_leave_a_server_time_to_start() -> None:
    assert SERVER_RESTART == "server_restart"
    assert SERVER_RESTART_LIMIT == 3
    assert SERVER_RESTART_COOLDOWN_S >= max(60, serve.START_TIMEOUT_S)


def test_a_dead_server_is_restarted_and_the_attempt_is_recorded(
    host: Path, run: int, spawns: Spawns
) -> None:
    assert restart.restart_server(host, "pre_write", PAYLOAD) is True

    assert spawns.roots == [host]
    assert _events(host) == [("main", SERVER_RESTART, "pre_write")]


def test_a_subagent_call_counts_for_its_sessions_agent(
    host: Path, run: int, spawns: Spawns
) -> None:
    payload = json.dumps({"session_id": "main", "agent_id": "sub-loop-1"})

    assert restart.restart_server(host, "pre_shell", payload) is True

    assert _events(host) == [("main", SERVER_RESTART, "pre_shell")]


def test_an_answering_server_is_never_restarted(
    host: Path, run: int, spawns: Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve, "answering", lambda root: ({"port": 1}, {"version": "0"}))

    assert restart.restart_server(host, "pre_write", PAYLOAD) is False

    assert spawns.roots == []
    assert _events(host) == []


def test_an_unknown_session_restarts_nothing(host: Path, run: int, spawns: Spawns) -> None:
    payload = json.dumps({"session_id": "stranger"})

    assert restart.restart_server(host, "pre_write", payload) is False
    assert restart.restart_server(host, "pre_write", "") is False
    assert restart.restart_server(host, "pre_write", "[1]") is False

    assert spawns.roots == [] and _events(host) == []


def test_an_unknown_event_restarts_nothing(host: Path, run: int, spawns: Spawns) -> None:
    assert restart.restart_server(host, "bogus", PAYLOAD) is False
    assert spawns.roots == []


@pytest.mark.parametrize("state", ["paused", "finished"])
def test_a_run_that_is_not_active_restarts_nothing(
    host: Path, run: int, spawns: Spawns, state: str
) -> None:
    _set_run_state(host, state)

    assert restart.restart_server(host, "pre_write", PAYLOAD) is False

    assert spawns.roots == [] and _events(host) == []


def test_a_fourth_attempt_spawns_nothing_and_writes_no_row(
    host: Path, run: int, spawns: Spawns
) -> None:
    _add_restarts(host, [3000, 2000, 1000])

    assert restart.restart_server(host, "pre_write", PAYLOAD) is False

    assert spawns.roots == []
    assert len(_events(host)) == SERVER_RESTART_LIMIT


def test_the_limit_counts_the_restarts_of_every_agent_of_the_run(
    host: Path, run: int, spawns: Spawns
) -> None:
    ledger = env.open_ledger(host)
    try:
        with write_tx(ledger.conn) as conn:
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, parent_agent_id, run_id, state) "
                "VALUES ('sess-coder', 'coder-1', 'coder', 'main', ?, 'working')",
                (run,),
            )
    finally:
        ledger.conn.close()
    _add_restarts(host, [3000, 2000], "sess-coder")
    _add_restarts(host, [1000])

    assert restart.restart_server(host, "pre_write", PAYLOAD) is False
    assert spawns.roots == []


def test_a_restart_inside_the_cooldown_spends_no_attempt(
    host: Path, run: int, spawns: Spawns
) -> None:
    assert restart.restart_server(host, "pre_write", PAYLOAD) is True
    assert restart.restart_server(host, "post_any", PAYLOAD) is False

    assert spawns.roots == [host]
    assert len(_events(host)) == 1

    later = datetime.now(timezone.utc) + timedelta(seconds=SERVER_RESTART_COOLDOWN_S + 1)
    assert restart.claim_restart(host, "post_any", PAYLOAD, now=later) is True
    assert len(_events(host)) == 2


def test_a_claim_made_during_the_probe_stops_this_one(
    host: Path, run: int, spawns: Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(root: Path) -> None:
        _add_restarts(host, [0])

    monkeypatch.setattr(serve, "answering", probe)

    assert restart.restart_server(host, "pre_write", PAYLOAD) is False

    assert spawns.roots == []
    assert len(_events(host)) == 1


def test_a_spawn_that_raises_still_leaves_the_record(host: Path, run: int, spawns: Spawns) -> None:
    spawns.error = OSError("no python")

    with pytest.raises(OSError):
        restart.restart_server(host, "pre_write", PAYLOAD)

    assert _events(host) == [("main", SERVER_RESTART, "pre_write")]
    spawns.error = None
    assert restart.restart_server(host, "pre_write", PAYLOAD) is False


# -- the launcher entry ------------------------------------------------------------------------


def _run_entry(
    monkeypatch: pytest.MonkeyPatch, host: Path, event: str, raw: str
) -> tuple[io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "argv", ["hooks", event])
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setenv(env.REPO_ROOT_VAR, str(host))
    entry.main()
    return out, err


def test_the_entry_restarts_after_it_has_written_the_answer(
    host: Path, run: int, spawns: Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = restart.restart_server

    def spy(root: Path, event: str, raw: str) -> bool:
        seen.append(sys.stdout.getvalue())  # type: ignore[attr-defined]
        return real(root, event, raw)

    monkeypatch.setattr(restart, "restart_server", spy)
    payload = json.dumps({"session_id": "main", "tool_name": "Write", "tool_input": {}})

    out, _ = _run_entry(monkeypatch, host, "pre_write", payload)

    assert spawns.roots == [host]
    assert seen == [out.getvalue()]
    assert _events(host) == [("main", SERVER_RESTART, "pre_write")]


def _records_write(host: Path) -> str:
    return json.dumps(
        {
            "session_id": "main",
            "tool_name": "Write",
            "tool_input": {"file_path": str(host / ".sentinel-swarm" / "ledger.db")},
        }
    )


def test_a_restart_that_raises_changes_no_answer_and_writes_one_stderr_line(
    host: Path, run: int, spawns: Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _records_write(host)
    expected, _ = run_event("pre_write", payload, host, fallback=True)
    assert "deny" in expected

    def boom(root: Path, event: str, raw: str) -> bool:
        raise RuntimeError("restart broke")

    monkeypatch.setattr(restart, "restart_server", boom)
    out, err = _run_entry(monkeypatch, host, "pre_write", payload)

    assert out.getvalue() == expected
    assert err.getvalue() == "swarm_ledger.hooks pre_write: server restart failed: restart broke\n"
    assert spawns.roots == []


def test_a_crashed_gate_still_denies_when_the_restart_step_raises_too(
    host: Path, run: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(ledger: Ledger, data: dict) -> dict:
        raise RuntimeError("handler broke")

    def boom(root: Path, event: str, raw: str) -> bool:
        raise RuntimeError("restart broke")

    monkeypatch.setitem(hooks.HANDLERS, "pre_write", crash)
    monkeypatch.setattr(restart, "restart_server", boom)

    out, err = _run_entry(monkeypatch, host, "pre_write", PAYLOAD)

    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "cannot check this call: handler broke" in decision["permissionDecisionReason"]
    assert "restart broke" in err.getvalue()


def test_a_denying_gate_denies_the_same_while_the_server_restarts(
    host: Path, run: int, spawns: Spawns, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _records_write(host)
    expected, _ = run_event("pre_write", payload, host, fallback=True)
    assert "deny" in expected

    out, _ = _run_entry(monkeypatch, host, "pre_write", payload)

    assert out.getvalue() == expected
    assert spawns.roots == [host]


def test_run_event_never_restarts_a_server(host: Path, run: int, spawns: Spawns) -> None:
    run_event("post_activity", PAYLOAD, host)
    run_event("post_activity", PAYLOAD, host, fallback=True)

    assert spawns.roots == []
    assert _events(host) == []


# -- the spawn --------------------------------------------------------------------------------


def test_spawn_server_gives_the_server_no_pipe_of_the_hook(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> object:
        captured.update(kwargs, command=command)
        return SimpleNamespace()

    monkeypatch.setattr(serve.subprocess, "Popen", fake_popen)

    serve.spawn_server(host)

    assert captured["command"][-3:] == ["serve", "--repo", str(host)]
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stdout"] == subprocess.DEVNULL
    assert Path(captured["stderr"].name) == host / ".sentinel-swarm" / serve.LOG_FILE
    assert captured["cwd"] == host


def test_ensure_server_spawns_once_and_waits_for_a_current_server(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = {"url": "http://127.0.0.1:1/mcp", "port": 1}
    health = {"version": __version__, "plugin_root": str(serve.PLUGIN_ROOT)}
    answers = iter([None, None, (info, health)])
    spawned: list[Path] = []

    def spawn(root: Path) -> object:
        spawned.append(root)
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(serve, "answering", lambda root: next(answers))
    monkeypatch.setattr(serve, "spawn_server", spawn)
    monkeypatch.setattr(serve, "_POLL_S", 0.0)

    assert serve.ensure_server(host, timeout=5) == info["url"]
    assert spawned == [host.resolve()]
