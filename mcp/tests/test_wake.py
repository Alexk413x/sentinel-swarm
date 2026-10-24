from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from helpers import (
    ledger_for,
    make_host,
    register_agent,
    running,
    settings_text,
    untimed,
)

from swarm_ledger import sessions, wake, watch, watchdog
from swarm_ledger.db import connect, write_tx
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger

ORACLE = "sess-1"
ORACLE_SESSION = "host-oracle"
MANAGER = "mgr-agent"
MANAGER_SESSION = "host-r1-manager-1"


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    entries: list[dict] = []

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return json.dumps(entries)

    monkeypatch.setattr(sessions, "_run", fake_run)
    return entries


@pytest.fixture
def host(tmp_path: Path) -> Path:
    return make_host(tmp_path / "host", settings_text(test_command="pytest {target}"))


@pytest.fixture
def ledger(host: Path, listing: list[dict]) -> Ledger:
    return ledger_for(host)


def _bootstrap(ledger: Ledger, listing: list[dict]) -> dict:
    running(listing, ORACLE, ORACLE_SESSION)
    started = ledger.run_start(prd="Build X", session_id=ORACLE)
    phase = ledger.phase_add("oracle", ORACLE, "phase-1")
    ledger.phase_update("oracle", ORACLE, phase["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle",
        ORACLE,
        "mgr-p1-phase-1",
        "manager",
        "opus",
        "Own p1-phase-1.",
        phase_id=phase["phase_id"],
    )
    register_agent(ledger, MANAGER, "manager", parent_agent_id=ORACLE)
    ledger.brief_ack("mgr-p1-phase-1", MANAGER)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (MANAGER_SESSION, MANAGER)
        )
    running(listing, MANAGER, MANAGER_SESSION)
    return started


def _agent(ledger: Ledger, agent_id: str) -> dict:
    return dict(
        ledger.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    )


def _wakeups(ledger: Ledger) -> list[dict]:
    return [dict(r) for r in ledger.conn.execute("SELECT * FROM wakeups ORDER BY wakeup_id")]


def _wakeup(**overrides: Any) -> dict:
    return {
        "wakeup_id": 7,
        "to_agent_id": ORACLE,
        "to_name": "oracle",
        "to_session_name": ORACLE_SESSION,
        "reason": "message_post",
        "pointer": "Message 3 is waiting in the ledger.",
        **overrides,
    }


SEND = 'SendMessage(to="host-oracle", message="Message 3 is waiting in the ledger.")'
RESUME = 'agent_resume(target_name="oracle")'


# -- route_wakeup ---------------------------------------------------------------------------


def test_a_running_target_gets_the_send_message_instruction() -> None:
    delivery = wake.route_wakeup(_wakeup(), live=True)
    assert delivery.kind == "send"
    assert delivery.next == SEND


def test_a_stopped_target_is_resumed() -> None:
    delivery = wake.route_wakeup(_wakeup(), live=False)
    assert delivery.kind == "resume"
    assert delivery.next == RESUME


def test_an_unknown_liveness_names_both_calls() -> None:
    delivery = wake.route_wakeup(_wakeup(), live=None)
    assert delivery.kind == "send_or_resume"
    assert delivery.next == f"{SEND}, or {RESUME} if that session is not running"


def test_a_target_with_no_session_name_is_resumed() -> None:
    delivery = wake.route_wakeup(_wakeup(to_session_name=None), live=True)
    assert delivery.next == RESUME


# -- the ledger's single switch point -------------------------------------------------------


def test_next_step_names_send_message_for_a_running_oracle(
    ledger: Ledger, listing: list[dict]
) -> None:
    _bootstrap(ledger, listing)

    posted = ledger.message_post("mgr-p1-phase-1", MANAGER, "oracle", "phase-1 is done")

    (wakeup,) = _wakeups(ledger)
    message = json.loads(posted["next"].split("message=", 1)[1].removesuffix(")"))
    assert posted["next"] == (
        f"SendMessage(to={json.dumps(ORACLE_SESSION)}, message={json.dumps(message)})"
    )
    assert re.fullmatch(re.escape(wakeup["pointer"]) + r" elapsed \d+s", message)
    assert wakeup["sent_at"] is None
    assert "pushed_at" not in wakeup
    assert "channel" not in _agent(ledger, ORACLE)


def test_the_wake_hint_routes_through_the_switch(ledger: Ledger, listing: list[dict]) -> None:
    _bootstrap(ledger, listing)
    manager = _agent(ledger, MANAGER)
    assert (
        events._wake_hint(manager, {MANAGER})
        == f"wake it with SendMessage(to={json.dumps(MANAGER_SESSION)})"
    )
    assert "its session is not running" in events._wake_hint(manager, set())


def test_the_watchdog_wake_call_routes_through_the_switch(ledger: Ledger, listing) -> None:
    _bootstrap(ledger, listing)
    manager = _agent(ledger, MANAGER)
    running = {MANAGER: {"pid": 1, "sessionId": MANAGER, "status": "idle"}}
    call = watchdog._wake_call(manager, running)
    assert call == f"SendMessage(to={json.dumps(MANAGER_SESSION)})"
    assert watchdog._wake_call(manager, {}) == 'agent_resume(target_name="mgr-p1-phase-1")'


# -- time signal -----------------------------------------------------------------------------

SIGNAL = "elapsed 340s / 1200s"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_the_signal_ends_the_send_message_and_never_the_resume() -> None:
    timed = f"Message 3 is waiting in the ledger. {SIGNAL}"
    delivery = wake.route_wakeup(_wakeup(), live=True, signal=SIGNAL)
    assert delivery.next == f'SendMessage(to="host-oracle", message={json.dumps(timed)})'
    assert wake.route_wakeup(_wakeup(), live=False, signal=SIGNAL).next == RESUME


def test_a_wake_up_with_no_pointer_gets_no_signal() -> None:
    delivery = wake.route_wakeup(_wakeup(pointer=None), live=True, signal=SIGNAL)
    assert delivery.next == 'SendMessage(to="host-oracle")'


def _age_run(ledger: Ledger, seconds: int, budget: int | None = None) -> None:
    snapshot = json.loads(ledger.conn.execute("SELECT settings_json FROM runs").fetchone()[0])
    snapshot["time_budget_minutes"] = budget
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE runs SET started_at = ?, settings_json = ?",
            (watchdog.stamp(NOW - timedelta(seconds=seconds)), json.dumps(snapshot)),
        )


def _member(ledger: Ledger, agent_id: str, role: str, name: str | None = None) -> None:
    name = name or f"{role}-1"
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, run_id, state, session_name, "
            "parent_agent_id) VALUES (?, ?, ?, 1, 'idle', ?, ?)",
            (agent_id, name, role, f"host-{name}", MANAGER),
        )


@pytest.mark.parametrize("budget,expected", [(None, "elapsed 340s"), (20, SIGNAL)])
def test_the_signal_measures_from_the_run_start_against_the_budget(
    ledger: Ledger, listing: list[dict], budget: int | None, expected: str
) -> None:
    _bootstrap(ledger, listing)
    _age_run(ledger, 340, budget)
    assert wake.time_signal(ledger.conn, 1, NOW) == expected


@pytest.mark.parametrize(
    "role,timed", [("lead", False), ("coder", False), ("driver", False), ("manager", True)]
)
def test_only_the_oracle_and_a_manager_get_the_signal(
    ledger: Ledger, listing: list[dict], role: str, timed: bool
) -> None:
    _bootstrap(ledger, listing)
    _age_run(ledger, 340, 20)
    assert wake.signal_for(ledger.conn, ORACLE, NOW) == SIGNAL
    _member(ledger, f"{role}-agent", role, f"{role}-9")
    assert wake.signal_for(ledger.conn, f"{role}-agent", NOW) == (SIGNAL if timed else None)


def test_next_carries_the_signal_for_a_manager_and_none_for_a_lead(
    ledger: Ledger, listing: list[dict]
) -> None:
    _bootstrap(ledger, listing)
    _member(ledger, "lead-agent", "lead")
    running(listing, "lead-agent", "host-lead-1")

    to_manager = ledger.message_post("oracle", ORACLE, "mgr-p1-phase-1", "phase-1 changed")
    to_lead = ledger.message_post("mgr-p1-phase-1", MANAGER, "lead-1", "module changed")

    assert re.search(r' elapsed \d+s"\)$', to_manager["next"])
    assert "elapsed" not in to_lead["next"]
    assert all("elapsed" not in w["pointer"] for w in _wakeups(ledger))


def test_agent_resume_ends_a_managers_message_with_the_signal(
    ledger: Ledger, listing: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(ledger, listing)
    ledger.message_post("oracle", ORACLE, "mgr-p1-phase-1", "phase-1 changed")
    listing[:] = [e for e in listing if e["sessionId"] != MANAGER]
    sent: list[str] = []
    monkeypatch.setattr(
        sessions, "resume", lambda agent_id, message, **_: sent.append(message) or None
    )

    result = ledger.agent_resume("oracle", ORACLE, "mgr-p1-phase-1")

    assert re.search(r"\. elapsed \d+s$", result["message"])
    assert sent == [result["message"]]


def test_each_watch_line_ends_with_the_signal(ledger: Ledger, listing: list[dict]) -> None:
    _bootstrap(ledger, listing)
    _age_run(ledger, 340, 20)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO directives (run_id, source, sender_name, body) "
            "VALUES (1, 'watchdog', 'watchdog', 'Watchdog finding stuck')"
        )

    keep, lines = watch.poll(ledger.conn, NOW)

    assert keep
    assert lines == [f"Watchdog directive 1: Watchdog finding stuck {SIGNAL}"]


# -- the Stop hook ---------------------------------------------------------------------------


def _owe_oracle(ledger: Ledger, listing: list[dict]) -> dict:
    _bootstrap(ledger, listing)
    ledger.message_post("mgr-p1-phase-1", MANAGER, "oracle", "phase-1 is done")
    (wakeup,) = _wakeups(ledger)
    return wakeup


def test_stop_blocks_an_owed_wake_up_at_once_with_send_message(
    ledger: Ledger, listing: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    wakeup = _owe_oracle(ledger, listing)
    monkeypatch.setattr("time.sleep", lambda seconds: pytest.fail("no wait expected"))

    blocked = events.handle_stop(ledger, {"session_id": MANAGER})

    assert blocked is not None and blocked["decision"] == "block"
    send = f"SendMessage(to={json.dumps(ORACLE_SESSION)}, message={json.dumps(wakeup['pointer'])})"
    assert f"- {send}" in untimed(blocked["reason"])
    assert _wakeups(ledger)[0]["sent_at"] is None


def test_stop_names_agent_resume_for_a_stopped_target(ledger: Ledger, listing: list[dict]) -> None:
    _owe_oracle(ledger, listing)
    listing[:] = [e for e in listing if e["sessionId"] != ORACLE]

    blocked = events.handle_stop(ledger, {"session_id": MANAGER})

    assert blocked is not None
    assert f"- {RESUME}" in blocked["reason"]


def test_the_send_message_clears_the_debt(ledger: Ledger, listing: list[dict]) -> None:
    _owe_oracle(ledger, listing)
    events.handle_post_any(
        ledger,
        {"session_id": MANAGER, "tool_name": "SendMessage", "tool_input": {"to": ORACLE_SESSION}},
    )
    assert ledger.owed_wakeups(MANAGER) == []
    assert events.handle_stop(ledger, {"session_id": MANAGER}) is None


# -- launch ----------------------------------------------------------------------------------


# -- the database ------------------------------------------------------------------------------


def test_a_new_ledger_has_no_channel_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    agents = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    wakeups = {r["name"] for r in conn.execute("PRAGMA table_info(wakeups)")}
    conn.close()
    assert "channel" not in agents
    assert "pushed_at" not in wakeups
