from __future__ import annotations

import io
import json
import os
import socket
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from helpers import ledger_for, make_host

from swarm_ledger import capacity, notify, pauses, rekey, serve, sessions, wake, watch, watchdog
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import HANDLERS as _HANDLERS
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import WatchdogSettings, load_settings

ORACLE = "sess-oracle"


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
def host(tmp_path: Path) -> Path:
    return make_host(tmp_path / "host")


@pytest.fixture
def ledger(host: Path, claude: FakeClaude) -> Ledger:
    del claude
    return ledger_for(host)


@pytest.fixture
def now() -> datetime:
    return watchdog.utcnow()


def _run_id(ledger: Ledger) -> int:
    run_id = ledger.run_start(prd="Build X", session_id=ORACLE)["run"]["run_id"]
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET session_name = 'host-oracle' WHERE agent_id = ?", (ORACLE,))
    return run_id


def _signal(ledger: Ledger, now: datetime) -> str | None:
    run_id = ledger.conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    return wake.time_signal(ledger.conn, run_id, now)


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


def _scan(ledger: Ledger, claude: FakeClaude, now: datetime, **settings: Any):
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


def _parked_working(ledger: Ledger, agent_id: str, role: str, now: datetime, beat_age: int) -> None:
    _agent(
        ledger,
        agent_id,
        role,
        "working",
        started=now - timedelta(hours=3),
        heartbeat=now - timedelta(minutes=beat_age),
    )
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET state = 'parked', state_before_park = 'working', "
            "parked_at = ?, park_reason = 'its session is not running' WHERE agent_id = ?",
            (watchdog.stamp(now - timedelta(hours=1)), agent_id),
        )


def _failed_resumes(ledger: Ledger, agent_id: str, count: int) -> None:
    with write_tx(ledger.conn) as conn:
        for _ in range(count):
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
                "VALUES (?, NULL, 'resume_failed', 'claude --resume failed')",
                (agent_id,),
            )


def test_a_dead_agent_whose_resumes_keep_failing_is_reported_for_release(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _parked_working(ledger, "sess-lead-1", "lead", now, beat_age=120)
    _failed_resumes(ledger, "sess-lead-1", 3)

    (finding,) = _scan(ledger, claude, now)

    assert (finding.name, finding.kind) == ("lead-1", "crashed")
    assert "3 resumes failed" in finding.detail
    assert 'agent_release(target_agent_id="sess-lead-1")' in finding.next_step
    assert "agent_resume" not in finding.next_step


def test_a_dead_agent_is_resumed_until_its_resumes_have_failed_repeatedly(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _parked_working(ledger, "sess-lead-1", "lead", now, beat_age=120)
    _parked_working(ledger, "sess-coder-1", "coder", now, beat_age=1)
    _failed_resumes(ledger, "sess-lead-1", 2)
    _failed_resumes(ledger, "sess-coder-1", 5)

    findings = _scan(ledger, claude, now)

    assert [f.next_step for f in findings] == [
        'Resume it with agent_resume(target_name="coder-1").',
        'Resume it with agent_resume(target_name="lead-1").',
    ]


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


def _records(path: Path, *records: dict) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _tool_use(name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}],
        },
    }


_TOOL_RESULT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]},
}


def test_a_session_waiting_on_a_permission_prompt_needs_the_user(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=30)
    transcript = _records(
        tmp_path / "coder.jsonl",
        _tool_use("Write", {"file_path": ".claude/scratch/stitched/test_emit_l4.py"}),
    )
    _agent(
        ledger, "sess-asking", "coder", "working", started=old, heartbeat=old, transcript=transcript
    )
    claude.run("sess-asking", status="waiting", waitingFor="permission prompt")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("asking", "needs_user")]
    assert findings[0].detail == (
        "its session host-r1-asking waits on permission prompt: "
        "Write .claude/scratch/stitched/test_emit_l4.py"
    )
    assert "open the session host-r1-asking in agent view" in findings[0].next_step


_WAITS = [
    {"status": "waiting", "waitingFor": "MCP server prompt"},
    {"status": "waiting"},
    {"status": "idle", "state": "waiting"},
    {"status": "idle", "state": "blocked"},
]


@pytest.mark.parametrize("extra", _WAITS)
def test_a_waiting_status_with_a_pending_request_needs_the_user(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path, extra: dict
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    transcript = _records(tmp_path / "coder.jsonl", _tool_use("Bash", {"command": "npm ci"}))
    _agent(
        ledger, "sess-asking", "coder", "working", started=now, heartbeat=now, transcript=transcript
    )
    claude.run("sess-asking", **extra)

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("asking", "needs_user")]
    default = "the user (blocked)" if extra.get("state") == "blocked" else "input"
    assert findings[0].detail == (
        f"its session host-r1-asking waits on {extra.get('waitingFor') or default}: Bash npm ci"
    )


@pytest.mark.parametrize("extra", _WAITS)
def test_a_waiting_status_with_no_request_is_not_the_users(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path, extra: dict
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    use = _tool_use("Bash", {"command": "npm run lint"})
    replay = {"type": "api-request-blob", "hash": "h1", "message": use["message"]}
    transcript = _records(tmp_path / "coder.jsonl", use, _TOOL_RESULT, replay)
    _agent(
        ledger, "sess-ended", "coder", "working", started=now, heartbeat=now, transcript=transcript
    )
    claude.run("sess-ended", **extra)

    assert _scan(ledger, claude, now) == []


def test_a_blocked_session_with_no_request_goes_to_its_parent_once_quiet(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=20)
    _agent(ledger, "sess-lead-1", "lead", "working", started=old, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=old, heartbeat=old, parent="sess-lead-1"
    )
    claude.run("sess-coder-1", status="idle", state="blocked")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("coder-1", "blocked")]
    assert findings[0].detail == (
        "its session host-r1-coder-1 ended its turn blocked on its work, with no permission "
        "prompt or tool request pending, and has shown no activity for 20 minutes"
    )
    assert findings[0].next_step.startswith('Wake lead-1 (SendMessage(to="host-r1-lead-1")')
    assert "blocked" not in watchdog.USER_KINDS
    assert _scan(ledger, claude, now, stuck_minutes=30) == []


def test_a_blocked_oracle_with_no_request_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle", state="blocked")

    assert _scan(ledger, claude, now) == []


def test_a_coder_on_a_superseded_file_is_only_reported_for_release(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=30)
    transcript = _records(tmp_path / "old.jsonl", _tool_use("Bash", {"command": "npm ci"}))
    _agent(ledger, "sess-lead-1", "lead", "working", started=old, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger,
        "sess-old",
        "coder",
        "working",
        started=old,
        heartbeat=old,
        parent="sess-lead-1",
        transcript=transcript,
    )
    claude.run("sess-old", status="idle", state="blocked")
    with write_tx(ledger.conn) as conn:
        phase = conn.execute(
            "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'p1-x', 1, 'working')",
            (run_id,),
        ).lastrowid
        module = conn.execute(
            "INSERT INTO modules (phase_id, name, state) VALUES (?, 'm', 'working')", (phase,)
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files (module_id, path, owner_agent_id, state, released_at) "
            "VALUES (?, 'src/a.ts', 'old', 'superseded', ?)",
            (module, watchdog.stamp(old)),
        ).lastrowid
        conn.execute("UPDATE agents SET file_id = ? WHERE agent_id = 'sess-old'", (file_id,))

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("old", "superseded")]
    assert findings[0].detail == f"its file {file_id} (src/a.ts) was superseded by a later claim"
    assert 'agent_release(target_agent_id="sess-old")' in findings[0].next_step
    assert findings[0].next_step.startswith("Ask lead-1 to release it")


def test_an_oracle_waiting_on_its_own_question_needs_the_user(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    question = {
        "questions": [
            {"question": "Which database?", "options": [{"label": "SQLite"}, {"label": "Postgres"}]}
        ]
    }
    transcript = _records(tmp_path / "oracle.jsonl", _tool_use("AskUserQuestion", question))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = ?", (str(transcript), ORACLE)
        )
    claude.run(ORACLE, "host-oracle", status="waiting", waitingFor="input needed")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("oracle", "needs_user")]
    assert findings[0].detail.endswith(
        "waits on input needed: AskUserQuestion: Which database? (options: SQLite, Postgres)"
    )


def test_a_tool_call_that_already_has_its_result_is_not_pending(tmp_path: Path) -> None:
    transcript = _records(
        tmp_path / "t.jsonl", _tool_use("Bash", {"command": "pytest"}), _TOOL_RESULT
    )
    assert watchdog.pending_request(str(transcript)) is None
    assert watchdog.pending_request(str(tmp_path / "missing.jsonl")) is None
    assert watchdog.pending_request(None) is None


def _quiet_run(ledger: Ledger, claude: FakeClaude, now: datetime, minutes: int = 20) -> datetime:
    _run_id(ledger)
    old = now - timedelta(minutes=minutes)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET started_at = ?", (watchdog.stamp(old),))
        conn.execute("UPDATE agent_events SET at = ?", (watchdog.stamp(old),))
        conn.execute("UPDATE agents SET state = 'idle'")
    claude.run(ORACLE, "host-oracle", status="busy")
    _agent(ledger, "sess-lead-1", "lead", "idle", started=old, heartbeat=old)
    claude.run("sess-lead-1", status="idle")
    return old


def test_a_run_where_nobody_works_and_nothing_is_owed_is_run_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _quiet_run(ledger, claude, now)

    findings = [f for f in _scan(ledger, claude, now) if f.kind == "run_stalled"]

    assert _kinds(findings) == [("oracle", "run_stalled")]
    assert findings[0].detail == (
        "no agent of the run has worked and no wake-up has been owed for 20 minutes"
    )
    assert "run_stalled" not in [f.kind for f in _scan(ledger, claude, now, stuck_minutes=30)]


def test_a_waiting_session_does_not_count_as_working_for_run_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    old = _quiet_run(ledger, claude, now)
    question = {"questions": [{"question": "Which database?"}]}
    transcript = _records(tmp_path / "oracle.jsonl", _tool_use("AskUserQuestion", question))
    os.utime(transcript, (old.timestamp(), old.timestamp()))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET state = 'working', transcript_path = ? WHERE agent_id = ?",
            (str(transcript), ORACLE),
        )
    claude.listing[0] |= {"status": "waiting", "waitingFor": "input needed"}
    asked = ledger.directive_submit("user_chat", "alex", "Which database?")
    ledger.directive_resolve("oracle", ORACLE, asked["directive_id"], "needs_user", "Ask Alex.")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE directives SET created_at = ?", (watchdog.stamp(now - timedelta(minutes=20)),)
        )

    kinds = _kinds(_scan(ledger, claude, now))

    assert ("oracle", "run_stalled") in kinds
    assert ("oracle", "needs_user") in kinds


def test_an_owed_wake_up_or_a_working_agent_is_not_run_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    old = _quiet_run(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer, created_at) VALUES (1, ?, 'sess-lead-1', 'lead-1', "
            "'host-r1-lead-1', 'review', 'wake', ?)",
            (ORACLE, watchdog.stamp(old)),
        )
    assert "run_stalled" not in [f.kind for f in _scan(ledger, claude, now)]

    with write_tx(ledger.conn) as conn:
        conn.execute("DELETE FROM wakeups")
        conn.execute("UPDATE agents SET state = 'working' WHERE agent_id = 'sess-lead-1'")
    assert "run_stalled" not in [f.kind for f in _scan(ledger, claude, now)]


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


def test_a_superseded_handoff_is_not_pending_work(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)
    with write_tx(ledger.conn) as conn:
        run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
        phase_id = conn.execute(
            "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'p1-a', 1, 'working')",
            (run_id,),
        ).lastrowid
        module_id = conn.execute(
            "INSERT INTO modules (phase_id, name, state) VALUES (?, 'm1', 'working')",
            (phase_id,),
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files (module_id, path, state) VALUES (?, 'a.py', 'handed_up')",
            (module_id,),
        ).lastrowid
        for state in ("superseded", "submitted"):
            conn.execute(
                "INSERT INTO handoffs (file_id, agent_id, state) VALUES (?, 'sess-lead-1', ?)",
                (file_id, state),
            )

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("oracle", "stalled")]
    assert "1 submitted handoff(s)" in findings[0].detail


def test_live_sessions_that_all_sit_idle_with_pending_work_are_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)
    ledger.message_post("oracle", ORACLE, "lead-1", "Module 1 changed.")
    claude.run(ORACLE, "host-oracle", status="busy")
    claude.run("sess-lead-1", status="idle")
    transcript = tmp_path / "oracle.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = ?", (str(transcript), ORACLE)
        )
    later = now + timedelta(minutes=3)
    old = (now - timedelta(minutes=10)).timestamp()
    os.utime(transcript, (old, old))

    findings = _scan(ledger, claude, later)

    assert _kinds(findings) == [("oracle", "stalled")]
    assert findings[0].detail.startswith("no agent of the run has worked for 2 minutes")
    assert 'SendMessage(to="host-r1-lead-1")' in findings[0].next_step

    assert _scan(ledger, claude, now + timedelta(minutes=1)) == []
    fresh = (later - timedelta(seconds=30)).timestamp()
    os.utime(transcript, (fresh, fresh))
    assert _scan(ledger, claude, later) == []
    os.utime(transcript, (old, old))
    claude.listing[1]["status"] = "busy"
    assert _scan(ledger, claude, later) == []


def test_an_empty_stalled_run_still_needs_the_oracle(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    ledger.directive_submit("watchdog", "watchdog", "An earlier report.")

    findings = _scan(ledger, claude, now)

    assert _kinds(findings) == [("oracle", "stalled")]
    assert findings[0].detail.endswith("and the run is not finished")
    assert findings[0].next_step == "Continue the plan, or call run_finish."


def test_a_run_waiting_on_the_user_is_not_stalled(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)
    asked = ledger.directive_submit("user_chat", "alex", "Which database?")
    ledger.directive_resolve("oracle", ORACLE, asked["directive_id"], "needs_user", "Ask Alex.")

    assert _scan(ledger, claude, now) == []

    ledger.directive_submit("user_chat", "alex", "SQLite.", reply_to=asked["directive_id"])
    assert _kinds(_scan(ledger, claude, now)) == [("oracle", "stalled")]


def _directives(ledger: Ledger) -> list[dict]:
    return [dict(r) for r in ledger.conn.execute("SELECT * FROM directives ORDER BY directive_id")]


def test_the_watchdog_flags_a_user_directive_the_oracle_has_not_read(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    sent = ledger.directive_submit("user_chat", "alex", "Scope it to phase 1.")
    ledger.directive_submit("watchdog", "watchdog", "A finding.")
    after = now + timedelta(minutes=6)

    early = _scan(ledger, claude, now + timedelta(minutes=4))
    assert [f for f in early if f.kind == "directive_unread"] == []
    [finding] = [f for f in _scan(ledger, claude, after) if f.kind == "directive_unread"]
    assert finding.name == "oracle"
    assert str(sent["directive_id"]) in finding.detail
    assert "directive_inbox" in finding.next_step
    assert "directive_unread" not in watchdog.USER_KINDS

    ledger.directive_inbox("oracle", ORACLE)
    assert [f for f in _scan(ledger, claude, after) if f.kind == "directive_unread"] == []


def test_a_finding_is_reported_once_cleared_when_gone_and_reopened_when_it_returns(
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
    assert back == []
    assert len(_directives(ledger)) == 1
    assert ledger.conn.execute("SELECT cleared_at FROM watchdog_findings").fetchone()[0] is None

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE directives SET state = 'resolved'")
        conn.execute("UPDATE watchdog_findings SET cleared_at = ?", (watchdog.stamp(later),))
    again_later = watchdog.record(ledger.conn, run_id, _scan(ledger, claude, later), later)
    assert [r["kind"] for r in again_later] == ["crashed"]
    assert len(_directives(ledger)) == 2
    assert ledger.conn.execute("SELECT COUNT(*) FROM watchdog_findings").fetchone()[0] == 1


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


def _dog(
    ledger: Ledger,
    exits: Exits,
    *,
    notify_events: dict[str, Any] | None = None,
    **settings: Any,
) -> watchdog.Watchdog:
    return watchdog.Watchdog(
        ledger.repo_root,
        ledger.conn,
        WatchdogSettings(**settings),
        exit_server=exits,
        activity=lambda: exits.activity,
        log=lambda message: None,
        notify_events=notify_events,
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


def _resume_claim_expired(ledger: Ledger) -> None:
    # The claim of an Oracle resume lasts 120 real seconds; the ticks here step a made-up clock.
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET resume_started_at = NULL")


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
    run_id = _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    dog = _dog(ledger, Exits())

    dog.tick(now)
    assert claude.resumed == []
    dog.tick(now + timedelta(seconds=30))
    message = wake.timed(
        "The watchdog reported 1 finding(s). Read directive_inbox.",
        wake.time_signal(ledger.conn, run_id, now + timedelta(seconds=30)),
    )
    assert claude.resumed == [(ORACLE, message)]
    dog.tick(now + timedelta(minutes=2))
    assert len(claude.resumed) == 1
    _resume_claim_expired(ledger)
    dog.tick(now + timedelta(minutes=6))
    _resume_claim_expired(ledger)
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
        _resume_claim_expired(ledger)
        dog.tick(now + timedelta(minutes=minutes))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET last_heartbeat_at = ? WHERE agent_id = ?",
            (watchdog.stamp(now + timedelta(minutes=13)), ORACLE),
        )

    _resume_claim_expired(ledger)
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


def test_an_oracle_with_no_recorded_session_is_not_resumed(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET session_name = NULL WHERE agent_id = ?", (ORACLE,))
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))

    assert claude.resumed == []
    assert _wakes(ledger) == [
        f"the watchdog cannot wake the Oracle: no session is recorded for {ORACLE!r}"
    ]


def _notifications(ledger: Ledger) -> list[dict]:
    return [
        dict(r) for r in ledger.conn.execute("SELECT * FROM notifications ORDER BY notification_id")
    ]


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
    assert [n for n in _notifications(ledger) if n["event_key"].startswith("oracle_")] == []
    dog.tick(now + timedelta(minutes=2))
    unreachable = [n for n in _notifications(ledger) if n["event_key"].startswith("oracle_")]
    assert len(unreachable) == 1
    assert "sits idle with no watchdog listener" in unreachable[0]["message"]
    dog.tick(now + timedelta(minutes=3))
    assert len([n for n in _notifications(ledger) if n["event_key"].startswith("oracle_")]) == 1

    watch.poll(ledger.conn, now + timedelta(minutes=3))
    oracle["status"] = "stopped"
    dog.tick(now + timedelta(minutes=4))
    assert claude.resumed == []


@pytest.mark.usefixtures("claude")
def test_the_server_exits_after_the_idle_window_with_no_active_run(
    ledger: Ledger, now: datetime
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


@pytest.mark.usefixtures("claude")
def test_an_active_run_never_idles_the_server_out(ledger: Ledger, now: datetime) -> None:
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
    ledger.directive_submit("user_chat", "alex", "Not for the listener.")

    keep, lines = watch.poll(ledger.conn, now)
    assert keep is True
    assert lines == [
        f"Watchdog directive 1: Watchdog finding stuck: line two {_signal(ledger, now)}"
    ]
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
    assert out.getvalue() == f"Watchdog directive 1: First. {_signal(ledger, now)}\n"
    assert naps == [2.0, 2.0]


def test_a_newer_listener_retires_the_older_one(ledger: Ledger, now: datetime) -> None:
    _run_id(ledger)
    watch.claim(ledger.conn, "old")
    ledger.directive_submit("watchdog", "watchdog", "For the new listener.")
    watch.claim(ledger.conn, "new")

    assert watch.poll(ledger.conn, now, "old") == (False, [])
    keep, lines = watch.poll(ledger.conn, now, "new")
    assert keep
    assert lines == [f"Watchdog directive 1: For the new listener. {_signal(ledger, now)}"]


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
    notify.mark_sent(ledger.conn, 1, "")
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


def test_a_busy_oracle_is_left_to_its_listener(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="busy")
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))

    assert claude.resumed == []
    assert _notifications(ledger) == []


def test_needs_user_and_run_stalled_notify_the_user(
    ledger: Ledger,
    claude: FakeClaude,
    now: datetime,
    os_notifications: list[list[str]],
    tmp_path: Path,
) -> None:
    old = _quiet_run(ledger, claude, now)
    transcript = _records(tmp_path / "lead.jsonl", _tool_use("Bash", {"command": "npm ci"}))
    os.utime(transcript, (old.timestamp(), old.timestamp()))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = 'sess-lead-1'",
            (str(transcript),),
        )
    claude.listing[1] |= {"status": "waiting", "waitingFor": "permission prompt"}

    _dog(ledger, Exits()).tick(now)

    messages = {n["message"] for n in _notifications(ledger)}
    assert any(
        m.startswith("The swarm needs you: lead-1, its session host-r1-lead-1") for m in messages
    )
    assert any(m.startswith("The swarm run stalled: no agent of the run") for m in messages)
    assert all(n["push_owed"] == 1 for n in _notifications(ledger))
    assert len(os_notifications) == 2


def _claude_json(text: str) -> Path:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_run_pause_notifies_the_user_with_the_reason(
    ledger: Ledger, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)

    ledger.run_pause("oracle", ORACLE, "the API key is missing")

    [notice] = _notifications(ledger)
    assert notice["message"] == (
        "Run paused: the API key is missing. Say continue or run /sentinel-swarm:resume to "
        "continue it."
    )
    assert notice["kind"] == "warning"
    assert len(os_notifications) == 1


def test_run_pause_refuses_an_unknown_clear_check(ledger: Ledger) -> None:
    _run_id(ledger)
    with pytest.raises(LedgerError, match="unknown clear_check 'disk_free'"):
        ledger.run_pause("oracle", ORACLE, "full disk", clear_check="disk_free")


def test_the_watchdog_resumes_a_pause_once_its_clear_check_passes(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle")
    _claude_json('{"projects": ')
    ledger.run_pause("oracle", ORACLE, "~/.claude.json is corrupt", clear_check="config_parses")
    assert (
        "It resumes by itself once the config file ~/.claude.json parses."
        in (_notifications(ledger)[0]["message"])
    )
    dog = _dog(ledger, Exits())

    dog.tick(now)
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"

    _claude_json('{"projects": {}}')
    dog.tick(now + timedelta(seconds=30))

    run = ledger.conn.execute("SELECT * FROM runs").fetchone()
    assert (run["state"], run["pause_check"], run["paused_at"]) == ("active", None, None)
    events_ = [
        r["reason"]
        for r in ledger.conn.execute(
            "SELECT reason FROM agent_events WHERE from_state = 'paused' AND to_state = 'active'"
        )
    ]
    assert events_ == ["the watchdog resumed the run: the config file ~/.claude.json parses"]
    [directive] = _directives(ledger)
    assert directive["source"] == "watchdog"
    assert "Arm the watchdog Monitor again" in directive["body"]
    assert any(n["event_key"].startswith("resumed:") for n in _notifications(ledger))


def test_a_pause_with_a_clear_check_keeps_the_server(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _claude_json("not json")
    ledger.run_pause("oracle", ORACLE, "~/.claude.json is corrupt", clear_check="config_parses")
    exits = Exits()
    dog = _dog(ledger, exits, idle_exit_minutes=15)

    dog.tick(now)
    dog.tick(now + timedelta(minutes=40))

    assert exits.count == 0


def test_a_paused_run_reminds_the_user_on_the_timer(
    ledger: Ledger, claude: FakeClaude, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle")
    ledger.run_pause("oracle", ORACLE, "the API key is missing")
    assert len(os_notifications) == 1
    paused_at = watchdog.parse_stamp(
        ledger.conn.execute("SELECT paused_at FROM runs").fetchone()[0]
    )
    assert paused_at is not None
    dog = _dog(ledger, Exits(), remind_minutes=30)

    dog.tick(paused_at + timedelta(minutes=10))
    assert len(os_notifications) == 1
    dog.tick(paused_at + timedelta(minutes=31))
    assert len(os_notifications) == 2
    dog.tick(paused_at + timedelta(minutes=40))
    assert len(os_notifications) == 2
    dog.tick(paused_at + timedelta(minutes=62))
    assert len(os_notifications) == 3


def test_an_open_question_is_reminded_and_zero_turns_reminders_off(
    ledger: Ledger, claude: FakeClaude, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="busy")
    ledger.user_ask("oracle", ORACLE, "Which database?")
    assert len(os_notifications) == 1

    _dog(ledger, Exits(), remind_minutes=0).tick(now + timedelta(minutes=90))
    assert len(os_notifications) == 1

    dog = _dog(ledger, Exits(), remind_minutes=30)
    dog.tick(now)
    dog.tick(now + timedelta(minutes=31))
    assert len(os_notifications) == 2
    assert (
        watchdog.reminder(ledger.conn, ledger.conn.execute("SELECT * FROM runs").fetchone(), None)
        == "1 question(s) wait on you: Which database?"
    )


def test_a_session_blocked_on_the_user_is_reminded_until_it_clears(
    ledger: Ledger,
    claude: FakeClaude,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(notify, "show", lambda message, **_: shown.append(message))
    _run_id(ledger)
    _oracle_running(claude)
    transcript = _records(tmp_path / "coder.jsonl", _tool_use("Bash", {"command": "npm ci"}))
    _agent(
        ledger, "sess-asking", "coder", "working", started=now, heartbeat=now, transcript=transcript
    )
    entry = claude.run("sess-asking", status="waiting", waitingFor="permission prompt")
    dog = _dog(ledger, Exits(), remind_minutes=30)
    reminder = (
        "1 session(s) wait on you: asking, its session host-r1-asking waits on permission "
        "prompt: Bash npm ci"
    )

    dog.tick(now)
    [notice] = shown
    assert notice.startswith("The swarm needs you: asking")

    dog.tick(now + timedelta(minutes=31))
    assert [m for m in shown if "wait on you" in m] == [reminder]

    del entry["waitingFor"]
    entry["status"] = "busy"
    dog.tick(now + timedelta(minutes=32))
    dog.tick(now + timedelta(minutes=70))
    assert [m for m in shown if "wait on you" in m] == [reminder]
    assert ledger.conn.execute("SELECT reminded_at FROM runs").fetchone()[0] is None


def test_the_watchdogs_own_pause_notifies_the_user(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    dog = _dog(ledger, Exits())
    for minutes in (0, 1, 6, 12, 18):
        dog.tick(now + timedelta(minutes=minutes))

    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"
    assert any(
        n["message"].startswith("Run paused: the watchdog could not wake the Oracle.")
        for n in _notifications(ledger)
    )


def test_user_ask_posts_an_open_needs_user_directive_and_notifies(
    ledger: Ledger, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-lead-1", "lead", "idle", started=now)

    asked = ledger.user_ask("oracle", ORACLE, "Which database?")

    assert (asked["source"], asked["state"], asked["outcome"]) == ("oracle", "open", "needs_user")
    assert asked["question"] == "Which database?"
    [notice] = _notifications(ledger)
    assert notice["event_key"] == f"needs_user:{asked['directive_id']}"
    assert notice["message"] == (
        f"The swarm needs you (directive {asked['directive_id']}): Which database?"
    )
    assert len(os_notifications) == 1
    with pytest.raises(LedgerError):
        ledger.user_ask("lead-1", "sess-lead-1", "Me too?")
    with pytest.raises(LedgerError, match="needs the question"):
        ledger.user_ask("oracle", ORACLE, "  ")


def test_resolving_a_directive_needs_user_notifies_once(
    ledger: Ledger, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    asked = ledger.directive_submit("user_chat", "alex", "Ship it?")

    ledger.directive_resolve("oracle", ORACLE, asked["directive_id"], "needs_user", "Which tag?")
    ledger.directive_resolve("oracle", ORACLE, asked["directive_id"], "needs_user", "Which tag?")

    assert [n["event_key"] for n in _notifications(ledger)] == [
        f"needs_user:{asked['directive_id']}"
    ]
    assert len(os_notifications) == 1


def _limited(path: Path, text: str, at: str = "2026-11-15T06:19:25.545Z") -> Path:
    record = {
        "type": "assistant",
        "timestamp": at,
        "isApiErrorMessage": True,
        "error": "rate_limit",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    return _records(path, _tool_use("Bash", {"command": "npm test"}), _TOOL_RESULT, record)


def test_a_usage_limit_stop_pauses_the_run_instead_of_needing_the_user(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    limit = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = ?",
            (str(_limited(tmp_path / "oracle.jsonl", limit)), ORACLE),
        )
    claude.run(ORACLE, "host-oracle", status="idle", state="blocked")
    old = now - timedelta(minutes=30)
    coder_log = _limited(tmp_path / "coder.jsonl", limit)
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=old, heartbeat=old, transcript=coder_log
    )
    claude.run("sess-coder-1", status="idle", state="blocked")

    _dog(ledger, Exits()).tick(now)

    run = ledger.conn.execute("SELECT * FROM runs").fetchone()
    assert run["state"] == "paused"
    assert run["pause_check"].startswith("usage_limit@")
    reason = ledger.conn.execute(
        "SELECT reason FROM agent_events WHERE to_state = 'paused'"
    ).fetchone()[0]
    assert "usage limit" in reason and "2 session(s)" in reason
    kinds = [r[0] for r in ledger.conn.execute("SELECT kind FROM watchdog_findings")]
    assert "needs_user" not in kinds


def test_a_usage_limit_pause_resumes_after_the_reset_and_wakes_the_stopped(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle")
    old = now - timedelta(minutes=30)
    coder_log = _limited(tmp_path / "coder.jsonl", "You've hit your limit · resets 3pm")
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=old, heartbeat=old, transcript=coder_log
    )
    claude.run("sess-coder-1", status="idle", state="blocked")
    reset = now + timedelta(hours=1)
    with write_tx(ledger.conn) as conn:
        pauses.record_pause(
            conn,
            1,
            ORACLE,
            "the account hit its usage limit",
            f"usage_limit@{watchdog.stamp(reset)}",
            (),
            now,
        )
    dog = _dog(ledger, Exits())

    dog.tick(now + timedelta(minutes=30))
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"

    dog.tick(reset + timedelta(minutes=3))

    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "active"
    owed = ledger.conn.execute(
        "SELECT from_agent_id, to_agent_id, reason FROM wakeups WHERE sent_at IS NULL "
        "ORDER BY wakeup_id"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in owed] == [
        (ORACLE, ORACLE, pauses.RESUMED_WAKE_REASON),
        (ORACLE, "sess-coder-1", pauses.USAGE_WAKE_REASON),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You've hit your weekly limit · resets Nov 18, 6am (America/Los_Angeles)", (11, 18, 6, 0)),
        ("You've hit your session limit · resets 3pm (America/Los_Angeles)", (11, 15, 15, 0)),
        ("You've hit your limit · resets 3:30am", (11, 16, 3, 30)),
    ],
)
def test_the_reset_time_is_read_from_the_limit_message(text: str, expected: tuple) -> None:
    now = datetime(2026, 11, 15, 10, 0).astimezone()
    reset = pauses.usage_reset(text, now)
    assert reset is not None
    local = reset.astimezone()
    assert (local.month, local.day, local.hour, local.minute) == expected
    assert pauses.usage_reset("You've hit your limit", now) is None


# -- issue 27: one crashed finding per agent -----------------------------------------------------


def _crash_cycle(ledger: Ledger, claude: FakeClaude, at: datetime, agent_id: str) -> list[dict]:
    run_id = ledger.conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    findings = _scan(ledger, claude, at)
    return watchdog.record(ledger.conn, run_id, findings, at)


def _rekey(ledger: Ledger, claude: FakeClaude, old_id: str, new_id: str) -> None:
    with write_tx(ledger.conn) as conn:
        rekey.adopt(conn, old_id, new_id)
    claude.run(new_id, "host-r1-coder-1")


def test_an_agent_that_keeps_dying_raises_one_crashed_finding_across_re_keys(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)

    first = _crash_cycle(ledger, claude, now, "sess-coder-1")
    assert [f["agent_id"] for f in first] == ["sess-coder-1"]

    _rekey(ledger, claude, "sess-coder-1", "sess-coder-2")
    assert _crash_cycle(ledger, claude, now + timedelta(seconds=30), "sess-coder-2") == []

    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != "sess-coder-2"]
    again = _crash_cycle(ledger, claude, now + timedelta(seconds=60), "sess-coder-2")

    assert again == []
    rows = ledger.conn.execute(
        "SELECT agent_id, cleared_at FROM watchdog_findings WHERE kind = 'crashed'"
    ).fetchall()
    assert [(r["agent_id"], r["cleared_at"]) for r in rows] == [("sess-coder-2", None)]
    assert len([d for d in _directives(ledger) if d["source"] == "watchdog"]) == 1


def test_a_crash_after_the_directive_was_resolved_is_reported_again_in_the_same_finding(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    _crash_cycle(ledger, claude, now, "sess-coder-1")
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE directives SET state = 'resolved' WHERE source = 'watchdog'")
    _rekey(ledger, claude, "sess-coder-1", "sess-coder-2")
    _crash_cycle(ledger, claude, now + timedelta(seconds=30), "sess-coder-2")
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != "sess-coder-2"]

    again = _crash_cycle(ledger, claude, now + timedelta(seconds=60), "sess-coder-2")

    assert len(again) == 1
    assert ledger.conn.execute("SELECT COUNT(*) FROM watchdog_findings").fetchone()[0] == 1
    assert len([d for d in _directives(ledger) if d["source"] == "watchdog"]) == 2


def test_the_crashed_finding_counts_failed_resumes_and_names_the_last_reason(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now, heartbeat=now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES ('sess-coder-1', NULL, 'resume_failed', 'exit code 3: no such session')"
        )
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES ('sess-coder-1', NULL, 'resume_vanished', "
            "'its resumed session registered and was gone 41s after the resume')"
        )

    (finding,) = _scan(ledger, claude, now)

    assert "2 resumes failed" in finding.detail
    assert "gone 41s after the resume" in finding.detail
    assert "agent_resume" in finding.next_step


def test_a_resume_that_stayed_up_resets_the_failed_resume_count(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now, heartbeat=now)
    with write_tx(ledger.conn) as conn:
        for state in ("resume_failed", "resume_stable"):
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
                "VALUES ('sess-coder-1', NULL, ?, 'x')",
                (state,),
            )

    (finding,) = _scan(ledger, claude, now)

    assert "resumes failed" not in finding.detail


def _parent_and_child(ledger: Ledger, now: datetime, child_state: str) -> datetime:
    _run_id(ledger)
    old = now - timedelta(minutes=30)
    _agent(ledger, "sess-lead-1", "lead", "working", started=old, heartbeat=old)
    _agent(
        ledger,
        "sess-coder-1",
        "coder",
        child_state,
        started=old,
        heartbeat=now,
        parent="sess-lead-1",
    )
    return old


@pytest.mark.parametrize("session", [{"status": "idle"}, {"status": "idle", "state": "blocked"}])
def test_a_parent_waiting_on_a_working_child_is_neither_stuck_nor_blocked(
    ledger: Ledger, claude: FakeClaude, now: datetime, session: dict
) -> None:
    _parent_and_child(ledger, now, "working")
    _oracle_running(claude)
    claude.run("sess-lead-1", **session)
    claude.run("sess-coder-1")

    kinds = _kinds(_scan(ledger, claude, now))

    assert ("lead-1", "stuck") not in kinds and ("lead-1", "blocked") not in kinds


def _park(ledger: Ledger, agent_id: str, before: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET state = 'parked', state_before_park = ?, parked_at = ?, "
            "park_reason = 'retired' WHERE agent_id = ?",
            (before, watchdog.stamp(watchdog.utcnow()), agent_id),
        )


@pytest.mark.parametrize("session", [{"status": "idle"}, {"status": "idle", "state": "blocked"}])
@pytest.mark.parametrize("before", ["registered", "working"])
def test_a_parent_whose_child_is_parked_mid_work_is_neither_stuck_nor_blocked(
    ledger: Ledger, claude: FakeClaude, now: datetime, session: dict, before: str
) -> None:
    _parent_and_child(ledger, now, "working")
    _park(ledger, "sess-coder-1", before)
    _oracle_running(claude)
    claude.run("sess-lead-1", **session)

    kinds = _kinds(_scan(ledger, claude, now))

    assert ("lead-1", "stuck") not in kinds and ("lead-1", "blocked") not in kinds


@pytest.mark.parametrize("before", ["idle", "handed_up"])
def test_a_parent_whose_child_is_parked_with_nothing_to_do_is_still_stuck(
    ledger: Ledger, claude: FakeClaude, now: datetime, before: str
) -> None:
    _parent_and_child(ledger, now, "working")
    _park(ledger, "sess-coder-1", before)
    _oracle_running(claude)
    claude.run("sess-lead-1", status="idle")

    assert ("lead-1", "stuck") in _kinds(_scan(ledger, claude, now))


def _brief_scenario(
    ledger: Ledger,
    claude: FakeClaude,
    now: datetime,
    *,
    role: str = "lead",
    age_minutes: int = 20,
    claim: bool = True,
) -> tuple[int, int]:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-mgr-1", "manager", "working", started=now, heartbeat=now)
    claude.run("sess-mgr-1", status="idle")
    child = "coder-9" if role == "coder" else "lead-9"
    with write_tx(ledger.conn) as conn:
        phase = conn.execute(
            "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'p1-x', 1, 'working')",
            (run_id,),
        ).lastrowid
        module = conn.execute(
            "INSERT INTO modules (phase_id, name, state) VALUES (?, 'm', 'working')", (phase,)
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files (module_id, path, owner_agent_id, state, released_at) "
            "VALUES (?, 'src/a.ts', ?, 'claimed', ?)",
            (module, child, None if claim else watchdog.stamp(now)),
        ).lastrowid
        brief_id = conn.execute(
            "INSERT INTO briefs (run_id, parent_agent_id, child_name, child_role, body, phase_id, "
            "file_id, created_at) VALUES (?, 'sess-mgr-1', ?, ?, 'Do it.', ?, ?, ?)",
            (
                run_id,
                child,
                role,
                phase,
                file_id if role == "coder" else None,
                watchdog.stamp(now - timedelta(minutes=age_minutes)),
            ),
        ).lastrowid
    assert brief_id is not None and phase is not None
    return brief_id, phase


def _unstarted(ledger: Ledger, claude: FakeClaude, now: datetime) -> list[watchdog.Finding]:
    return [f for f in _scan(ledger, claude, now) if f.kind == "brief_unstarted"]


def test_a_brief_no_child_started_for_is_reported_on_its_parent(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now)

    (found,) = _unstarted(ledger, claude, now)

    assert found.name == "mgr-1"
    assert "lead-9" in found.detail and "20 minutes" in found.detail
    assert "agent_spawn" in found.next_step and '"lead-9"' in found.next_step
    assert found.next_step.startswith("Wake mgr-1")


def test_the_brief_report_for_an_oracle_parent_says_to_call_agent_spawn_itself(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE briefs SET parent_agent_id = ?", (ORACLE,))

    (found,) = _unstarted(ledger, claude, now)

    assert found.name == "oracle"
    assert found.next_step.startswith("Call agent_spawn")


def test_a_coder_brief_with_a_live_claim_is_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now, role="coder")

    (found,) = _unstarted(ledger, claude, now)

    assert "coder-9" in found.detail


def test_a_brief_older_than_stuck_minutes_only_is_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now, age_minutes=14)

    assert _unstarted(ledger, claude, now) == []
    assert len(_unstarted(ledger, claude, now + timedelta(minutes=2))) == 1


def test_a_brief_with_a_queued_spawn_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    brief_id, _ = _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO spawn_queue (run_id, brief_id, child_name, role, parent_agent_id) "
            "VALUES (1, ?, 'lead-9', 'lead', 'sess-mgr-1')",
            (brief_id,),
        )

    assert _unstarted(ledger, claude, now) == []


def test_a_brief_whose_spawn_was_dropped_from_the_queue_is_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    brief_id, _ = _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO spawn_queue (run_id, brief_id, child_name, role, parent_agent_id, state) "
            "VALUES (1, ?, 'lead-9', 'lead', 'sess-mgr-1', 'dropped')",
            (brief_id,),
        )

    assert len(_unstarted(ledger, claude, now)) == 1


def test_a_voided_brief_is_not_reported(ledger: Ledger, claude: FakeClaude, now: datetime) -> None:
    brief_id, _ = _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE briefs SET voided_at = ? WHERE brief_id = ?", (watchdog.stamp(now), brief_id)
        )

    assert _unstarted(ledger, claude, now) == []


def test_a_brief_whose_child_is_live_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now)
    _agent(ledger, "sess-lead-9", "lead", "registered", started=now, parent="sess-mgr-1")

    assert _unstarted(ledger, claude, now) == []


def test_an_acknowledged_brief_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE briefs SET acked_at = ?", (watchdog.stamp(now),))

    assert _unstarted(ledger, claude, now) == []


def test_a_brief_whose_parent_ended_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET ended_at = ? WHERE agent_id = 'sess-mgr-1'", (watchdog.stamp(now),)
        )

    assert _unstarted(ledger, claude, now) == []


def test_a_coder_brief_without_a_live_claim_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _brief_scenario(ledger, claude, now, role="coder", claim=False)

    assert _unstarted(ledger, claude, now) == []


def test_a_brief_into_a_paused_phase_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _, phase = _brief_scenario(ledger, claude, now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE phases SET paused_at = ?, pause_reason = 'blocked' WHERE phase_id = ?",
            (watchdog.stamp(now), phase),
        )

    assert _unstarted(ledger, claude, now) == []


def test_a_parent_holding_an_open_wait_for_is_not_stuck(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=30)
    _agent(ledger, "sess-lead-1", "lead", "working", started=old, heartbeat=old)
    claude.run("sess-lead-1", status="idle")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO file_waits (run_id, agent_id, path, reason) VALUES (?, ?, ?, ?)",
            (run_id, "sess-lead-1", "src/a.ts", "its contract"),
        )

    assert ("lead-1", "stuck") not in _kinds(_scan(ledger, claude, now))


def test_a_parent_whose_child_handed_up_is_still_stuck(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _parent_and_child(ledger, now, "handed_up")
    _oracle_running(claude)
    claude.run("sess-lead-1", status="idle")
    claude.run("sess-coder-1", status="idle")

    assert ("lead-1", "stuck") in _kinds(_scan(ledger, claude, now))


def test_stuck_then_blocked_is_one_finding_and_one_directive(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    run_id = _run_id(ledger)
    _oracle_running(claude)
    old = now - timedelta(minutes=30)
    _agent(ledger, "sess-lead-1", "lead", "working", started=old, heartbeat=old)
    entry = claude.run("sess-lead-1", status="idle")

    watchdog.record(ledger.conn, run_id, _scan(ledger, claude, now), now)
    entry["state"] = "blocked"
    later = now + timedelta(minutes=1)
    watchdog.record(ledger.conn, run_id, _scan(ledger, claude, later), later)

    rows = ledger.conn.execute(
        "SELECT kind, cleared_at FROM watchdog_findings WHERE agent_id = 'sess-lead-1'"
    ).fetchall()
    assert [(r["kind"], r["cleared_at"]) for r in rows] == [("blocked", None)]
    directives = [d for d in _directives(ledger) if "lead-1" in d["body"]]
    assert len(directives) == 1


# -- per-type notification switches ---------------------------------------------------------


def _stall_with_needs_user(
    ledger: Ledger, claude: FakeClaude, now: datetime, tmp_path: Path
) -> None:
    old = _quiet_run(ledger, claude, now)
    transcript = _records(tmp_path / "lead.jsonl", _tool_use("Bash", {"command": "npm ci"}))
    os.utime(transcript, (old.timestamp(), old.timestamp()))
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = 'sess-lead-1'",
            (str(transcript),),
        )
    claude.listing[1] |= {"status": "waiting", "waitingFor": "permission prompt"}


def test_a_switched_off_needs_user_sends_nothing_while_run_stalled_keeps_its_channel(
    ledger: Ledger,
    claude: FakeClaude,
    now: datetime,
    os_notifications: list[list[str]],
    tmp_path: Path,
) -> None:
    _stall_with_needs_user(ledger, claude, now, tmp_path)

    _dog(ledger, Exits(), notify_events={"needs_user": False, "run_stalled": ["os"]}).tick(now)

    rows = {n["message"].split(":")[0]: n for n in _notifications(ledger)}
    assert list(rows) == ["The swarm run stalled"]
    assert rows["The swarm run stalled"]["push_owed"] == 0
    assert len(os_notifications) == 1
    assert notify.owed(ledger.conn, 1) == []


def test_a_needs_user_that_follows_notify_still_shows_and_owes_push(
    ledger: Ledger,
    claude: FakeClaude,
    now: datetime,
    os_notifications: list[list[str]],
    tmp_path: Path,
) -> None:
    _stall_with_needs_user(ledger, claude, now, tmp_path)

    _dog(ledger, Exits(), notify_events={"run_stalled": False}).tick(now)

    [row] = [n for n in _notifications(ledger) if n["message"].startswith("The swarm needs you")]
    assert row["push_owed"] == 1
    assert len(os_notifications) == 1


def _pause_and_time(ledger: Ledger, claude: FakeClaude) -> datetime:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle")
    ledger.run_pause("oracle", ORACLE, "the API key is missing")
    paused_at = watchdog.parse_stamp(
        ledger.conn.execute("SELECT paused_at FROM runs").fetchone()[0]
    )
    assert paused_at is not None
    return paused_at


def test_a_switched_off_pause_reminder_never_shows(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    paused_at = _pause_and_time(ledger, claude)
    assert len(os_notifications) == 1
    dog = _dog(ledger, Exits(), remind_minutes=30, notify_events={"pause_reminder": False})

    dog.tick(paused_at + timedelta(minutes=31))
    dog.tick(paused_at + timedelta(minutes=62))

    assert len(os_notifications) == 1


def test_a_pause_reminder_limited_to_push_shows_no_os_notification(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    paused_at = _pause_and_time(ledger, claude)
    dog = _dog(ledger, Exits(), remind_minutes=30, notify_events={"pause_reminder": ["push"]})

    dog.tick(paused_at + timedelta(minutes=31))

    assert len(os_notifications) == 1


def test_a_switched_off_run_resumed_sends_nothing(
    ledger: Ledger, claude: FakeClaude, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    claude.run(ORACLE, "host-oracle", status="idle")
    _claude_json('{"projects": ')
    ledger.run_pause("oracle", ORACLE, "~/.claude.json is corrupt", clear_check="config_parses")
    shown = len(os_notifications)
    dog = _dog(ledger, Exits(), notify_events={"run_resumed": False})
    dog.tick(now)

    _claude_json('{"projects": {}}')
    dog.tick(now + timedelta(seconds=30))

    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "active"
    assert [n for n in _notifications(ledger) if n["event_key"].startswith("resumed:")] == []
    assert len(os_notifications) == shown


def test_a_switched_off_run_paused_covers_the_watchdogs_own_pause(
    ledger: Ledger, claude: FakeClaude, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="stopped")
    dog = _dog(ledger, Exits(), notify_events={"run_paused": False})
    for minutes in (0, 1, 6, 12, 18):
        dog.tick(now + timedelta(minutes=minutes))

    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"
    assert _notifications(ledger) == []
    assert os_notifications == []


def test_a_switched_off_oracle_unreachable_sends_nothing(
    ledger: Ledger, claude: FakeClaude, now: datetime, os_notifications: list[list[str]]
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="idle")
    dog = _dog(ledger, Exits(), notify_events={"oracle_unreachable": False})
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))

    assert _notifications(ledger) == []
    assert os_notifications == []


def test_a_switched_off_driver_sends_nothing_from_the_watchdog(
    ledger: Ledger, now: datetime, os_notifications: list[list[str]]
) -> None:
    run_id = _run_id(ledger)
    _agent(ledger, "sess-driver", "driver", "working", started=now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO directives (run_id, source, sender_name, body) "
            "VALUES (?, 'watchdog', 'watchdog', 'x')",
            (run_id,),
        )
    reported = [{"agent_id": "sess-driver", "kind": "crashed", "detail": "gone", "directive_id": 1}]

    off = watchdog.driver_notices(ledger.conn, run_id, reported, ["os", "push"], {"driver": False})
    assert off == []
    assert os_notifications == []

    [notice] = watchdog.driver_notices(ledger.conn, run_id, reported, ["os", "push"], {})
    assert notice["channels"] == ["os", "push"]


# -- one digest directive per pass ----------------------------------------------------------------


def _finding(agent_id: str, kind: str, detail: str = "it is broken") -> watchdog.Finding:
    name = agent_id.removeprefix("sess-")
    return watchdog.Finding(agent_id, name, f"host-r1-{name}", kind, detail, f"Fix {name}.")


def _two_coders(ledger: Ledger, now: datetime) -> int:
    run_id = _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    _agent(ledger, "sess-coder-2", "coder", "working", started=now)
    return run_id


def test_findings_of_one_pass_share_one_digest_directive(ledger: Ledger, now: datetime) -> None:
    run_id = _two_coders(ledger, now)
    _agent(ledger, "sess-coder-3", "coder", "working", started=now)
    findings = [
        _finding("sess-coder-1", "crashed", "its session is gone"),
        _finding("sess-coder-2", "stuck", "no activity for 20 minutes"),
        _finding("sess-coder-3", "blocked", "it ended its turn"),
    ]

    reported = watchdog.record(ledger.conn, run_id, findings, now)

    [directive] = _directives(ledger)
    assert [r["directive_id"] for r in reported] == [directive["directive_id"]] * 3
    lines = directive["body"].splitlines()
    assert lines[0] == "Watchdog pass: 3 findings"
    assert len(lines) == 5
    assert lines[1] == (
        "- crashed: coder-1 (session host-r1-coder-1): its session is gone. Next: Fix coder-1."
    )
    assert "stuck: coder-2" in lines[2] and "blocked: coder-3" in lines[3]
    assert "directive_resolve" in lines[4]
    rows = ledger.conn.execute("SELECT directive_id FROM watchdog_findings").fetchall()
    assert {r[0] for r in rows} == {directive["directive_id"]}


def test_a_pass_with_one_finding_keeps_the_single_finding_directive(
    ledger: Ledger, now: datetime
) -> None:
    run_id = _two_coders(ledger, now)

    watchdog.record(ledger.conn, run_id, [_finding("sess-coder-1", "stuck")], now)

    [directive] = _directives(ledger)
    assert directive["body"] == (
        "Watchdog finding stuck: coder-1 (session host-r1-coder-1): it is broken. "
        "Next: Fix coder-1. Then resolve this directive with directive_resolve."
    )


def test_user_and_driver_findings_keep_their_own_directives(ledger: Ledger, now: datetime) -> None:
    run_id = _two_coders(ledger, now)
    _agent(ledger, "sess-driver-1", "driver", "working", started=now)
    findings = [
        _finding("sess-coder-1", "needs_user"),
        _finding("sess-coder-2", "stuck"),
        _finding("sess-driver-1", "crashed"),
        _finding("sess-driver-1", "driver_overdue"),
        _finding(ORACLE, "run_stalled"),
    ]

    reported = watchdog.record(ledger.conn, run_id, findings, now)

    assert len(reported) == 5
    assert len(_directives(ledger)) == 5
    assert len({r["directive_id"] for r in reported}) == 5
    assert all("Watchdog pass" not in d["body"] for d in _directives(ledger))
    notices = watchdog.user_notices(ledger.conn, run_id, reported, ["os"])
    assert {n["event_key"] for n in notices} == {
        f"directive:{r['directive_id']}" for r in reported if r["kind"] in watchdog.USER_KINDS
    }


def test_findings_beside_a_user_finding_share_a_digest_of_their_own(
    ledger: Ledger, now: datetime
) -> None:
    run_id = _two_coders(ledger, now)
    findings = [
        _finding("sess-coder-1", "needs_user"),
        _finding("sess-coder-1", "stuck"),
        _finding("sess-coder-2", "blocked"),
    ]

    reported = watchdog.record(ledger.conn, run_id, findings, now)

    bodies = [d["body"] for d in _directives(ledger)]
    assert len(bodies) == 2
    assert bodies[0].startswith("Watchdog finding needs_user")
    assert bodies[1].startswith("Watchdog pass: 2 findings")
    assert reported[0]["directive_id"] != reported[1]["directive_id"] == reported[2]["directive_id"]


def test_a_digest_directive_stays_open_while_its_other_findings_are_open(
    ledger: Ledger, now: datetime
) -> None:
    run_id = _two_coders(ledger, now)
    both = [_finding("sess-coder-1", "stuck"), _finding("sess-coder-2", "blocked")]
    watchdog.record(ledger.conn, run_id, both, now)
    later = now + timedelta(minutes=1)

    again = watchdog.record(ledger.conn, run_id, both[1:], later)

    assert again == []
    [directive] = _directives(ledger)
    assert directive["state"] == "open"
    rows = ledger.conn.execute(
        "SELECT agent_id, cleared_at, directive_id FROM watchdog_findings ORDER BY finding_id"
    ).fetchall()
    assert [(r[0], r[1] is None) for r in rows] == [("sess-coder-1", False), ("sess-coder-2", True)]
    assert {r[2] for r in rows} == {directive["directive_id"]}


# -- wake-ups the Oracle's listener does not deliver ----------------------------------------------


def _self_wakeups(ledger: Ledger) -> list[dict]:
    return [
        dict(r)
        for r in ledger.conn.execute(
            "SELECT * FROM wakeups WHERE from_agent_id = to_agent_id ORDER BY wakeup_id"
        )
    ]


def _unreachable_notices(ledger: Ledger) -> list[dict]:
    return [n for n in _notifications(ledger) if n["event_key"].startswith("oracle_")]


def test_an_idle_oracle_with_no_listener_is_woken_before_the_user_is_notified(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="idle")
    dog = _dog(ledger, Exits())
    dog.tick(now)

    dog.tick(now + timedelta(minutes=1))

    [owed] = _self_wakeups(ledger)
    assert (owed["to_agent_id"], owed["sent_at"]) == (ORACLE, None)
    assert "watchdog reported 1 finding(s)" in owed["pointer"]
    assert "directive_inbox" in owed["pointer"] and watchdog.MONITOR_CALL in owed["pointer"]
    assert _unreachable_notices(ledger) == []

    dog.tick(now + timedelta(minutes=1, seconds=10))
    assert len(_self_wakeups(ledger)) == 1
    assert _unreachable_notices(ledger) == []

    dog.tick(now + timedelta(minutes=2))
    [notice] = _unreachable_notices(ledger)
    assert "sits idle with no watchdog listener" in notice["message"]
    assert len(_self_wakeups(ledger)) == 1
    assert claude.resumed == []


def test_a_delivered_wake_up_spares_the_user_the_notice(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="idle")
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))

    events.handle_self_owed(ledger, {"session_id": ORACLE})
    ledger.directive_inbox("oracle", ORACLE)
    dog.tick(now + timedelta(minutes=2))

    assert _unreachable_notices(ledger) == []


def test_a_directive_filed_after_the_last_wake_up_owes_a_new_one(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _agent(ledger, "sess-coder-1", "coder", "working", started=now)
    claude.run(ORACLE, status="idle")
    dog = _dog(ledger, Exits())
    dog.tick(now)
    dog.tick(now + timedelta(minutes=1))
    events.handle_self_owed(ledger, {"session_id": ORACLE})
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE directives SET notified_at = ?", (watchdog.stamp(now),))
    ledger.directive_submit("watchdog", "watchdog", "A later finding.")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE directives SET created_at = ? WHERE notified_at IS NULL",
            (watchdog.stamp(now + timedelta(minutes=3)),),
        )

    dog.tick(now + timedelta(minutes=4))

    assert len(_self_wakeups(ledger)) == 2
    assert _unreachable_notices(ledger) == []


def _wake_row(
    ledger: Ledger, from_id: str, to_id: str, to_name: str, session: str, at: datetime
) -> None:
    with write_tx(ledger.conn) as conn:
        run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer, created_at) VALUES (?, ?, ?, ?, ?, 'message', 'Read your inbox.', ?)",
            (run_id, from_id, to_id, to_name, session, watchdog.stamp(at)),
        )


def _unsent(ledger: Ledger, claude: FakeClaude, now: datetime) -> list[watchdog.Finding]:
    return [f for f in _scan(ledger, claude, now) if f.kind == "wake_unsent"]


def test_an_unsent_wake_up_to_a_session_the_oracle_cannot_message_names_the_parents_call(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=now, heartbeat=now, parent="sess-lead-1"
    )
    claude.run("sess-coder-1")
    _wake_row(ledger, "sess-lead-1", "sess-coder-1", "coder-1", "host-r1-coder-1", now)

    [finding] = _unsent(ledger, claude, now + timedelta(minutes=10))

    assert finding.name == "coder-1"
    assert 'SendMessage(to="host-r1-lead-1"' in finding.next_step
    assert 'SendMessage(to="host-r1-coder-1"' not in finding.next_step


def test_an_unsent_wake_up_to_an_idle_session_still_names_the_direct_call(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger, "sess-coder-1", "coder", "idle", started=now, heartbeat=now, parent="sess-lead-1"
    )
    claude.run("sess-coder-1", status="idle")
    _wake_row(ledger, "sess-lead-1", "sess-coder-1", "coder-1", "host-r1-coder-1", now)

    [finding] = _unsent(ledger, claude, now + timedelta(minutes=10))

    assert finding.next_step.startswith('Wake it: SendMessage(to="host-r1-coder-1"')


def test_an_unsent_wake_up_to_a_child_of_the_oracle_names_the_direct_call(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now, heartbeat=now)
    claude.run("sess-lead-1")
    _wake_row(ledger, ORACLE, "sess-lead-1", "lead-1", "host-r1-lead-1", now)

    [finding] = _unsent(ledger, claude, now + timedelta(minutes=10))

    assert finding.next_step.startswith('Wake it: SendMessage(to="host-r1-lead-1"')


def test_a_wake_up_unsent_to_a_session_parked_for_memory_is_not_reported(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=now, heartbeat=now, parent="sess-lead-1"
    )
    _wake_row(ledger, "sess-lead-1", "sess-coder-1", "coder-1", "host-r1-coder-1", now)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET state = 'parked', state_before_park = 'working', parked_at = ?, "
            "park_reason = ? WHERE agent_id = 'sess-coder-1'",
            (watchdog.stamp(now), capacity.WAITING_FOR_MEMORY),
        )
    later = now + timedelta(minutes=10)

    assert _unsent(ledger, claude, later) == []

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET park_reason = 'a pressure park'")
    assert [f.name for f in _unsent(ledger, claude, later)] == ["coder-1"]


def test_the_listener_prints_a_digest_directive_as_one_line(ledger: Ledger, now: datetime) -> None:
    run_id = _two_coders(ledger, now)
    findings = [_finding("sess-coder-1", "stuck"), _finding("sess-coder-2", "blocked")]
    watchdog.record(ledger.conn, run_id, findings, now)

    keep, lines = watch.poll(ledger.conn, now)

    assert keep is True
    [line] = lines
    assert line.startswith("Watchdog directive 1: Watchdog pass: 2 findings - stuck: coder-1")
    assert "- blocked: coder-2" in line and "\n" not in line


def _hook_fallbacks(ledger: Ledger, agent_id: str, at: datetime, hooks: list[str]) -> None:
    with write_tx(ledger.conn) as conn:
        for hook in hooks:
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
                "VALUES (?, 'working', 'hook_fallback', ?, ?)",
                (agent_id, hook, watchdog.stamp(at)),
            )


def _unreachable(ledger: Ledger, claude: FakeClaude, now: datetime) -> list[watchdog.Finding]:
    return [f for f in _scan(ledger, claude, now) if f.kind == "hook_unreachable"]


def test_an_agent_whose_hooks_keep_falling_back_is_reported_to_its_parent(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _agent(ledger, "sess-lead-1", "lead", "working", started=now, heartbeat=now)
    claude.run("sess-lead-1")
    _agent(
        ledger, "sess-coder-1", "coder", "working", started=now, heartbeat=now, parent="sess-lead-1"
    )
    claude.run("sess-coder-1")
    assert watchdog.HOOK_FALLBACK_LIMIT == 3

    _hook_fallbacks(ledger, "sess-coder-1", now, ["pre_write", "post_any"])
    assert _unreachable(ledger, claude, now + timedelta(seconds=5)) == []

    _hook_fallbacks(ledger, "sess-coder-1", now, ["post_any"])
    [finding] = _unreachable(ledger, claude, now + timedelta(seconds=5))

    assert finding.name == "coder-1"
    assert (
        "3 of its hooks (post_any, pre_write) could not reach the ledger server" in finding.detail
    )
    assert finding.next_step.startswith('Wake its parent lead-1: SendMessage(to="host-r1-lead-1"')
    assert _unreachable(ledger, claude, now + timedelta(seconds=31)) == []


def test_fallback_events_of_two_agents_are_not_added_together(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _two_coders(ledger, now)
    _oracle_running(claude)
    _hook_fallbacks(ledger, "sess-coder-1", now, ["post_any", "post_any"])
    _hook_fallbacks(ledger, "sess-coder-2", now, ["post_any", "post_any"])

    assert _unreachable(ledger, claude, now) == []


def test_the_oracles_own_fallbacks_name_the_server_check(
    ledger: Ledger, claude: FakeClaude, now: datetime
) -> None:
    _run_id(ledger)
    _oracle_running(claude)
    _hook_fallbacks(ledger, ORACLE, now, ["stop", "post_any", "pre_ledger"])

    [finding] = _unreachable(ledger, claude, now)

    assert finding.agent_id == ORACLE
    assert finding.next_step.startswith("Call ledger_info()")


def test_a_hook_unreachable_finding_joins_the_digest(ledger: Ledger, now: datetime) -> None:
    run_id = _two_coders(ledger, now)
    findings = [_finding("sess-coder-1", "hook_unreachable"), _finding("sess-coder-2", "stuck")]

    watchdog.record(ledger.conn, run_id, findings, now)

    bodies = [r["body"] for r in ledger.conn.execute("SELECT body FROM directives")]
    assert len(bodies) == 1
    assert bodies[0].startswith("Watchdog pass: 2 findings")
