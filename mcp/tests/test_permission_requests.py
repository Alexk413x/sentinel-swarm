from __future__ import annotations

import json
import os
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import bootstrap_module, make_host, settings_text

from swarm_ledger import auth, front, panel, permission, serve, watchdog
from swarm_ledger.clock import stamp, utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events, run_event
from swarm_ledger.http_front import LedgerHttpServer
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import WatchdogSettings, load_settings

pytestmark = pytest.mark.usefixtures("claude_sessions")

LEAD = "lead-agent"
OUTSIDE = {"file_path": "C:/elsewhere/x.py", "content": "print('x')\n"}


def _ask(ledger: Ledger, session_id: str = LEAD, **extra: object) -> dict | None:
    data = {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": OUTSIDE,
        "reason": "Write outside the allowed working directories",
        **extra,
    }
    return events.handle_permission_request(ledger, data)


def _row(ledger: Ledger, request_id: int) -> dict:
    row = ledger.conn.execute(
        "SELECT * FROM permission_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return dict(row)


def _expire(ledger: Ledger, request_id: int) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE permission_requests SET expires_at = ? WHERE request_id = ?",
            (stamp(utcnow() - timedelta(seconds=1)), request_id),
        )


def test_a_live_agent_of_the_run_records_an_open_request(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)

    opened = _ask(ledger)

    assert opened is not None
    assert (opened["hold_seconds"], opened["wait_seconds"]) == (300, permission.WAIT_S)
    row = _row(ledger, opened["request_id"])
    assert row["run_id"] == ctx["run_id"]
    assert (row["agent_id"], row["tool"], row["summary"]) == (LEAD, "Write", "C:/elsewhere/x.py")
    assert row["reason"] == "Write outside the allowed working directories"
    assert row["state"] == "open"
    assert row["expires_at"] == opened["expires_at"] > row["created_at"]


def test_a_shell_request_summarizes_its_command_on_one_line(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger, tool_name="Bash", tool_input={"command": "rm -rf\n  build"})
    assert opened is not None
    assert _row(ledger, opened["request_id"])["summary"] == "rm -rf build"


def test_a_session_outside_the_live_run_records_nothing(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    assert _ask(ledger, session_id="someone-else") is None

    ledger.run_close("oracle", ctx["oracle_id"], "abandoned: a test closes the run")
    assert _ask(ledger) is None
    assert ledger.conn.execute("SELECT COUNT(*) FROM permission_requests").fetchone()[0] == 0


def test_a_hold_of_zero_records_nothing(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    ledger.settings.permission_hold_seconds = 0
    assert _ask(ledger) is None


def test_the_wait_returns_the_answer_and_its_note(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    request_id = opened["request_id"]
    wait = {"session_id": LEAD, "request_id": request_id, "wait_seconds": 0}

    assert events.handle_permission_wait(ledger, wait) == {"state": "open", "note": None}

    permission.answer(ledger.conn, request_id, False, "write it under src", utcnow())
    assert events.handle_permission_wait(ledger, wait) == {
        "state": "denied",
        "note": "write it under src",
    }
    row = _row(ledger, request_id)
    assert (row["answered_by"], row["answered_at"] is not None) == ("run panel", True)


def test_another_session_cannot_wait_on_or_close_the_request(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    other = {"session_id": "mgr-agent", "request_id": opened["request_id"], "wait_seconds": 0}

    assert events.handle_permission_wait(ledger, other) is None
    assert events.handle_permission_close(ledger, {**other, "state": "withdrawn"}) == {
        "closed": False
    }
    assert _row(ledger, opened["request_id"])["state"] == "open"


def test_the_wait_sees_an_answer_that_arrives_while_it_waits(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    request_id = opened["request_id"]
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        permission.answer(ledger.conn, request_id, True, None, utcnow())

    row = permission.wait_answer(ledger.conn, request_id, LEAD, 5.0, sleep=nap)

    assert row is not None and row["state"] == "allowed"
    assert naps == [permission.POLL_S]


def test_a_request_past_its_hold_expires(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    _expire(ledger, opened["request_id"])

    answer = events.handle_permission_wait(
        ledger, {"session_id": LEAD, "request_id": opened["request_id"], "wait_seconds": 0}
    )

    assert answer == {"state": "expired", "note": None}


@pytest.mark.parametrize("state", ["expired", "withdrawn"])
def test_the_session_closes_its_open_request(ledger: Ledger, state: str) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    close = {"session_id": LEAD, "request_id": opened["request_id"], "state": state}

    assert events.handle_permission_close(ledger, close) == {"closed": True}
    assert _row(ledger, opened["request_id"])["state"] == state
    assert events.handle_permission_close(ledger, close) == {"closed": False}
    assert events.handle_permission_close(ledger, {**close, "state": "allowed"}) is None


def _launch(host: Path, *args: str) -> int:
    return permission.main(["--repo", str(host), *args])


def test_the_launcher_allows_an_open_request(
    ledger: Ledger, host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None

    assert _launch(host, "--id", str(opened["request_id"]), "--allow") == 0

    printed = json.loads(capsys.readouterr().out)
    assert (printed["state"], printed["answered_by"]) == ("allowed", "run panel")
    assert _row(ledger, opened["request_id"])["state"] == "allowed"


def test_the_launcher_denies_with_a_note(ledger: Ledger, host: Path) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None

    assert _launch(host, "--id", str(opened["request_id"]), "--deny", "--note", "not there") == 0

    row = _row(ledger, opened["request_id"])
    assert (row["state"], row["note"]) == ("denied", "not there")


def test_the_launcher_refuses_a_request_that_is_not_open(
    ledger: Ledger, host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    request_id = str(opened["request_id"])
    assert _launch(host, "--id", request_id, "--allow") == 0
    capsys.readouterr()

    assert _launch(host, "--id", request_id, "--deny") == 1

    assert "is allowed, not open" in capsys.readouterr().err
    assert _row(ledger, opened["request_id"])["state"] == "allowed"
    assert _launch(host, "--id", "999", "--allow") == 1
    assert "no permission request 999" in capsys.readouterr().err


def test_the_launcher_expires_a_request_past_its_hold(
    ledger: Ledger, host: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    _expire(ledger, opened["request_id"])

    assert _launch(host, "--id", str(opened["request_id"]), "--allow") == 1

    assert "agent view" in capsys.readouterr().err
    assert _row(ledger, opened["request_id"])["state"] == "expired"


def test_answer_refuses_a_closed_request_directly(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None
    permission.close(ledger.conn, opened["request_id"], LEAD, "withdrawn", utcnow())
    with pytest.raises(LedgerError, match="is withdrawn, not open"):
        permission.answer(ledger.conn, opened["request_id"], True, None, utcnow())


def test_the_panel_lists_open_requests_under_waiting(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    listed = _ask(ledger)
    answered = _ask(ledger)
    expired = _ask(ledger)
    assert listed is not None and answered is not None and expired is not None
    permission.answer(ledger.conn, answered["request_id"], True, None, utcnow())
    _expire(ledger, expired["request_id"])

    run = panel.snapshot(ledger.conn, {})["runs"][0]

    assert run["waiting"]["permissions"] == [
        {
            "request_id": listed["request_id"],
            "agent": "lead-p1-module-1",
            "tool": "Write",
            "summary": "C:/elsewhere/x.py",
            "reason": "Write outside the allowed working directories",
            "asked_at": _row(ledger, listed["request_id"])["created_at"],
            "expires_at": listed["expires_at"],
        }
    ]


def _working_lead(ledger: Ledger, minutes_ago: int) -> list[dict]:
    old = stamp(utcnow() - timedelta(minutes=minutes_ago))
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET started_at = ?", (old,))
        conn.execute("UPDATE agent_events SET at = ?", (old,))
        conn.execute("UPDATE messages SET created_at = ?", (old,))
        conn.execute("UPDATE wakeups SET sent_at = ?", (old,))
        conn.execute("UPDATE agents SET state = 'idle'")
        conn.execute(
            "UPDATE agents SET state = 'working', last_heartbeat_at = ?, "
            "session_name = 'host-r1-lead' WHERE agent_id = ?",
            (old, LEAD),
        )
    return [
        {"pid": 1, "sessionId": "sess-1", "name": "host-oracle", "status": "busy"},
        {"pid": 2, "sessionId": "mgr-agent", "name": "host-r1-mgr", "status": "idle"},
        {"pid": 3, "sessionId": LEAD, "name": "host-r1-lead", "status": "busy"},
    ]


def _scan(ledger: Ledger, listing: list[dict]) -> list[watchdog.Finding]:
    return watchdog.scan(ledger.conn, listing, utcnow(), WatchdogSettings())


def test_a_held_request_needs_the_user_and_is_not_stuck(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    listing = _working_lead(ledger, 20)
    assert ("lead-p1-module-1", "stuck") in [(f.name, f.kind) for f in _scan(ledger, listing)]
    opened = _ask(ledger)
    assert opened is not None

    findings = [f for f in _scan(ledger, listing) if f.agent_id == LEAD]

    assert [f.kind for f in findings] == ["needs_user"]
    detail = findings[0].detail
    assert detail.startswith(f"its session host-r1-lead waits on {watchdog.HELD_WAIT}")
    assert "Write C:/elsewhere/x.py" in detail
    assert f"answer permission request {opened['request_id']} in the run panel" in detail
    assert "in the run panel" in findings[0].next_step


def test_a_held_session_does_not_count_as_working_for_run_stalled(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    listing = _working_lead(ledger, 20)
    assert "run_stalled" not in [f.kind for f in _scan(ledger, listing)]
    assert _ask(ledger) is not None

    stalled = [f for f in _scan(ledger, listing) if f.kind == "run_stalled"]

    assert len(stalled) == 1
    assert stalled[0].detail.endswith("waiting on the user: host-r1-lead")


def test_an_expired_or_answered_request_no_longer_waits(ledger: Ledger) -> None:
    bootstrap_module(ledger)
    listing = _working_lead(ledger, 1)
    opened = _ask(ledger)
    assert opened is not None
    _expire(ledger, opened["request_id"])
    assert "needs_user" not in [f.kind for f in _scan(ledger, listing)]

    answered = _ask(ledger)
    assert answered is not None
    permission.answer(ledger.conn, answered["request_id"], True, None, utcnow())
    assert "needs_user" not in [f.kind for f in _scan(ledger, listing)]


def test_a_held_request_notifies_and_is_reminded(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    listing = _working_lead(ledger, 1)
    assert _ask(ledger) is not None
    now = utcnow()

    reported = watchdog.record(ledger.conn, ctx["run_id"], _scan(ledger, listing), now)
    notices = watchdog.user_notices(ledger.conn, ctx["run_id"], reported, ["os"])

    assert [row["kind"] for row in reported] == ["needs_user"]
    assert "The swarm needs you: lead-p1-module-1" in notices[0]["message"]
    assert "in the run panel" in notices[0]["message"]
    run = ledger.conn.execute("SELECT * FROM runs WHERE run_id = ?", (ctx["run_id"],)).fetchone()
    reminder = watchdog.reminder(ledger.conn, run, None)
    assert reminder is not None and "1 session(s) wait on you: lead-p1-module-1" in reminder


def test_the_hold_setting_defaults_to_300_and_0_turns_it_off(tmp_path: Path) -> None:
    default = make_host(tmp_path / "default")
    assert load_settings(default).permission_hold_seconds == 300
    off = make_host(
        tmp_path / "off",
        settings_text(swaps=[("permission_hold_seconds: 300", "permission_hold_seconds: 0")]),
    )
    assert load_settings(off).permission_hold_seconds == 0
    odd = make_host(
        tmp_path / "odd",
        settings_text(swaps=[("permission_hold_seconds: 300", "permission_hold_seconds: soon")]),
    )
    assert load_settings(odd).permission_hold_seconds == 300


def test_the_wait_runs_on_the_front_thread_and_never_on_a_hook_worker(
    ledger: Ledger, host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap_module(ledger)
    opened = _ask(ledger)
    assert opened is not None

    class NoWorker:
        def call(self, request: dict) -> dict:
            raise AssertionError(f"a hook worker ran {request}")

    monkeypatch.setattr(front, "_hook_pool", NoWorker())
    payload = json.dumps(
        {"session_id": LEAD, "request_id": opened["request_id"], "wait_seconds": 0}
    )

    stdout, stderr = front.run_hook(host, "permission_wait", payload)

    assert (json.loads(stdout), stderr) == ({"state": "open", "note": None}, "")


@pytest.fixture
def lean(host: Path) -> Iterator[int]:
    token = auth.ensure_token(host)
    sock = serve.bind_socket(host)
    port = sock.getsockname()[1]
    front.configure(host)
    httpd = LedgerHttpServer(
        sock,
        host,
        lambda client, headers, path: serve.refusal(client, headers, path, token, port),
        {"name": "swarm-ledger", "repo_root": str(host), "pid": os.getpid()},
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(port: int, host: Path, event: str, body: dict, token: bool = True) -> tuple[int, str]:
    import http.client

    headers = auth.auth_headers(host) if token else {}
    headers[serve.REPO_HEADER] = urllib.parse.quote(str(host))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", f"{serve.HOOK_PATH}/{event}", json.dumps(body), headers)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()


def test_the_routes_record_answer_and_close_a_request_over_http(
    ledger: Ledger, host: Path, lean: int
) -> None:
    bootstrap_module(ledger)
    ask = {"session_id": LEAD, "tool_name": "Write", "tool_input": OUTSIDE, "reason": "outside"}

    status, body = _post(lean, host, "permission_request", ask)
    assert status == 200
    request_id = json.loads(body)["request_id"]
    permission.answer(ledger.conn, request_id, True, None, utcnow())
    status, body = _post(
        lean, host, "permission_wait", {"session_id": LEAD, "request_id": request_id}
    )
    assert (status, json.loads(body)) == (200, {"state": "allowed", "note": None})

    status, body = _post(lean, host, "permission_request", ask, token=False)
    assert status == 403
    status, body = _post(lean, host, "permission_request", {**ask, "session_id": "stranger"})
    assert (status, body) == (200, "")


def test_run_event_matches_the_route(ledger: Ledger, host: Path) -> None:
    bootstrap_module(ledger)
    raw = json.dumps({"session_id": LEAD, "tool_name": "Write", "tool_input": OUTSIDE})
    stdout, stderr = run_event("permission_request", raw, host)
    assert stderr == ""
    assert json.loads(stdout)["hold_seconds"] == 300
