from __future__ import annotations

import io
import json
import socket
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from swarm_ledger import serve, sessions, watch, watchdog
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.hooks.__main__ import _HANDLERS
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import WatchdogSettings, load_settings

ORACLE = "sess-oracle"
SETTINGS = WatchdogSettings()


class FakeClaude:
    def __init__(self) -> None:
        self.listing: list[dict] = []
        self.resumed: list[tuple[str, str]] = []
        self.fail_list = False
        self.fail_resume = False

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        del cwd
        if args[:2] == ["agents", "--json"]:
            if self.fail_list:
                raise LedgerError("claude agents --json failed")
            return json.dumps(self.listing)
        if args[0] == "--resume":
            if self.fail_resume:
                raise LedgerError("claude --resume failed")
            self.resumed.append((args[1], args[3]))
            return f"backgrounded · {args[1][:8]} · resumed\n"
        raise AssertionError(f"unexpected claude call: {args}")

    def run(self, session_id: str, name: str = "", **extra: object) -> dict:
        entry: dict = {"pid": 10 + len(self.listing), "sessionId": session_id, "name": name}
        entry |= {"status": "busy", "state": "working"} | extra
        self.listing.append(entry)
        return entry


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(sessions, "_run", fake)
    return fake


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    return root


@pytest.fixture
def ledger(host: Path, claude: FakeClaude) -> Ledger:
    del claude
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


@pytest.fixture
def now() -> datetime:
    return watchdog.utcnow()


def _run_id(ledger: Ledger) -> int:
    return ledger.run_start(prd="Build X", session_id=ORACLE)["run"]["run_id"]


def _agent(
    ledger: Ledger,
    agent_id: str,
    role: str,
    state: str,
    *,
    started: datetime,
    heartbeat: datetime | None = None,
    parent: str = ORACLE,
    transcript: Path | None = None,
    model: str | None = None,
) -> None:
    with write_tx(ledger.conn) as conn:
        run_id = conn.execute("SELECT run_id FROM runs ORDER BY run_id DESC").fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, model, parent_agent_id, run_id, state, "
            "session_name, started_at, last_heartbeat_at, transcript_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                agent_id.removeprefix("sess-"),
                role,
                model,
                parent,
                run_id,
                state,
                f"host-r1-{agent_id.removeprefix('sess-')}",
                watchdog.stamp(started),
                watchdog.stamp(heartbeat) if heartbeat else None,
                str(transcript) if transcript else None,
            ),
        )


def _kinds(findings: list[watchdog.Finding]) -> list[tuple[str, str]]:
    return [(f.name, f.kind) for f in findings]


def _scan(ledger: Ledger, claude: FakeClaude, now: datetime, **settings: int):
    return watchdog.scan(ledger.conn, claude.listing, now, WatchdogSettings(**settings))


def _oracle_running(claude: FakeClaude) -> None:
    claude.run(ORACLE, "host-oracle")


def test_a_working_agent_whose_session_is_gone_crashed(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now)
    claude.run("sess-lead-1", status="stopped")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("coder-1", "crashed"), ("lead-1", "crashed")]
    assert "is not in claude agents --json" in findings[0].detail
    assert "is not running (status stopped" in findings[1].detail
    assert findings[0].next_step == 'Resume it with agent_resume(target_name="coder-1").'


def test_a_registered_agent_crashed_only_after_the_grace(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-new", "coder", "registered", started=now - timedelta(seconds=90))
    _agent(ledger, "sess-old", "coder", "registered", started=now - timedelta(minutes=3))

    assert _kinds(_scan(ledger, claude, now)) == [("old", "crashed")]


def test_an_idle_or_handed_up_agent_whose_session_exited_is_normal(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-idle", "lead", "idle", started=now - timedelta(hours=1))
    _agent(ledger, "sess-done", "coder", "handed_up", started=now - timedelta(hours=1))

    assert _scan(ledger, claude, now) == []


def test_a_paused_run_is_not_scanned(ledger: Ledger, claude: FakeClaude, now: datetime) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    ledger.run_pause("oracle", ORACLE, "waiting on the user")

    assert _scan(ledger, claude, now) == []


def test_a_running_agent_with_no_activity_is_stuck(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=20)
    _agent(ledger, "sess-quiet", "coder", "working", started=old, heartbeat=old)
    _agent(ledger, "sess-silent", "coder", "working", started=old)
    _agent(ledger, "sess-busy", "coder", "working", started=old, heartbeat=now)
    for agent_id in ("sess-quiet", "sess-silent", "sess-busy"):
        claude.run(agent_id)

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("quiet", "stuck"), ("silent", "stuck")]
    assert "no activity for 20 minutes" in findings[0].detail
    assert 'SendMessage(to="host-r1-quiet")' in findings[0].next_step
    assert "agent_resume refuses a running session" in findings[0].next_step
    assert _kinds(_scan(ledger, claude, now, stuck_minutes=30)) == []


def test_a_session_waiting_on_a_permission_prompt_is_reported_once(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, status="waiting", waitingFor="permission prompt")
    old = now - timedelta(minutes=30)
    _agent(ledger, "sess-asking", "coder", "working", started=old, heartbeat=old)
    claude.run("sess-asking", status="waiting", waitingFor="permission prompt")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("asking", "waiting_permission")]
    assert "open the session host-r1-asking in agent view" in findings[0].next_step


def _test_runs(ledger: Ledger, agent_id: str, target: str, results: list[bool]) -> None:
    with write_tx(ledger.conn) as conn:
        run_id = conn.execute("SELECT run_id FROM runs").fetchone()[0]
        for passed in results:
            conn.execute(
                "INSERT INTO test_runs (run_id, agent_id, scope, target, exit_code, passed, "
                "failed) VALUES (?, ?, 'file', ?, ?, ?, ?)",
                (run_id, agent_id, target, 0 if passed else 1, 3, 0 if passed else 1),
            )


def test_the_same_test_failing_five_times_in_a_row_is_spinning(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    for agent_id in ("sess-lead-1", "sess-spin", "sess-four", "sess-fixed", "sess-mixed"):
        role = "lead" if agent_id == "sess-lead-1" else "coder"
        parent = ORACLE if role == "lead" else "sess-lead-1"
        _agent(ledger, agent_id, role, "working", started=now, heartbeat=now, parent=parent)
        claude.run(agent_id)
    _test_runs(ledger, "sess-spin", "a.py", [True] + [False] * 5)
    _test_runs(ledger, "sess-four", "b.py", [False] * 4)
    _test_runs(ledger, "sess-fixed", "c.py", [False] * 5 + [True])
    _test_runs(ledger, "sess-mixed", "d.py", [False] * 3)
    _test_runs(ledger, "sess-mixed", "e.py", [False] * 2)

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("spin", "spinning")]
    assert "the last 5 test runs for scope file and target a.py failed" in findings[0].detail
    assert findings[0].next_step.startswith("Ask lead-1 to review the work")
    assert "issue_escalate" in findings[0].next_step


def _transcript(path: Path, tokens: int, model: str = "claude-sonnet-5") -> Path:
    records = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": model,
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": tokens - 110,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 999_999,
                },
            },
        },
        {"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 1}}},
        {"type": "user", "message": {"role": "user", "content": "tool result"}},
    ]
    early = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 5}}}
    lines = [json.dumps(early)] * 5000 + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_an_agent_near_its_context_window_is_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    full = _transcript(tmp_path / "full.jsonl", 170_000, model="claude-haiku-4-5")
    roomy = _transcript(tmp_path / "roomy.jsonl", 170_000)
    _agent(ledger, "sess-full", "coder", "idle", started=now, transcript=full)
    _agent(ledger, "sess-1m", "coder", "idle", started=now, transcript=roomy, model="sonnet")
    _agent(ledger, "sess-none", "coder", "idle", started=now, transcript=tmp_path / "gone.jsonl")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = ?", (str(full), ORACLE)
        )

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("full", "context_high"), ("oracle", "context_high")]
    assert "used 170000 of 200000 context tokens (85%)" in findings[0].detail
    assert findings[0].next_step.startswith("Have oracle release it and brief a fresh agent")
    assert "call run_pause" in findings[1].next_step
    assert _scan(ledger, claude, now, context_pct=90) == []
    overridden = _scan(ledger, claude, now, context_window=200_000)
    assert ("1m", "context_high") in _kinds(overridden)


def test_the_latest_usage_is_found_across_read_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog, "_TAIL_BLOCK", 64)
    path = tmp_path / "t.jsonl"
    usage = {"role": "assistant", "model": "m[1m]", "usage": {"input_tokens": 1234}}
    lines = [json.dumps({"message": {"role": "assistant", "usage": {"input_tokens": 1}}})]
    lines.append(json.dumps({"message": usage, "padding": "x" * 300}))
    lines += [json.dumps({"message": {"role": "user", "content": "y" * 40}})] * 20
    path.write_text("\n".join(lines), encoding="utf-8")

    assert watchdog._last_usage(path) == (1234, "m[1m]")
    path.write_text("", encoding="utf-8")
    assert watchdog._last_usage(path) is None


def test_session_start_and_post_any_record_the_transcript_path(ledger: Ledger, now) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)

    events.handle_session_start(
        ledger, {"session_id": "sess-coder-1", "transcript_path": "/t/one.jsonl"}
    )
    assert ledger._agent_dict("sess-coder-1")["transcript_path"] == "/t/one.jsonl"

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET transcript_path = NULL")
    events.handle_post_any(
        ledger,
        {"session_id": "sess-coder-1", "tool_name": "Read", "transcript_path": "/t/two.jsonl"},
    )
    assert ledger._agent_dict("sess-coder-1")["transcript_path"] == "/t/two.jsonl"


def test_a_run_with_no_running_session_and_pending_work_is_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)
    ledger.message_post("oracle", ORACLE, "lead-1", "Module 1 changed.")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("oracle", "stalled")]
    assert "work is pending: 1 live agent(s)" in findings[0].detail
    assert 'agent_resume(target_name="lead-1")' in findings[0].next_step

    claude.run("sess-lead-1", status="idle")
    assert _scan(ledger, claude, now) == []


def test_an_empty_stalled_run_still_needs_the_oracle(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    ledger.directive_submit("watchdog", "watchdog", "An earlier report.")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("oracle", "stalled")]
    assert findings[0].detail.endswith("and the run is not finished")
    assert findings[0].next_step == "Continue the plan, or call run_finish."


def _directives(ledger: Ledger) -> list[dict]:
    return [dict(r) for r in ledger.conn.execute("SELECT * FROM directives ORDER BY directive_id")]


def test_a_finding_is_reported_once_cleared_when_gone_and_reported_again(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)

    first = watchdog.record(ledger.conn, run_id, _scan(ledger, claude, now), now)
    later = now + timedelta(seconds=30)
    again = watchdog.record(ledger.conn, run_id, _scan(ledger, claude, later), later)

    assert [r["kind"] for r in first] == ["crashed"]
    assert again == []
    assert len(_directives(ledger)) == 1
    row = ledger.conn.execute("SELECT * FROM watchdog_findings").fetchone()
    assert (row["first_seen_at"], row["last_seen_at"]) == (
        watchdog.stamp(now),
        watchdog.stamp(later),
    )
    assert row["directive_id"] == _directives(ledger)[0]["directive_id"]

    claude.run("sess-coder-1")
    watchdog.record(ledger.conn, run_id, _scan(ledger, claude, later), later)
    assert ledger.conn.execute("SELECT cleared_at FROM watchdog_findings").fetchone()[0]

    claude.listing.pop()
    back = watchdog.record(ledger.conn, run_id, _scan(ledger, claude, later), later)
    assert [r["kind"] for r in back] == ["crashed"]
    assert len(_directives(ledger)) == 2
    assert ledger.conn.execute("SELECT COUNT(*) FROM watchdog_findings").fetchone()[0] == 2


def test_the_directive_names_the_agent_session_kind_detail_and_next_step(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)

    watchdog.record(ledger.conn, run_id, _scan(ledger, claude, now), now)

    directive = _directives(ledger)[0]
    assert (directive["source"], directive["sender_name"]) == ("watchdog", "watchdog")
    assert directive["state"] == "open"
    assert directive["body"] == (
        "Watchdog finding crashed: coder-1 (session host-r1-coder-1): the ledger says "
        "working, but its session is not in claude agents --json. Next: Resume it with "
        'agent_resume(target_name="coder-1"). Then resolve this directive with '
        "directive_resolve."
    )
    with pytest.raises(LedgerError, match="1 directive"):
        ledger.run_finish("oracle", ORACLE, "success")


def test_a_stall_is_reported_only_when_two_passes_see_it(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    run_id = _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)

    assert watchdog.record(ledger.conn, run_id, _scan(ledger, claude, now), now) == []
    assert _directives(ledger) == []
    reported = watchdog.record(ledger.conn, run_id, _scan(ledger, claude, now), now)
    assert [r["kind"] for r in reported] == ["stalled"]
    assert "Watchdog finding stalled: oracle" in _directives(ledger)[0]["body"]


class Exits:
    def __init__(self) -> None:
        self.count = 0
        self.activity = 0

    def __call__(self) -> None:
        self.count += 1


def _dog(ledger: Ledger, exits: Exits, **settings: int) -> watchdog.Watchdog:
    return watchdog.Watchdog(
        ledger.repo_root,
        ledger.conn,
        WatchdogSettings(**settings),
        exit_server=exits,
        activity=lambda: exits.activity,
        log=lambda message: None,
    )


def test_a_tick_reports_findings_and_skips_a_paused_run(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    ledger.run_pause("oracle", ORACLE, "waiting on the user")
    dog = _dog(ledger, Exits())

    dog.tick(now)
    assert _directives(ledger) == []

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET state = 'active'")
    dog.tick(now)
    assert len(_directives(ledger)) == 1


def test_a_failed_session_listing_skips_the_pass(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.fail_list = True

    _dog(ledger, Exits()).tick(now)

    assert _directives(ledger) == []


def _wakes(ledger: Ledger) -> list[str]:
    return [
        r["reason"]
        for r in ledger.conn.execute(
            "SELECT reason FROM agent_events WHERE to_state = 'watchdog_wake' ORDER BY event_id"
        )
    ]


def test_the_watchdog_wakes_a_stopped_oracle_at_most_every_five_minutes_then_pauses(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    dog = _dog(ledger, Exits())

    dog.tick(now)
    assert claude.resumed == []
    dog.tick(now + timedelta(seconds=30))
    assert claude.resumed == [(ORACLE, "The watchdog reported 1 finding(s). Read directive_inbox.")]
    dog.tick(now + timedelta(minutes=2))
    assert len(claude.resumed) == 1
    dog.tick(now + timedelta(minutes=6))
    dog.tick(now + timedelta(minutes=12))
    assert len(claude.resumed) == 3
    assert len(_wakes(ledger)) == 3

    dog.tick(now + timedelta(minutes=15))
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "active"
    dog.tick(now + timedelta(minutes=18))
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"
    assert ledger.pause_reason(1) == "the watchdog could not wake the Oracle"
    assert len(claude.resumed) == 3


def test_oracle_activity_resets_the_wake_attempts(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    dog = _dog(ledger, Exits())
    dog.tick(now)
    for minutes in (1, 6, 12):
        dog.tick(now + timedelta(minutes=minutes))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET last_heartbeat_at = ? WHERE agent_id = ?",
            (watchdog.stamp(now + timedelta(minutes=13)), ORACLE),
        )

    dog.tick(now + timedelta(minutes=18))

    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "active"
    assert len(claude.resumed) == 4


def test_a_failed_wake_counts_as_an_attempt(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.fail_resume = True
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))

    assert _wakes(ledger) == ["the watchdog could not resume the Oracle: claude --resume failed"]


def test_no_wake_while_the_oracle_runs_or_after_the_listener_notified_it(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    oracle = claude.run(ORACLE, status="idle")
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))
    assert claude.resumed == []

    watch.poll(ledger.conn, now + timedelta(minutes=1))
    oracle["status"] = "stopped"
    dog.tick(now + timedelta(minutes=2))
    assert claude.resumed == []


def test_the_server_exits_after_the_idle_window_with_no_active_run(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    exits = Exits()
    dog = _dog(ledger, exits, idle_exit_minutes=15)

    dog.tick(now)
    dog.tick(now + timedelta(minutes=14))
    assert exits.count == 0
    exits.activity += 1
    dog.tick(now + timedelta(minutes=16))
    assert exits.count == 0
    dog.tick(now + timedelta(minutes=17))
    dog.tick(now + timedelta(minutes=31))
    assert exits.count == 0
    dog.tick(now + timedelta(minutes=32))
    assert exits.count == 1


def test_a_paused_run_keeps_the_server_while_one_of_its_sessions_runs(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    ledger.run_pause("oracle", ORACLE, "waiting on the user")
    oracle = claude.run(ORACLE, status="idle")
    exits = Exits()
    dog = _dog(ledger, exits)

    for minutes in (0, 20, 40):
        dog.tick(now + timedelta(minutes=minutes))
    assert exits.count == 0

    oracle["status"] = "stopped"
    dog.tick(now + timedelta(minutes=41))
    dog.tick(now + timedelta(minutes=56))
    assert exits.count == 1


def test_an_active_run_never_idles_the_server_out(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    exits = Exits()
    dog = _dog(ledger, exits)
    for minutes in (0, 20, 40, 60):
        dog.tick(now + timedelta(minutes=minutes))
    assert exits.count == 0


def test_run_forever_logs_a_failing_pass_and_keeps_going(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[str] = []
    dog = watchdog.Watchdog(
        ledger.repo_root,
        ledger.conn,
        WatchdogSettings(interval_seconds=0),
        exit_server=lambda: None,
        log=logged.append,
    )
    calls = []

    class Stop(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            return len(calls) >= 2

    def boom(moment: datetime) -> None:
        calls.append(moment)
        raise RuntimeError("broken")

    monkeypatch.setattr(dog, "tick", boom)
    dog.run_forever(Stop())

    assert len(calls) == 2
    assert logged == ["a pass failed: RuntimeError: broken"] * 2


def test_the_server_binds_its_saved_port_and_falls_back_when_taken(host: Path) -> None:
    first = serve.bind_socket(host)
    port = first.getsockname()[1]
    first.close()
    assert serve.port_path(host).read_text(encoding="utf-8") == f"{port}\n"

    again = serve.bind_socket(host)
    assert again.getsockname()[1] == port
    again.listen()
    try:
        moved = serve.bind_socket(host)
        try:
            assert moved.getsockname()[1] != port
            assert serve.port_path(host).read_text(encoding="utf-8").strip() == str(
                moved.getsockname()[1]
            )
        finally:
            moved.close()
    finally:
        again.close()


def test_a_malformed_port_file_binds_a_free_port(host: Path) -> None:
    path = serve.port_path(host)
    path.parent.mkdir(parents=True)
    path.write_text("not a port", encoding="utf-8")
    sock = serve.bind_socket(host)
    try:
        assert isinstance(sock, socket.socket)
        assert path.read_text(encoding="utf-8").strip() == str(sock.getsockname()[1])
    finally:
        sock.close()


def test_the_listener_prints_each_watchdog_directive_once_and_beats(
    ledger: Ledger, now: datetime
) -> None:
    _run_id(ledger)
    ledger.directive_submit("watchdog", "watchdog", "Watchdog finding stuck:\nline two")
    ledger.directive_submit("user-chat", "alex", "Not for the listener.")

    keep, lines = watch.poll(ledger.conn, now)
    assert keep is True
    assert lines == ["Watchdog directive 1: Watchdog finding stuck: line two"]
    assert ledger.conn.execute("SELECT watch_heartbeat_at FROM runs").fetchone()[0] == (
        watchdog.stamp(now)
    )

    later = now + timedelta(seconds=2)
    assert watch.poll(ledger.conn, later) == (True, [])
    assert ledger.conn.execute("SELECT watch_heartbeat_at FROM runs").fetchone()[0] == (
        watchdog.stamp(later)
    )


@pytest.mark.parametrize("end", ["paused", "finished", "none"])
def test_the_listener_exits_when_the_run_is_not_active(ledger: Ledger, now, end: str) -> None:
    if end != "none":
        _run_id(ledger)
        with write_tx(ledger.conn) as conn:
            conn.execute("UPDATE runs SET state = ?", (end,))
    out = io.StringIO()

    assert watch.watch(ledger.conn, out, sleep=lambda s: None, clock=lambda: now) == 0
    assert out.getvalue() == ""


def test_the_listener_loop_prints_new_directives_then_exits_on_pause(
    ledger: Ledger, now: datetime
) -> None:
    _run_id(ledger)
    out = io.StringIO()
    naps: list[float] = []

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 1:
            ledger.directive_submit("watchdog", "watchdog", "First.")
        elif len(naps) == 2:
            ledger.run_pause("oracle", ORACLE, "waiting on the user")

    assert watch.watch(ledger.conn, out, sleep=sleep, clock=lambda: now) == 0
    assert out.getvalue() == "Watchdog directive 1: First.\n"
    assert naps == [2.0, 2.0]


def test_a_newer_listener_retires_the_older_one(ledger: Ledger, now: datetime) -> None:
    _run_id(ledger)
    watch.claim(ledger.conn, "old")
    ledger.directive_submit("watchdog", "watchdog", "For the new listener.")
    watch.claim(ledger.conn, "new")

    assert watch.poll(ledger.conn, now, "old") == (False, [])
    keep, lines = watch.poll(ledger.conn, now, "new")
    assert keep
    assert lines == ["Watchdog directive 1: For the new listener."]


def test_the_listener_loop_exits_once_another_listener_claims_the_run(
    ledger: Ledger, now: datetime
) -> None:
    _run_id(ledger)
    out = io.StringIO()

    def sleep(seconds: float) -> None:
        watch.claim(ledger.conn, "newer")

    assert watch.watch(ledger.conn, out, sleep=sleep, clock=lambda: now, owner="older") == 0
    assert out.getvalue() == ""


def test_directive_inbox_marks_directives_notified(ledger: Ledger) -> None:
    _run_id(ledger)
    ledger.directive_submit("watchdog", "watchdog", "Seen in the inbox.")

    inbox = ledger.directive_inbox("oracle", ORACLE)

    assert inbox[0]["notified_at"] is not None
    assert watch.poll(ledger.conn, watchdog.utcnow()) == (True, [])


def _monitor(agent: str | None, command: str, **extra: object) -> dict:
    data: dict = {"tool_name": "Monitor", "tool_input": {"command": command} | extra}
    if agent is not None:
        data["session_id"] = agent
    return data


def test_pre_monitor_allows_only_the_oracles_watch_command(ledger: Ledger, now) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-mgr", "manager", "working", started=now)
    spaced = "python3  .sentinel-swarm/hook.py watch ||\n python .sentinel-swarm/hook.py watch"

    assert events.handle_pre_monitor(ledger, _monitor(ORACLE, watchdog.WATCH_COMMAND)) is None
    assert events.handle_pre_monitor(ledger, _monitor(ORACLE, spaced)) is None

    for data in (
        _monitor(ORACLE, "tail -f build.log"),
        _monitor(ORACLE, watchdog.WATCH_COMMAND, ws="ws://127.0.0.1:1"),
        _monitor("sess-mgr", watchdog.WATCH_COMMAND),
    ):
        denied = events.handle_pre_monitor(ledger, data)
        assert denied is not None
        output = denied["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert watchdog.MONITOR_CALL in output["permissionDecisionReason"]


def test_pre_monitor_passes_a_non_swarm_caller_and_gates_by_agent_type(ledger: Ledger) -> None:
    assert events.handle_pre_monitor(ledger, _monitor("someone", "tail -f x")) is None

    before_run_start = _monitor("new-oracle", "tail -f x") | {"agent_type": "swarm-oracle"}
    assert events.handle_pre_monitor(ledger, before_run_start) is not None
    exact = _monitor("new-oracle", watchdog.WATCH_COMMAND) | {"agent_type": "swarm-oracle"}
    assert events.handle_pre_monitor(ledger, exact) is None
    coder = _monitor("new-coder", watchdog.WATCH_COMMAND) | {"agent_type": "swarm-coder"}
    assert events.handle_pre_monitor(ledger, coder) is not None


def test_pre_monitor_is_a_registered_hook_event() -> None:
    assert _HANDLERS["pre_monitor"] is events.handle_pre_monitor


def test_the_command_and_call_are_the_documented_strings() -> None:
    assert watchdog.WATCH_COMMAND == (
        "python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch"
    )
    assert watchdog.MONITOR_CALL == (
        'Monitor(command="python3 .sentinel-swarm/hook.py watch || python '
        '.sentinel-swarm/hook.py watch", description="sentinel-swarm watchdog", '
        "timeout_ms=1800000)"
    )


def _set_watch(ledger: Ledger, heartbeat: datetime | None, expires: datetime | None) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE runs SET watch_heartbeat_at = ?, watch_expires_at = ?",
            (
                watchdog.stamp(heartbeat) if heartbeat else None,
                watchdog.stamp(expires) if expires else None,
            ),
        )


def test_the_oracle_cannot_stop_until_the_watchdog_is_armed(ledger: Ledger) -> None:
    _run_id(ledger)
    now = watchdog.utcnow()
    stop = {"session_id": ORACLE}

    blocked = events.handle_stop(ledger, stop)
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert watchdog.MONITOR_CALL in blocked["reason"]
    assert "Re-arm it" in blocked["reason"]

    _set_watch(ledger, now - timedelta(seconds=90), now + timedelta(minutes=20))
    assert events.handle_stop(ledger, stop) is not None
    _set_watch(ledger, now, now + timedelta(seconds=10))
    assert events.handle_stop(ledger, stop) is not None
    _set_watch(ledger, now, now + timedelta(minutes=20))
    assert events.handle_stop(ledger, stop) is None
    _set_watch(ledger, now, None)
    assert events.handle_stop(ledger, stop) is None


def test_the_arm_rule_keeps_the_oracles_other_stop_rules(ledger: Ledger) -> None:
    _run_id(ledger)
    ledger.phase_add("oracle", ORACLE, "phase-1")
    ledger.phase_update("oracle", ORACLE, 1, "unlocked")

    assert events.handle_stop(ledger, {"session_id": ORACLE, "stop_hook_active": True}) is None
    merged = events.handle_stop(ledger, {"session_id": ORACLE})
    assert merged is not None
    assert merged["reason"].startswith("The run still has work: 1 unlocked phase(s).")
    assert merged["reason"].endswith("whenever it expires.")

    ledger.run_pause("oracle", ORACLE, "waiting on the user")
    assert events.handle_stop(ledger, {"session_id": ORACLE}) is None


def test_arming_the_monitor_records_the_heartbeat_and_expiry(ledger: Ledger) -> None:
    _run_id(ledger)
    before = watchdog.utcnow()

    events.handle_post_any(
        ledger,
        _monitor(ORACLE, watchdog.WATCH_COMMAND, timeout_ms=1_800_000, description="x"),
    )

    run = ledger.conn.execute("SELECT * FROM runs").fetchone()
    heartbeat = watchdog.parse_stamp(run["watch_heartbeat_at"])
    expires = watchdog.parse_stamp(run["watch_expires_at"])
    assert heartbeat is not None and expires is not None
    assert heartbeat >= before - timedelta(seconds=1)
    assert timedelta(minutes=29) < expires - heartbeat <= timedelta(minutes=30, seconds=1)
    assert events.handle_stop(ledger, {"session_id": ORACLE}) is None


def test_another_monitor_call_does_not_count_as_armed(ledger: Ledger) -> None:
    _run_id(ledger)
    events.handle_post_any(ledger, _monitor(ORACLE, "tail -f x", timeout_ms=1000))
    assert ledger.conn.execute("SELECT watch_heartbeat_at FROM runs").fetchone()[0] is None


def test_watchdog_settings_come_from_the_local_settings_file(host: Path) -> None:
    assert load_settings(host).watchdog == WatchdogSettings(
        interval_seconds=30, stuck_minutes=15, spin_failures=5, context_pct=80, idle_exit_minutes=15
    )
    path = host / ".claude" / "sentinel-swarm.local.md"
    text = path.read_text(encoding="utf-8").replace("stuck_minutes: 15", "stuck_minutes: 40")
    text = text.replace("# context_window: 1000000", "context_window: 500000")
    path.write_text(text, encoding="utf-8")
    assert load_settings(host).watchdog.stuck_minutes == 40
    assert load_settings(host).watchdog.context_window == 500_000
