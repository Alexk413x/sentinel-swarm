from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import make_host, settings_text
from test_memory import _reservations_on
from test_rekey import MovingClaude, claude
from test_sessions import Ctx, _bootstrap, _brief_coder, _row, _spawn_coder, host

from swarm_ledger import lock, memory, procs, sessions, setup, watchdog
from swarm_ledger.clock import utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import run_event
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.review import live_agents
from swarm_ledger.settings import WatchdogSettings, load_settings

__all__ = ["claude", "host"]


def _gone(claude: MovingClaude, agent_id: str) -> None:
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != agent_id]


def _parked_long_ago(ledger: Ledger, agent_id: str, minutes: int = 30) -> None:
    reason = f"the daemon retired its session: idle {minutes}m"
    ledger._park(agent_id, reason, utcnow() - timedelta(minutes=minutes))


# -- issue 12: parked is a state -----------------------------------------------------------------


def test_parking_sets_the_parked_state_and_unparking_restores_the_old_one(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.agent_idle(ctx.manager[1], "stop")
    _gone(claude, ctx.manager[1])

    ledger.mark_parked(claude.listing, utcnow() + timedelta(minutes=5), 120)

    row = _row(ledger, ctx.manager[1])
    assert (row["state"], row["state_before_park"]) == ("parked", "idle")
    assert row["park_reason"] == "its session is not in claude agents --json"
    claude.add(ctx.manager[1], "mgr")
    ledger.mark_parked(claude.listing, utcnow(), 120)
    row = _row(ledger, ctx.manager[1])
    assert (row["state"], row["state_before_park"], row["parked_at"]) == ("idle", None, None)


def test_a_resume_that_rekeys_the_agent_restores_its_state(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.agent_idle(ctx.manager[1], "stop")
    claude.stop_session(ctx.manager[1])
    ledger._park(ctx.manager[1], "retired")

    result = ledger.resume_agent(ledger._row(ctx.manager[1]), "test")

    row = _row(ledger, result["session_id"])
    assert (row["state"], row["parked_at"], row["state_before_park"]) == ("idle", None, None)


def test_a_hook_from_a_parked_session_unparks_it_but_its_stop_does_not(
    ledger: Ledger, claude: MovingClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger._park(ctx.lead[1], "its session is not in claude agents --json")
    payload = {"session_id": ctx.lead[1], "stop_hook_active": True}

    run_event("stop", json.dumps(payload), host)
    assert _row(ledger, ctx.lead[1])["state"] == "parked"

    tool = {"session_id": ctx.lead[1], "tool_name": "Read", "tool_input": {}}
    run_event("post_activity", json.dumps(tool), host)
    assert _row(ledger, ctx.lead[1])["state"] == "working"


def test_a_state_change_while_parked_lands_in_the_state_it_returns_to(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger._park(ctx.lead[1], "retired")
    assert ledger.agent_idle(ctx.lead[1], "stop") is True
    row = _row(ledger, ctx.lead[1])
    assert (row["state"], row["state_before_park"]) == ("parked", "idle")


def test_status_tree_shows_a_parked_agent_with_its_session_closed(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger._park(ctx.lead[1], "the daemon retired its session: idle 6m [low memory]")
    agents = {a["name"]: a for a in live_agents(ledger.conn, ctx.run_id)}
    lead = agents[ctx.lead[0]]
    assert (lead["state"], lead["session"]) == ("parked", "closed")
    assert lead["park_reason"].startswith("the daemon retired")
    assert "session" not in agents[ctx.manager[0]]
    tree = ledger.status_tree(*ctx.oracle)
    assert {a["name"]: a["state"] for a in tree["agents"]}[ctx.lead[0]] == "parked"


def test_a_parked_working_coder_whose_session_is_gone_is_still_reported_crashed(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "c1")
    _gone(claude, coder[1])
    later = utcnow() + timedelta(minutes=5)
    ledger.mark_parked(claude.listing, later, 120)
    assert _row(ledger, coder[1])["state"] == "parked"

    findings = watchdog.scan(ledger.conn, claude.listing, later, WatchdogSettings())

    assert ("crashed", coder[1]) in {(f.kind, f.agent_id) for f in findings}


def test_the_watchdog_releases_a_parked_lead_and_then_its_manager_once_their_work_is_done(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE modules SET state = 'approved'")
        conn.execute("UPDATE phases SET state = 'handed_up'")
    for agent_id in (ctx.lead[1], ctx.manager[1]):
        _gone(claude, agent_id)
        _parked_long_ago(ledger, agent_id)
    now = utcnow()

    first = ledger.release_finished_parked(now)
    second = ledger.release_finished_parked(now)

    assert [a["name"] for a in first] == [ctx.lead[0]]
    assert [a["name"] for a in second] == [ctx.manager[0]]
    assert _row(ledger, ctx.manager[1])["state"] == "released"
    event = ledger.conn.execute(
        "SELECT reason FROM agent_events WHERE agent_id = ? AND to_state = 'released'",
        (ctx.manager[1],),
    ).fetchone()["reason"]
    assert event.startswith("the watchdog released it: parked over 15 minutes")
    assert event.endswith("its phase is handed_up")


def test_a_parked_parent_is_kept_while_recent_unfinished_or_owed_a_wake_up(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _gone(claude, ctx.lead[1])
    _parked_long_ago(ledger, ctx.lead[1], minutes=5)
    assert ledger.release_finished_parked(utcnow()) == []
    _parked_long_ago(ledger, ctx.lead[1], minutes=30)
    assert ledger.release_finished_parked(utcnow()) == []
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE modules SET state = 'approved'")
    ledger.message_post(*ctx.manager, ctx.lead[0], "one more thing")
    assert ledger.release_finished_parked(utcnow()) == []


# -- issue 13: process-tree release ----------------------------------------------------------------


def _table(*rows: tuple[int, int, int, str]) -> dict[int, procs.Proc]:
    return {pid: procs.Proc(pid, ppid, created, name) for pid, ppid, created, name in rows}


SESSION_TABLE = (
    (500, 1, 100, "claude.exe"),
    (501, 500, 110, "python.exe"),
    (502, 501, 120, "python.exe"),
    (503, 500, 130, "bash.exe"),
    (504, 503, 140, "python.exe"),
    (505, 504, 150, "conhost.exe"),
    (506, 500, 90, "bash.exe"),
)


def test_the_tool_tree_holds_the_shells_and_what_they_started_but_not_the_mcp_servers() -> None:
    tree = procs.tool_tree(_table(*SESSION_TABLE), 500, 100)
    assert sorted(p.pid for p in tree) == [503, 504, 505]


def test_survivors_follow_new_children_and_skip_reused_pids_and_protected_ones() -> None:
    table = _table(
        (503, 500, 130, "bash.exe"), (504, 1, 999, "notepad.exe"), (507, 503, 160, "pytest")
    )
    left = procs.survivors(table, [(503, 130), (504, 140)], protected=set())
    assert sorted(p.pid for p in left) == [503, 507]
    assert procs.survivors(table, [(503, 130)], protected={507}) == [table[503]]


def test_ps_output_parses_into_the_process_table() -> None:
    table = procs.parse_ps(
        "  12     1 Thu Oct 30 10:00:00 2026 /bin/bash\n"
        "  13    12 Thu Oct 30 10:00:05 2026 python3 -m pytest\n"
        "garbage\n"
    )
    assert table[13].ppid == 12
    assert table[13].name == "python3 -m pytest"
    assert (table[12].created or 0) + 5 == table[13].created
    assert procs.is_shell(table[12])


def test_the_process_table_reads_this_process_on_this_machine() -> None:
    table = procs._windows_snapshot() if os.name == "nt" else procs._posix_snapshot()
    assert os.getpid() in table


def _release_setup(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> tuple[Ctx, tuple[str, str], list[int]]:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx, "c1")
    table = _table(*SESSION_TABLE)
    killed: list[int] = []
    monkeypatch.setattr(memory, "session_pids", lambda: {coder[1]: 500})
    monkeypatch.setattr(procs, "snapshot", lambda: dict(table))

    def terminate(proc: procs.Proc) -> bool:
        killed.append(proc.pid)
        return True

    monkeypatch.setattr(procs, "terminate", terminate)
    return ctx, coder, killed


def test_agent_release_stops_what_the_sessions_tools_left_running(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, coder, killed = _release_setup(ledger, claude, monkeypatch)

    ledger.agent_release(*ctx.lead, coder[1])

    assert sorted(killed) == [503, 504, 505]
    recorded = ledger.conn.execute(
        "SELECT pid, is_session FROM released_processes WHERE agent_id = ? ORDER BY pid",
        (coder[1],),
    ).fetchall()
    assert [tuple(r) for r in recorded] == [(500, 1), (503, 0), (504, 0), (505, 0)]
    reason = ledger.conn.execute(
        "SELECT reason FROM agent_events WHERE agent_id = ? ORDER BY event_id DESC LIMIT 1",
        (coder[1],),
    ).fetchone()["reason"]
    assert "3 process(es) its tools left running stopped" in reason


def test_a_release_after_the_session_died_uses_the_pid_the_watchdog_saw(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, coder, killed = _release_setup(ledger, claude, monkeypatch)
    entry = claude.entry(coder[1])
    assert entry is not None
    entry.pop("pid")
    entry["kind"] = "background"
    monkeypatch.setattr(procs, "created_of", lambda pid: 100)
    ledger.sample_memory(claude.listing)
    row = _row(ledger, coder[1])
    assert (row["session_pid"], row["session_pid_created"]) == (500, 100)
    without_session = {k: v for k, v in _table(*SESSION_TABLE).items() if k != 500}
    monkeypatch.setattr(procs, "snapshot", lambda: dict(without_session))
    monkeypatch.setattr(memory, "session_pids", dict)

    ledger.agent_release(*ctx.lead, coder[1])

    assert sorted(killed) == [503, 504, 505]


def test_the_watchdog_reports_a_process_a_released_session_left_running(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, coder, _ = _release_setup(ledger, claude, monkeypatch)
    ledger.agent_release(*ctx.lead, coder[1])
    later = utcnow() + timedelta(minutes=2)

    findings = watchdog.scan(ledger.conn, claude.listing, later, WatchdogSettings())

    stray = [f for f in findings if f.kind == "stray_process"]
    assert [f.agent_id for f in stray] == [coder[1]]
    assert "python.exe (pid 504)" in stray[0].detail
    assert "pid 501" not in stray[0].detail
    monkeypatch.setattr(procs, "snapshot", dict)
    assert not [
        f
        for f in watchdog.scan(ledger.conn, claude.listing, later, WatchdogSettings())
        if f.kind == "stray_process"
    ]


# -- issue 4: machine-wide admission ---------------------------------------------------------------


def test_a_session_still_starting_counts_against_the_next_admission(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    first = _brief_coder(ledger, ctx, "c1")
    second = _brief_coder(ledger, ctx, "c2")
    _reservations_on(monkeypatch)
    monkeypatch.setattr(memory, "free_mb", lambda: 1_500 + 600 + 100)

    assert "queued" not in ledger.agent_spawn(*ctx.lead, first)
    queued = ledger.agent_spawn(*ctx.lead, second)

    assert queued["queued"] is True
    assert "for sessions still starting on this machine" in queued["reason"]
    assert ledger.run_status(*ctx.lead)["capacity"]["reserved_mb"] >= 600


def test_another_swarms_reservation_counts_and_a_dead_servers_does_not(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    monkeypatch.setattr(memory, "free_mb", lambda: 1_500 + 600 + 100)
    _reservations_on(monkeypatch)
    lock.reserve(2**31 - 7, tmp_path, 5_000)
    assert lock.reserved_mb() == 0
    lock.reserve(os.getppid(), tmp_path, 300)
    assert lock.reserved_mb() == 300

    assert ledger.agent_spawn(*ctx.lead, name)["queued"] is True


def test_run_start_and_capacity_list_the_other_live_swarms(
    ledger: Ledger, claude: MovingClaude, tmp_path: Path
) -> None:
    other = make_host(tmp_path / "other", settings_text())
    lock.acquire(other, 7, os.getppid())
    started_dead = make_host(tmp_path / "dead", settings_text())
    lock.acquire(started_dead, 8, 2**31 - 7)

    ctx = _bootstrap(ledger, claude)

    swarms = ledger.run_status(*ctx.oracle)["capacity"]["other_swarms"]
    assert [(s["run_id"], s["server_pid"]) for s in swarms] == [(7, os.getppid())]
    again = ledger.run_start(prd="x", session_id=ctx.oracle[1])
    assert [s["run_id"] for s in again["other_swarms"]] == [7]


def test_parallelism_cap_defaults_from_total_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "total_mb", lambda: 32_000)
    root = make_host(tmp_path / "h", settings_text())
    assert load_settings(root).parallelism_cap == 26
    assert memory.default_parallelism_cap(4_000) == 4
    assert memory.default_parallelism_cap(None) is None
    for raw, cap in (("5", 5), ("0", None)):
        line = "\nparallelism_cap:\n"
        text = settings_text(swaps=[(line, line.replace(":", f": {raw}"))])
        assert load_settings(make_host(tmp_path / f"h{raw}", text)).parallelism_cap == cap


def test_total_memory_is_read_on_this_machine() -> None:
    total = memory.platform_total_mb()
    assert total is not None and total > 0


def test_setup_says_the_cap_counts_other_swarms_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "total_mb", lambda: 32_000)
    report = setup.SetupReport()
    setup.report_parallelism(make_host(tmp_path / "h", settings_text()), report)
    assert report.lines == [
        "parallelism_cap is 26 (the default for 32000 MB of memory). It counts this run's live "
        "sessions plus every other swarm's running sessions on this machine. Set "
        "parallelism_cap in .claude/sentinel-swarm.local.md to change it; 0 turns it off"
    ]


# -- issue 5: an unreadable ~/.claude.json ---------------------------------------------------------

PARSE_ERROR = "claude --bg failed with exit code 1: JSON Parse error: Unexpected EOF"


def _claude_json(text: str) -> Path:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    path.write_text(text, encoding="utf-8")
    return path


def _backup(name: str, text: str, age: int) -> Path:
    folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    at = utcnow().timestamp() - age
    os.utime(path, (at, at))
    return path


def test_a_command_that_fails_on_the_config_file_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def flaky(args: list[str], cwd: Path | None = None) -> str:
        calls.append(args)
        if len(calls) < 3:
            raise LedgerError(PARSE_ERROR)
        return "ok"

    monkeypatch.setattr(sessions, "_run", flaky)
    waits: list[float] = []
    monkeypatch.setattr(sessions, "_sleep", waits.append)
    assert sessions._run_retrying(["stop", "abc"]) == "ok"
    assert waits == [1.0, 2.0]


def test_the_retries_are_bounded_and_other_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def failing(args: list[str], cwd: Path | None = None) -> str:
        calls.append(args[0])
        raise LedgerError(args[0])

    monkeypatch.setattr(sessions, "_run", failing)
    with pytest.raises(sessions.ConfigParseError, match="tried 5 times over 15s"):
        sessions._run_retrying(["JSON Parse error: Unexpected EOF"])
    assert len(calls) == 5
    with pytest.raises(LedgerError) as caught:
        sessions._run_retrying(["exit code 2"])
    assert not isinstance(caught.value, sessions.ConfigParseError)
    assert len(calls) == 6


def test_the_newest_backup_that_parses_is_named(tmp_path: Path) -> None:
    _backup(".claude.json.backup.1", "{}", age=300)
    newest = _backup(".claude.json.backup.2", '{"a": 1}', age=100)
    _backup(".claude.json.backup.3", "{truncated", age=50)
    _backup(".claude.json.corrupted.4", "{}", age=10)
    assert sessions.newest_parsing_backup() == newest
    reason = sessions.config_pause_reason("JSON Parse error")
    assert reason.startswith(sessions.CONFIG_PAUSE)
    assert str(newest) in reason


def test_a_spawn_that_still_fails_on_the_config_file_pauses_the_run(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    backup = _backup(".claude.json.backup.1", "{}", age=60)
    _claude_json("{trunc")

    def broken(args: list[str], cwd: Path | None = None) -> str:
        if args[:2] == ["agents", "--json"]:
            return json.dumps(claude.listing)
        raise LedgerError(PARSE_ERROR)

    monkeypatch.setattr(sessions, "_run", broken)
    with pytest.raises(sessions.ConfigParseError) as caught:
        ledger.agent_spawn(*ctx.lead, name)

    assert str(backup) in str(caught.value)
    assert "The ledger paused the run" in str(caught.value)
    assert "resumes by itself" in str(caught.value)
    run = ledger.conn.execute("SELECT state, pause_check FROM runs").fetchone()
    assert (run["state"], run["pause_check"]) == ("paused", "config_parses")
    assert (ledger.pause_reason(ctx.run_id) or "").startswith(sessions.CONFIG_PAUSE)
    notice = ledger.conn.execute("SELECT message FROM notifications").fetchone()
    assert notice["message"].startswith(f"Run paused: {sessions.CONFIG_PAUSE}")


def test_a_failure_after_the_config_file_parses_again_does_not_pause(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    _claude_json("{}")

    def broken(args: list[str], cwd: Path | None = None) -> str:
        if args[:2] == ["agents", "--json"]:
            return json.dumps(claude.listing)
        raise LedgerError(PARSE_ERROR)

    monkeypatch.setattr(sessions, "_run", broken)
    with pytest.raises(sessions.ConfigParseError):
        ledger.agent_spawn(*ctx.lead, name)
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()["state"] == "active"


def test_a_queued_spawn_whose_start_fails_on_the_config_file_stays_queued(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    name = _brief_coder(ledger, ctx, "c1")
    monkeypatch.setattr(memory, "free_mb", lambda: 1_000)
    ledger.agent_spawn(*ctx.lead, name)
    monkeypatch.setattr(memory, "free_mb", lambda: 64_000)
    _claude_json("{}")

    def broken(args: list[str], cwd: Path | None = None) -> str:
        if args[:2] == ["agents", "--json"]:
            return json.dumps(claude.listing)
        raise LedgerError(PARSE_ERROR)

    monkeypatch.setattr(sessions, "_run", broken)
    assert ledger.start_queued_spawns(claude.listing) == []
    assert ledger.conn.execute("SELECT state FROM spawn_queue").fetchone()["state"] == "queued"
