"""pre_shell for a swarm session the ledger does not know, a subagent gated as its parent
session's agent, and a gating hook that crashes."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from helpers import bootstrap_module, make_host, settings_text

from swarm_ledger import env, hooks, setup
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events, run_event
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path) -> Path:
    settings = settings_text(
        test_command="pytest -q {target}",
        build_command="npm run build",
        lint_command="ruff check",
    )
    return make_host(tmp_path / "host", settings)


def _reason(result: dict | None) -> str:
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _driver(ledger: Ledger, ctx: dict) -> str:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, parent_agent_id, run_id, state) "
            "VALUES ('driver-sess', 'driver-e1', 'driver', ?, ?, 'working')",
            (ctx["oracle_id"], ctx["run_id"]),
        )
        conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, opened_by, focus, agent_id) "
            "VALUES (?, 1, ?, 'everything', 'driver-sess')",
            (ctx["run_id"], ctx["oracle_id"]),
        )
    return "driver-sess"


def _subagent(parent: str, **fields: object) -> dict:
    # What the mod posts for a subagent's call: the subagent loop's id as agent_id, the parent
    # session as session_id, and no agent_type.
    return {"agent_id": "sub-loop-1", "session_id": parent, **fields}


# -- 1. pre_shell and a swarm session that is not a live agent --------------------------------


def test_pre_shell_denies_a_swarm_session_the_ledger_does_not_know(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    data = {
        "session_id": "sess-unknown",
        "agent_type": "swarm-coder",
        "tool_input": {"command": "pytest -q"},
    }

    reason = _reason(events.handle_pre_shell(ledger, data))
    assert "not a live agent of the run" in reason
    assert "runs no shell command" in reason
    assert "ledger_info()" in reason


def test_pre_shell_lets_a_non_swarm_session_pass(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    data = {"session_id": "sess-unknown", "tool_input": {"command": "ls"}}

    assert events.handle_pre_shell(ledger, data) is None
    assert events.handle_pre_shell(ledger, {**data, "agent_type": "general-purpose"}) is None
    assert events.handle_pre_shell(ledger, {**data, "agent_type": "swarm-nonsense"}) is None


def test_pre_shell_changes_nothing_while_no_run_is_live(ledger: Ledger) -> None:
    data = {
        "session_id": "sess-unknown",
        "agent_type": "swarm-coder",
        "tool_input": {"command": "pytest -q"},
    }

    assert events.handle_pre_shell(ledger, data) is None


def test_pre_shell_denies_a_released_swarm_agent(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    ledger.agent_release("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    data = {
        "session_id": ctx["lead"]["agent_id"],
        "agent_type": "swarm-lead",
        "tool_input": {"command": "ls"},
    }

    assert "not a live agent of the run" in _reason(events.handle_pre_shell(ledger, data))


# -- 2. a subagent is gated as its parent session's agent -------------------------------------


def test_a_driver_subagent_runs_only_the_build_command(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    driver = _driver(ledger, ctx)

    assert (
        events.handle_pre_shell(ledger, _subagent(driver, tool_input={"command": "npm run build"}))
        is None
    )
    denied = events.handle_pre_shell(ledger, _subagent(driver, tool_input={"command": "pytest"}))
    assert "npm run build" in _reason(denied)


def test_a_driver_subagent_writes_no_project_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    driver = _driver(ledger, ctx)
    target = str(ledger.repo_root / "src" / "a.py")

    denied = events.handle_pre_write(ledger, _subagent(driver, tool_input={"file_path": target}))
    assert "only a Coder writes project files" in _reason(denied)
    # The same call with no known parent session is not a swarm call and passes.
    assert (
        events.handle_pre_write(
            ledger, _subagent("not-a-swarm-session", tool_input={"file_path": target})
        )
        is None
    )


def test_a_driver_subagent_calls_map_test_before_map_explore(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    driver = _driver(ledger, ctx)

    def skill(name: str) -> dict:
        return _subagent(driver, tool_name="Skill", tool_input={"skill": name})

    assert "map-test" in _reason(events.handle_pre_skill(ledger, skill("cartographer:map-explore")))
    assert events.handle_pre_skill(ledger, skill("map-test")) is None
    assert events.handle_pre_skill(ledger, skill("map-explore")) is None


def test_any_role_subagent_is_gated_as_its_parent(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    lead = ctx["lead"]["agent_id"]
    target = str(ledger.repo_root / "src" / "a.py")

    assert "only a Coder writes" in _reason(
        events.handle_pre_write(ledger, _subagent(lead, tool_input={"file_path": target}))
    )
    assert "no shell" in _reason(
        events.handle_pre_shell(ledger, _subagent(lead, tool_input={"command": "ls"}))
    )


def test_a_known_agent_id_is_gated_as_itself_not_its_session(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    driver = _driver(ledger, ctx)
    lead = ctx["lead"]["agent_id"]

    # The agent_id is the Lead's own: its role, not the Driver's, decides.
    reason = _reason(
        events.handle_pre_shell(
            ledger,
            {"agent_id": lead, "session_id": driver, "tool_input": {"command": "npm run build"}},
        )
    )
    assert "no shell" in reason


def test_pre_ledger_still_stamps_the_subagent_not_its_parent(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    driver = _driver(ledger, ctx)
    data = _subagent(
        driver,
        tool_name="mcp__swarm-ledger__run_status",
        tool_input={"caller": "driver-e1", "agent_id": "forged"},
    )

    answer = events.handle_pre_ledger(ledger, data)
    assert answer["hookSpecificOutput"]["updatedInput"]["agent_id"] == "sub-loop-1"


def test_run_event_does_not_adopt_a_session_for_a_subagent_call(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted: list[object] = []
    monkeypatch.setattr(
        Ledger, "adopt_session", lambda self, session_id, **kwargs: adopted.append(session_id)
    )

    run_event("post_any", json.dumps({"session_id": "main", "tool_name": "Read"}), host)
    run_event(
        "post_any",
        json.dumps({"session_id": "main", "agent_id": "sub", "tool_name": "Read"}),
        host,
    )

    assert adopted == ["main"]


# -- 3. a crashed gate retries once on a locked database, then denies -------------------------


def _locked() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("database is locked")


class _Handler:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.connections: list[sqlite3.Connection] = []

    def __call__(self, ledger: Ledger, data: dict) -> dict | None:
        self.calls += 1
        self.connections.append(ledger.conn)
        if self.errors:
            raise self.errors.pop(0)
        return None


def _closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def _install(monkeypatch: pytest.MonkeyPatch, event: str, handler: _Handler) -> None:
    monkeypatch.setitem(hooks.HANDLERS, event, handler)


def test_a_locked_database_is_retried_once_and_then_answers(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _Handler(_locked())
    _install(monkeypatch, "pre_write", handler)

    assert run_event("pre_write", "{}", host) == ("", "")
    assert handler.calls == 2
    assert all(_closed(conn) for conn in handler.connections)
    assert handler.connections[0] is not handler.connections[1]


@pytest.mark.parametrize("message", ["database table is locked", "database is locked"])
def test_a_second_lock_denies(host: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    handler = _Handler(_locked(), sqlite3.OperationalError(message))
    _install(monkeypatch, "pre_shell", handler)

    stdout, stderr = run_event("pre_shell", "{}", host)

    assert handler.calls == 2
    assert all(_closed(conn) for conn in handler.connections)
    answer = json.loads(stdout)["hookSpecificOutput"]
    assert answer["hookEventName"] == "PreToolUse"
    assert answer["permissionDecision"] == "deny"
    assert answer["permissionDecisionReason"] == (
        f"sentinel-swarm cannot check this call: {message}; run /sentinel-swarm:setup"
    )
    assert stderr == f"swarm_ledger.hooks pre_shell: {message}\n"


def test_any_other_error_denies_without_a_retry(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _Handler(ValueError("boom"))
    _install(monkeypatch, "pre_skill", handler)

    stdout, stderr = run_event("pre_skill", "{}", host)

    assert handler.calls == 1
    assert all(_closed(conn) for conn in handler.connections)
    reason = json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == "sentinel-swarm cannot check this call: boom; run /sentinel-swarm:setup"
    assert stderr == "swarm_ledger.hooks pre_skill: boom\n"


def test_a_non_lock_operational_error_is_not_retried(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _Handler(sqlite3.OperationalError("no such table: agents"))
    _install(monkeypatch, "pre_ledger", handler)

    stdout, _ = run_event("pre_ledger", "{}", host)

    assert handler.calls == 1
    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_gating_event_denies_on_a_crash(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for event in sorted(hooks.GATING_EVENTS):
        _install(monkeypatch, event, _Handler(RuntimeError("x")))
        stdout, stderr = run_event(event, "{}", host)
        assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", event
        assert stderr.startswith(f"swarm_ledger.hooks {event}: "), event


def test_a_gate_that_cannot_parse_its_input_denies(host: Path) -> None:
    stdout, stderr = run_event("pre_agent", "not valid json{", host)

    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert stderr.startswith("swarm_ledger.hooks pre_agent: ")


def test_a_gate_that_cannot_open_the_ledger_denies(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(root: Path | None = None) -> Ledger:
        raise OSError("disk gone")

    monkeypatch.setattr(env, "open_ledger", broken)

    stdout, _ = run_event("pre_write", "{}", host)
    assert "disk gone" in json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "event", ["pre_ask", "post_any", "post_activity", "stop", "session_start", "session_end"]
)
def test_other_events_keep_swallowing_a_crash(
    host: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    assert event not in hooks.GATING_EVENTS
    handler = _Handler(_locked(), _locked())
    _install(monkeypatch, event, handler)

    stdout, stderr = run_event(event, "{}", host)

    assert stdout == ""
    assert stderr.startswith(f"swarm_ledger.hooks {event}: ")
    assert handler.calls == 1
    assert all(_closed(conn) for conn in handler.connections)


def test_the_shim_and_the_hooks_package_name_the_same_gating_events() -> None:
    spec = importlib.util.spec_from_file_location("sentinel_swarm_shim_gating", setup.SHIM_TEMPLATE)
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    assert hooks.GATING_EVENTS == shim.GATING_EVENTS
    assert hooks.GATING_EVENTS <= set(hooks.HANDLERS)
