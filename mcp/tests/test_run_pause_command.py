from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import bootstrap_module

from swarm_ledger import env, pause, resume, serve, watch
from swarm_ledger.clock import utcnow
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger
from swarm_ledger.watchdog import MONITOR_CALL

LAUNCHER = Path(__file__).resolve().parents[1] / "launch" / "ledger.py"

pytestmark = pytest.mark.usefixtures("claude_sessions", "os_notifications")


@pytest.fixture(autouse=True)
def _ledger_db(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env.DB_PATH_VAR, str(ledger.repo_root / ".sentinel-swarm" / "ledger.db"))


@pytest.fixture
def started_servers(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    started: list[Path] = []

    def fake_ensure(root: Path) -> str:
        started.append(root)
        return "http://127.0.0.1:1234/mcp"

    monkeypatch.setattr(serve, "ensure_server", fake_ensure)
    return started


def _run(ledger: Ledger) -> dict:
    return dict(ledger.conn.execute("SELECT * FROM runs").fetchone())


def _pause(ledger: Ledger, *extra: str, reason: str = "I have to leave") -> int:
    return pause.main(["--repo", str(ledger.repo_root), "--reason", reason, *extra])


def test_pause_command_pauses_the_run_and_records_who_paused_it(
    ledger: Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = bootstrap_module(ledger)

    assert _pause(ledger, "--sender", "alex") == 0

    run = _run(ledger)
    assert (run["state"], run["pause_check"]) == ("paused", None)
    assert run["paused_at"] is not None
    reason = ledger.pause_reason(run["run_id"])
    assert reason == "I have to leave (paused by alex)"
    event = ledger.conn.execute(
        "SELECT agent_id, from_state FROM agent_events WHERE to_state = 'paused'"
    ).fetchone()
    assert (event["agent_id"], event["from_state"]) == (ctx["oracle_id"], "active")
    printed = json.loads(capsys.readouterr().out)
    assert (printed["state"], printed["reason"]) == ("paused", "I have to leave")


def test_pause_command_defaults_the_sender_to_the_user(ledger: Ledger) -> None:
    bootstrap_module(ledger)

    assert _pause(ledger) == 0

    assert ledger.pause_reason(_run(ledger)["run_id"]) == "I have to leave (paused by the user)"


def test_pause_command_files_a_directive_and_notifies_like_run_pause(
    ledger: Ledger, os_notifications: list[list[str]]
) -> None:
    bootstrap_module(ledger)

    assert _pause(ledger, "--sender", "alex") == 0

    directive = ledger.conn.execute("SELECT * FROM directives").fetchone()
    assert (directive["source"], directive["sender_name"], directive["state"]) == (
        "outside_session",
        "alex",
        "open",
    )
    assert "I have to leave" in directive["body"]
    assert "directive_resolve" in directive["body"]
    notice = ledger.conn.execute("SELECT * FROM notifications").fetchone()
    assert notice["message"] == (
        "Run paused: I have to leave (paused by alex). Say continue or run "
        "/sentinel-swarm:resume to continue it."
    )
    assert (notice["kind"], notice["push_owed"]) == ("warning", 1)
    assert len(os_notifications) == 1


def test_pause_command_ends_the_oracles_watch_listener(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    assert watch.poll(ledger.conn, utcnow())[0] is True

    _pause(ledger)

    assert watch.poll(ledger.conn, utcnow()) == (False, [])


def test_pause_command_refuses_a_paused_run_and_a_missing_run(
    ledger: Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _pause(ledger) == 1
    assert "no active run" in capsys.readouterr().err
    bootstrap_module(ledger)
    assert _pause(ledger) == 0
    capsys.readouterr()

    assert _pause(ledger) == 1

    assert "already paused" in capsys.readouterr().err


def test_pause_command_refuses_an_empty_reason(
    ledger: Ledger, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap_module(ledger)

    assert _pause(ledger, reason="  ") == 1

    assert "reason" in capsys.readouterr().err
    assert _run(ledger)["state"] == "active"


def test_resume_command_clears_the_pause_and_files_a_wake_directive(
    ledger: Ledger,
    capsys: pytest.CaptureFixture[str],
    started_servers: list[Path],
) -> None:
    ctx = bootstrap_module(ledger)
    _pause(ledger)
    capsys.readouterr()

    assert resume.main(["--repo", str(ledger.repo_root), "--sender", "alex"]) == 0

    run = _run(ledger)
    assert (run["state"], run["pause_check"], run["paused_at"]) == ("active", None, None)
    event = ledger.conn.execute(
        "SELECT agent_id, reason FROM agent_events WHERE from_state = 'paused' "
        "AND to_state = 'active'"
    ).fetchone()
    assert event["agent_id"] == ctx["oracle_id"]
    assert "alex" in event["reason"]
    wake = ledger.conn.execute("SELECT * FROM directives WHERE source = 'watchdog'").fetchone()
    assert wake["state"] == "open"
    assert "Arm the watchdog Monitor again" in wake["body"]
    owed = ledger.conn.execute("SELECT * FROM wakeups WHERE sent_at IS NULL").fetchall()
    assert [(w["from_agent_id"], w["to_agent_id"]) for w in owed] == [
        (ctx["oracle_id"], ctx["oracle_id"])
    ]
    answer = events.handle_self_owed(ledger, {"session_id": ctx["oracle_id"]})
    assert answer is not None and answer["poll"] is True
    assert "The run resumed" in answer["text"] and "directive_inbox" in answer["text"]
    assert MONITOR_CALL in answer["text"]
    assert (
        ledger.conn.execute("SELECT COUNT(*) FROM wakeups WHERE sent_at IS NULL").fetchone()[0] == 0
    )
    assert started_servers == [ledger.repo_root.resolve()]
    assert json.loads(capsys.readouterr().out)["state"] == "active"
    keep, lines = watch.poll(ledger.conn, utcnow() + timedelta(seconds=1))
    assert keep is True
    assert any("resumed the run" in line for line in lines)


def test_resume_command_refuses_a_run_that_is_not_paused(
    ledger: Ledger, capsys: pytest.CaptureFixture[str], started_servers: list[Path]
) -> None:
    assert resume.main(["--repo", str(ledger.repo_root)]) == 1
    assert "no active run" in capsys.readouterr().err
    bootstrap_module(ledger)

    assert resume.main(["--repo", str(ledger.repo_root)]) == 1

    assert "not paused" in capsys.readouterr().err
    assert started_servers == []


def test_resume_command_clears_a_pause_that_names_a_clear_check(
    ledger: Ledger, started_servers: list[Path]
) -> None:
    ctx = bootstrap_module(ledger)
    ledger.run_pause("oracle", ctx["oracle_id"], "config broke", clear_check="config_parses")

    assert resume.main(["--repo", str(ledger.repo_root)]) == 0

    run = _run(ledger)
    assert (run["state"], run["pause_check"]) == ("active", None)


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_the_launcher_runs_the_pause_commands(command: str) -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER), command, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0
    assert f"ledger.py {command}" in done.stdout
