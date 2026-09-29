from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import (
    agentfiles,
    bridge,
    launch,
    serve,
    sessions,
    setup,
    wake,
    watch,
    watchdog,
)
from swarm_ledger.db import connect, write_tx
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import load_settings

ORACLE = "sess-1"
ORACLE_SESSION = "host-oracle"
MANAGER = "mgr-agent"
MANAGER_SESSION = "host-r1-manager-1"


class Subscriber:
    def __init__(self, hub: wake.EventHub, session_id: str) -> None:
        self.loop = asyncio.new_event_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.hub = hub
        self.session_id = session_id
        self.token = hub.subscribe(session_id, self.loop, self.queue)

    def events(self) -> list[dict]:
        self.loop.run_until_complete(asyncio.sleep(0))
        found = []
        while not self.queue.empty():
            found.append(self.queue.get_nowait())
        return found

    def close(self) -> None:
        self.hub.unsubscribe(self.session_id, self.token)
        self.loop.close()


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> wake.EventHub:
    fresh = wake.EventHub()
    monkeypatch.setattr(wake, "HUB", fresh)
    return fresh


@pytest.fixture
def subscribe(hub: wake.EventHub) -> Iterator:
    made: list[Subscriber] = []

    def make(session_id: str) -> Subscriber:
        made.append(Subscriber(hub, session_id))
        return made[-1]

    yield make
    for sub in made:
        sub.close()


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    entries: list[dict] = []

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return json.dumps(entries)

    monkeypatch.setattr(sessions, "_run", fake_run)
    return entries


def _write_settings(root: Path, text: str) -> None:
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    _write_settings(root, template.replace("test_command:\n", "test_command: pytest {target}\n"))
    return root


def _ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


@pytest.fixture
def ledger(host: Path, listing: list[dict], hub: wake.EventHub) -> Ledger:
    return _ledger(host)


def _running(listing: list[dict], agent_id: str, name: str) -> None:
    listing.append({"pid": 7, "sessionId": agent_id, "name": name, "status": "idle"})


def _bootstrap(ledger: Ledger, listing: list[dict]) -> dict:
    _running(listing, ORACLE, ORACLE_SESSION)
    started = ledger.run_start(prd="Build X", session_id=ORACLE)
    phase = ledger.phase_add("oracle", ORACLE, "phase-1")
    ledger.phase_update("oracle", ORACLE, phase["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle", ORACLE, "manager-1", "manager", "opus", "Own phase-1.", phase_id=phase["phase_id"]
    )
    ledger.agent_register_start(MANAGER, "manager", parent_agent_id=ORACLE)
    ledger.brief_ack("manager-1", MANAGER)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (MANAGER_SESSION, MANAGER)
        )
    _running(listing, MANAGER, MANAGER_SESSION)
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


def _untimed(text: str) -> str:
    assert re.search(r" elapsed \d+s(?: / \d+s)?", text), text
    return re.sub(r" elapsed \d+s(?: / \d+s)?", "", text)


def test_the_sendmessage_setting_returns_todays_instruction_and_never_pushes(
    hub: wake.EventHub, subscribe
) -> None:
    sub = subscribe(ORACLE)
    delivery = wake.route_wakeup(
        _wakeup(), transport="sendmessage", live=True, channel="confirmed", hub=hub
    )
    assert delivery.kind == "send"
    assert delivery.next == SEND
    assert sub.events() == []


def test_a_stopped_target_is_resumed_even_when_its_channel_is_connected(
    hub: wake.EventHub, subscribe
) -> None:
    sub = subscribe(ORACLE)
    delivery = wake.route_wakeup(
        _wakeup(), transport="channel", live=False, channel="launched", hub=hub
    )
    assert delivery.next == RESUME
    assert sub.events() == []


def test_a_supported_target_gets_the_event_and_the_sender_has_nothing_to_send(
    hub: wake.EventHub, subscribe
) -> None:
    sub = subscribe(ORACLE)
    delivery = wake.route_wakeup(
        _wakeup(), transport="channel", live=True, channel="launched", hub=hub
    )
    assert delivery.pushed
    assert delivery.next.startswith("Nothing to send: the ledger delivered this wake-up to oracle")
    assert sub.events() == [
        {
            "kind": "wakeup",
            "content": "Message 3 is waiting in the ledger.",
            "meta": {"wakeup_id": "7", "reason": "message_post"},
        }
    ]


@pytest.mark.parametrize(
    "channel,connected,wakeup_id",
    [("none", True, 7), ("launched", False, 7), ("launched", True, None)],
)
def test_an_unsupported_target_gets_the_send_message_instruction(
    hub: wake.EventHub, subscribe, channel: str, connected: bool, wakeup_id: int | None
) -> None:
    if connected:
        subscribe(ORACLE)
    delivery = wake.route_wakeup(
        _wakeup(wakeup_id=wakeup_id), transport="channel", live=True, channel=channel, hub=hub
    )
    assert delivery.kind == "send"
    assert delivery.next == SEND


def test_an_unknown_liveness_names_both_calls_or_pushes_to_a_connected_bridge(
    hub: wake.EventHub, subscribe
) -> None:
    delivery = wake.route_wakeup(
        _wakeup(), transport="channel", live=None, channel="launched", hub=hub
    )
    assert delivery.next == f"{SEND}, or {RESUME} if that session is not running"
    subscribe(ORACLE)
    assert wake.route_wakeup(
        _wakeup(), transport="channel", live=None, channel="launched", hub=hub
    ).pushed


def test_a_target_with_no_session_name_is_resumed() -> None:
    delivery = wake.route_wakeup(_wakeup(to_session_name=None), transport="channel", live=True)
    assert delivery.next == RESUME


def test_the_fallback_never_pushes(hub: wake.EventHub, subscribe) -> None:
    sub = subscribe(ORACLE)
    assert wake.fallback(_wakeup(), True).next == SEND
    assert sub.events() == []


def test_a_push_to_a_closed_loop_counts_as_undelivered(hub: wake.EventHub) -> None:
    loop = asyncio.new_event_loop()
    hub.subscribe(ORACLE, loop, asyncio.Queue())
    loop.close()
    assert not hub.push(ORACLE, {"kind": "wakeup"})
    assert not hub.connected(ORACLE)


# -- settings --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "channel"),
        ("channel", "channel"),
        ("sendmessage", "sendmessage"),
        ("SendMessage", "sendmessage"),
        ("send_message", "sendmessage"),
        ("pigeon", "channel"),
    ],
)
def test_wake_transport_defaults_to_channel(tmp_path: Path, value: str | None, expected: str):
    if value is not None:
        _write_settings(tmp_path, f"---\nwake_transport: {value}\n---\n")
    assert load_settings(tmp_path).wake_transport == expected


def test_the_settings_example_names_the_default(repo_root: Path) -> None:
    text = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(encoding="utf-8")
    assert "\nwake_transport: channel\n" in text


# -- the ledger's single switch point -------------------------------------------------------


def test_next_step_pushes_to_a_launched_oracle_and_records_it(
    ledger: Ledger, listing: list[dict], subscribe
) -> None:
    sub = subscribe(ORACLE)
    _bootstrap(ledger, listing)
    assert _agent(ledger, ORACLE)["channel"] == "launched"

    posted = ledger.message_post("manager-1", MANAGER, "oracle", "phase-1 is done")

    assert posted["next"].startswith("Nothing to send")
    (wakeup,) = _wakeups(ledger)
    assert wakeup["pushed_at"] is not None
    assert wakeup["sent_at"] is None
    (event,) = sub.events()
    assert re.fullmatch(re.escape(wakeup["pointer"]) + r" elapsed \d+s", event["content"])
    assert event["meta"] == {"wakeup_id": str(wakeup["wakeup_id"]), "reason": "message_post"}


def test_the_sendmessage_setting_keeps_todays_next_exactly(
    host: Path, listing: list[dict], hub: wake.EventHub, subscribe
) -> None:
    _write_settings(host, "---\nwake_transport: sendmessage\n---\n")
    ledger = _ledger(host)
    sub = subscribe(ORACLE)
    _bootstrap(ledger, listing)

    posted = ledger.message_post("manager-1", MANAGER, "oracle", "phase-1 is done")

    (wakeup,) = _wakeups(ledger)
    message = json.loads(posted["next"].split("message=", 1)[1].removesuffix(")"))
    assert posted["next"] == (
        f"SendMessage(to={json.dumps(ORACLE_SESSION)}, message={json.dumps(message)})"
    )
    assert re.fullmatch(re.escape(wakeup["pointer"]) + r" elapsed \d+s", message)
    assert wakeup["pushed_at"] is None
    assert sub.events() == []


def test_a_session_without_a_bridge_stays_on_send_message(
    ledger: Ledger, listing: list[dict]
) -> None:
    _bootstrap(ledger, listing)
    assert _agent(ledger, ORACLE)["channel"] == "none"
    posted = ledger.message_post("manager-1", MANAGER, "oracle", "phase-1 is done")
    assert posted["next"].startswith(f"SendMessage(to={json.dumps(ORACLE_SESSION)}")
    assert _wakeups(ledger)[0]["pushed_at"] is None


def test_a_push_inside_a_write_transaction_is_recorded(ledger: Ledger, listing, subscribe) -> None:
    subscribe(ORACLE)
    _bootstrap(ledger, listing)
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer) VALUES (1, ?, ?, 'oracle', ?, 'test', 'p')",
            (MANAGER, ORACLE, ORACLE_SESSION),
        )
        wakeup = dict(
            conn.execute("SELECT * FROM wakeups WHERE wakeup_id = ?", (cur.lastrowid,)).fetchone()
        )
        assert ledger.next_step(wakeup) is not None
    assert _wakeups(ledger)[0]["pushed_at"] is not None


def test_channel_registered_marks_launched_but_never_downgrades(ledger: Ledger, listing) -> None:
    _bootstrap(ledger, listing)
    ledger.channel_registered(ORACLE)
    assert _agent(ledger, ORACLE)["channel"] == "launched"
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET channel = 'confirmed' WHERE agent_id = ?", (ORACLE,))
    ledger.channel_registered(ORACLE)
    assert _agent(ledger, ORACLE)["channel"] == "confirmed"


def test_a_resumed_run_marks_the_new_oracle_session(
    ledger: Ledger, listing: list[dict], subscribe
) -> None:
    _bootstrap(ledger, listing)
    listing.clear()
    subscribe("sess-2")
    resumed = ledger.run_start(prd="Build X", session_id="sess-2")
    assert resumed["resumed"] is True
    assert resumed["oracle"]["channel"] == "launched"


def test_the_wake_hint_routes_through_the_switch(ledger: Ledger, listing: list[dict]) -> None:
    _bootstrap(ledger, listing)
    manager = _agent(ledger, MANAGER)
    assert (
        events._wake_hint(ledger, manager, {MANAGER})
        == f"wake it with SendMessage(to={json.dumps(MANAGER_SESSION)})"
    )
    assert "its session is not running" in events._wake_hint(ledger, manager, set())


def test_the_watchdog_wake_call_routes_through_the_switch(ledger: Ledger, listing) -> None:
    _bootstrap(ledger, listing)
    manager = _agent(ledger, MANAGER)
    running = {MANAGER: {"pid": 1, "sessionId": MANAGER, "status": "idle"}}
    call = watchdog._wake_call(manager, running, "channel")
    assert call == f"SendMessage(to={json.dumps(MANAGER_SESSION)})"
    assert watchdog._wake_call(manager, {}, "channel") == 'agent_resume(target_name="manager-1")'


# -- time signal -----------------------------------------------------------------------------

SIGNAL = "elapsed 340s / 1200s"
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_the_signal_ends_the_send_message_and_the_pushed_event(
    hub: wake.EventHub, subscribe
) -> None:
    timed = f"Message 3 is waiting in the ledger. {SIGNAL}"
    delivery = wake.route_wakeup(_wakeup(), transport="sendmessage", live=True, signal=SIGNAL)
    assert delivery.next == f'SendMessage(to="host-oracle", message={json.dumps(timed)})'
    assert wake.fallback(_wakeup(), False, SIGNAL).next == RESUME

    sub = subscribe(ORACLE)
    pushed = wake.route_wakeup(
        _wakeup(), transport="channel", live=True, channel="launched", hub=hub, signal=SIGNAL
    )
    assert pushed.pushed
    assert sub.events()[0]["content"] == timed


def test_a_wake_up_with_no_pointer_gets_no_signal() -> None:
    delivery = wake.route_wakeup(
        _wakeup(pointer=None), transport="sendmessage", live=True, signal=SIGNAL
    )
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
    _running(listing, "lead-agent", "host-lead-1")

    to_manager = ledger.message_post("oracle", ORACLE, "manager-1", "phase-1 changed")
    to_lead = ledger.message_post("manager-1", MANAGER, "lead-1", "module changed")

    assert re.search(r' elapsed \d+s"\)$', to_manager["next"])
    assert "elapsed" not in to_lead["next"]
    assert all("elapsed" not in w["pointer"] for w in _wakeups(ledger))


def test_agent_resume_ends_a_managers_message_with_the_signal(
    ledger: Ledger, listing: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap(ledger, listing)
    ledger.message_post("oracle", ORACLE, "manager-1", "phase-1 changed")
    listing[:] = [e for e in listing if e["sessionId"] != MANAGER]
    sent: list[str] = []
    monkeypatch.setattr(
        sessions, "resume", lambda agent_id, message, **_: sent.append(message) or None
    )

    result = ledger.agent_resume("oracle", ORACLE, "manager-1")

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


# -- confirmation ----------------------------------------------------------------------------


PUSHED_AT = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def _line(text: str, *, at: datetime = PUSHED_AT, **fields: Any) -> str:
    record = {
        "type": "user",
        "isSidechain": False,
        "timestamp": watchdog.stamp(at),
        "message": {"role": "user", "content": text},
        **fields,
    }
    return json.dumps(record) + "\n"


def _write_transcript(path: Path, *lines: str) -> Path:
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_a_channel_origin_line_carrying_the_pointer_confirms(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        _line("Message 3 is waiting in the ledger.", origin={"kind": "channel"}),
    )
    assert wake.transcript_confirms(path, 7, "Message 3 is waiting in the ledger.", PUSHED_AT)


def test_a_channel_tag_with_the_wakeup_id_confirms(tmp_path: Path) -> None:
    tag = (
        '<channel source="swarm-events" wakeup_id="7" reason="message_post">'
        "see the ledger</channel>"
    )
    path = _write_transcript(tmp_path / "t.jsonl", _line(tag))
    assert wake.transcript_confirms(path, 7, "unrelated pointer", PUSHED_AT)
    assert not wake.transcript_confirms(path, 8, "unrelated pointer", PUSHED_AT)


@pytest.mark.parametrize(
    "line",
    [
        _line("Message 3 is waiting in the ledger. (a channel note)", origin={"kind": "peer"}),
        _line(
            "Message 3 is waiting in the ledger.",
            origin={"kind": "channel"},
            at=PUSHED_AT - timedelta(minutes=5),
        ),
        _line("Message 3 is waiting in the ledger.", origin={"kind": "channel"}, isSidechain=True),
        json.dumps(
            {
                "type": "assistant",
                "origin": {"kind": "channel"},
                "message": {"content": "Message 3 is waiting in the ledger."},
            }
        )
        + "\n",
        "not json but channel\n",
    ],
)
def test_other_lines_do_not_confirm(tmp_path: Path, line: str) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", line)
    assert not wake.transcript_confirms(path, 7, "Message 3 is waiting in the ledger.", PUSHED_AT)


def test_a_missing_transcript_does_not_confirm(tmp_path: Path) -> None:
    assert not wake.transcript_confirms(tmp_path / "none.jsonl", 7, "p", PUSHED_AT)


def _push_to_oracle(ledger: Ledger, listing: list[dict], subscribe) -> dict:
    subscribe(ORACLE)
    _bootstrap(ledger, listing)
    ledger.message_post("manager-1", MANAGER, "oracle", "phase-1 is done")
    (wakeup,) = _wakeups(ledger)
    return wakeup


def _set_pushed_at(ledger: Ledger, when: datetime) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE wakeups SET pushed_at = ?", (watchdog.stamp(when),))


def _oracle_transcript(ledger: Ledger, tmp_path: Path, *lines: str) -> Path:
    path = _write_transcript(tmp_path / "oracle.jsonl", *lines)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET transcript_path = ? WHERE agent_id = ?", (str(path), ORACLE)
        )
    return path


def _channel_line(wakeup: dict) -> str:
    return _line(wakeup["pointer"], at=watchdog.utcnow(), origin={"kind": "channel"})


def test_confirm_marks_the_wake_up_sent_and_the_channel_confirmed(
    ledger: Ledger, listing, subscribe, tmp_path: Path
) -> None:
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    _oracle_transcript(ledger, tmp_path, _channel_line(wakeup))
    assert wake.confirm_pushed(ledger.conn) == [wakeup["wakeup_id"]]
    assert _wakeups(ledger)[0]["sent_at"] is not None
    assert _agent(ledger, ORACLE)["channel"] == "confirmed"


# -- the Stop hook ---------------------------------------------------------------------------


def test_stop_passes_once_a_fresh_push_is_confirmed(
    ledger: Ledger, listing, subscribe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    path = _oracle_transcript(ledger, tmp_path)
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        path.write_text(_channel_line(wakeup), encoding="utf-8")

    monkeypatch.setattr(wake.time, "sleep", nap)
    assert events.handle_stop(ledger, {"session_id": MANAGER}) is None
    assert naps == [wake.CONFIRM_POLL_S]
    assert _wakeups(ledger)[0]["sent_at"] is not None
    assert ledger.owed_wakeups(MANAGER) == []


def test_stop_passes_a_push_that_is_already_confirmed(
    ledger: Ledger, listing, subscribe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    _oracle_transcript(ledger, tmp_path, _channel_line(wakeup))
    monkeypatch.setattr(wake.time, "sleep", lambda seconds: pytest.fail("no wait expected"))
    assert events.handle_stop(ledger, {"session_id": MANAGER}) is None


def test_stop_blocks_an_old_unconfirmed_push_with_send_message(
    ledger: Ledger, listing, subscribe, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    _set_pushed_at(ledger, watchdog.utcnow() - timedelta(seconds=31))
    monkeypatch.setattr(wake.time, "sleep", lambda seconds: pytest.fail("no wait expected"))

    blocked = events.handle_stop(ledger, {"session_id": MANAGER})

    assert blocked is not None and blocked["decision"] == "block"
    send = f"SendMessage(to={json.dumps(ORACLE_SESSION)}, message={json.dumps(wakeup['pointer'])})"
    assert f"- {send}" in _untimed(blocked["reason"])
    assert "Nothing to send" not in blocked["reason"]
    assert _wakeups(ledger)[0]["sent_at"] is None


def test_stop_waits_out_the_window_then_blocks_a_push_that_never_arrives(
    ledger: Ledger, listing, subscribe, monkeypatch: pytest.MonkeyPatch
) -> None:
    _push_to_oracle(ledger, listing, subscribe)
    start = watchdog.utcnow()
    _set_pushed_at(ledger, start)
    elapsed = [0.0]
    monkeypatch.setattr(wake, "utcnow", lambda: start + timedelta(seconds=elapsed[0]))
    monkeypatch.setattr(
        wake.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    )

    blocked = events.handle_stop(ledger, {"session_id": MANAGER})

    assert blocked is not None and "SendMessage(to=" in blocked["reason"]
    assert elapsed[0] == wake.CONFIRM_WINDOW.total_seconds()


def test_the_send_message_fallback_clears_the_pushed_debt(
    ledger: Ledger, listing, subscribe
) -> None:
    _push_to_oracle(ledger, listing, subscribe)
    _set_pushed_at(ledger, watchdog.utcnow() - timedelta(minutes=5))
    events.handle_post_any(
        ledger,
        {"session_id": MANAGER, "tool_name": "SendMessage", "tool_input": {"to": ORACLE_SESSION}},
    )
    assert ledger.owed_wakeups(MANAGER) == []
    assert events.handle_stop(ledger, {"session_id": MANAGER}) is None


# -- the watchdog ----------------------------------------------------------------------------


def test_the_watchdog_reports_a_push_that_stays_unconfirmed(ledger: Ledger, listing, subscribe):
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    now = watchdog.utcnow()
    _set_pushed_at(ledger, now - timedelta(minutes=1))
    settings = load_settings(ledger.repo_root).watchdog
    kinds = [f.kind for f in watchdog.scan(ledger.conn, listing, now, settings)]
    assert "wake_unconfirmed" not in kinds

    _set_pushed_at(ledger, now - watchdog.UNCONFIRMED_AFTER)
    findings = [
        f
        for f in watchdog.scan(ledger.conn, listing, now, settings)
        if f.kind == "wake_unconfirmed"
    ]
    assert [(f.agent_id, f.name) for f in findings] == [(ORACLE, "oracle")]
    assert findings[0].next_step == f"Act on it: {wakeup['pointer']}"
    assert "from manager-1 (message_post)" in findings[0].detail


def test_a_watchdog_pass_confirms_a_push_before_it_scans(
    ledger: Ledger, listing, subscribe, tmp_path: Path
) -> None:
    wakeup = _push_to_oracle(ledger, listing, subscribe)
    _set_pushed_at(ledger, watchdog.utcnow() - timedelta(minutes=10))
    _oracle_transcript(
        ledger, tmp_path, _line(wakeup["pointer"], at=watchdog.utcnow(), origin={"kind": "channel"})
    )
    dog = watchdog.Watchdog(
        ledger.repo_root,
        ledger.conn,
        load_settings(ledger.repo_root).watchdog,
        exit_server=lambda: None,
        log=lambda text: None,
        transport="channel",
    )
    dog.tick(watchdog.utcnow())
    assert _wakeups(ledger)[0]["sent_at"] is not None
    kinds = [
        r["kind"] for r in ledger.conn.execute("SELECT kind FROM watchdog_findings").fetchall()
    ]
    assert "wake_unconfirmed" not in kinds


# -- the event stream and the bridge ---------------------------------------------------------


def test_the_event_stream_yields_events_and_pings_and_unregisters() -> None:
    hub = wake.EventHub()

    async def scenario() -> list[dict]:
        stream = wake.event_stream(hub, ORACLE, ping_s=0.05)
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        assert hub.connected(ORACLE)
        hub.push(ORACLE, {"kind": "wakeup", "content": "hi", "meta": {}})
        lines = [await first, await stream.__anext__()]
        await stream.aclose()
        return [json.loads(line) for line in lines]

    got = asyncio.run(scenario())
    assert got == [{"kind": "wakeup", "content": "hi", "meta": {}}, {"kind": "ping"}]
    assert not hub.connected(ORACLE)


def _bridge(out: io.BytesIO, url: str | None = None, **kwargs: Any) -> bridge.Bridge:
    return bridge.Bridge(out, lambda: url, log=lambda text: None, **kwargs)


def _sent(out: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_the_bridge_declares_the_channel_capability_and_no_tools() -> None:
    out = io.BytesIO()
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
    ]
    lines = [json.dumps(r).encode() + b"\n" for r in requests] + [b"not json\n"]
    assert bridge.serve_stdio(_bridge(out), lines) == 0

    init, tools, ping, unknown = _sent(out)
    assert init["result"]["capabilities"] == {"experimental": {"claude/channel": {}}}
    assert init["result"]["serverInfo"]["name"] == "swarm-events"
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert "swarm-events" in init["result"]["instructions"]
    assert tools["result"] == {"tools": []}
    assert ping["result"] == {}
    assert unknown["error"]["code"] == -32601


def test_the_bridge_turns_one_ledger_event_into_one_notification() -> None:
    out = io.BytesIO()
    link = _bridge(out)
    assert link.notify(
        {
            "kind": "wakeup",
            "content": "Handoff 1 is waiting.",
            "meta": {"wakeup_id": "4", "reason": "handoff_submit", "bad-key": "x"},
        }
    )
    assert not link.notify({"kind": "ping"})
    assert _sent(out) == [
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "content": "Handoff 1 is waiting.",
                "meta": {"wakeup_id": "4", "reason": "handoff_submit"},
            },
        }
    ]


class _Ledger(BaseHTTPRequestHandler):
    connections: list[str] = []
    batches = [
        [{"kind": "ping"}, {"kind": "wakeup", "content": "first", "meta": {"wakeup_id": "1"}}],
        [{"kind": "wakeup", "content": "second", "meta": {"wakeup_id": "2"}}],
    ]

    def do_GET(self) -> None:
        index = len(self.connections)
        self.connections.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for event in self.batches[index] if index < len(self.batches) else []:
            self.wfile.write(json.dumps(event).encode() + b"\n")
            self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def test_the_bridge_reconnects_after_the_ledger_drops_the_stream() -> None:
    _Ledger.connections = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ledger)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = io.BytesIO()
    url = f"http://127.0.0.1:{server.server_address[1]}/events?session=sess-1"
    link = _bridge(out, url, retry_s=0.01)
    try:
        link.start()
        deadline = time.monotonic() + 10
        while len(_sent(out)) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        link.stop()
        server.shutdown()
        server.server_close()
    assert [m["params"]["content"] for m in _sent(out)] == ["first", "second"]
    assert _Ledger.connections[:2] == ["/events?session=sess-1"] * 2


def test_the_bridge_starts_listening_only_after_initialized() -> None:
    calls: list[str] = []

    def url() -> str | None:
        calls.append("url")
        return None

    link = bridge.Bridge(io.BytesIO(), url, retry_s=0.01, log=lambda text: None)
    link.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    time.sleep(0.05)
    assert calls == []
    link.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    link.stop()
    assert calls


def test_events_url_reads_the_recorded_server(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert bridge.events_url(tmp_path, "sess 1") is None
    path = serve.server_info_path(tmp_path)
    path.parent.mkdir(parents=True)
    serve.write_server_info(path, {"url": "http://127.0.0.1:5123/mcp", "port": 5123, "pid": 1})
    assert bridge.events_url(tmp_path, "sess 1") == "http://127.0.0.1:5123/events?session=sess+1"


# -- launch ----------------------------------------------------------------------------------


def test_dev_channel_args_merge_into_one_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sessions.dev_channel_args() == []
    assert sessions.dev_channel_args(["server:swarm-events"]) == [
        "--dangerously-load-development-channels",
        "server:swarm-events",
    ]
    monkeypatch.setenv("CLAUDE_DEV_CHANNELS", "plugin:q@m server:swarm-events")
    assert sessions.dev_channel_args(["server:swarm-events"]) == [
        "--dangerously-load-development-channels",
        "plugin:q@m",
        "server:swarm-events",
    ]


def test_session_options_add_the_bridge_only_when_asked(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    setup.run_setup(tmp_path)

    def servers(**kwargs: Any) -> dict:
        options = agentfiles.session_options(tmp_path, "oracle", None, "http://x/mcp", **kwargs)
        return json.loads(options[options.index("--mcp-config") + 1])["mcpServers"]

    assert "swarm-events" not in servers()
    assert servers(channel=True)["swarm-events"] == {
        "command": "python",
        "args": [".sentinel-swarm/hook.py", "channel"],
    }


class _Recorder:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write(b"{}\n")
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def launch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", "claude-under-test")
    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    (config / ".claude.json").write_text(
        json.dumps({"projects": {repo.resolve().as_posix(): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(launch, "_ensure_server", lambda root: "http://127.0.0.1:5123/mcp")
    recorder = _Recorder()
    monkeypatch.setattr(launch.subprocess, "run", recorder)
    return {"repo": repo, "run": recorder}


def _launched(launch_env: dict, *flags: str) -> tuple[list[str], dict]:
    assert launch.main(["--repo", str(launch_env["repo"]), *flags, "Build it"]) == 0
    command = launch_env["run"].commands[-1]
    config = json.loads(command[command.index("--mcp-config") + 1])
    return command, config["mcpServers"]


def test_an_interactive_launch_carries_the_channel(
    launch_env: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    command, servers = _launched(launch_env)
    assert "swarm-events" in servers
    assert command[-2:] == ["--dangerously-load-development-channels", "server:swarm-events"]
    assert "I am using this for local development" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--bg"], ["--headless"]])
def test_a_background_or_print_launch_never_carries_the_channel(
    launch_env: dict, flags: list[str]
) -> None:
    command, servers = _launched(launch_env, *flags)
    assert "swarm-events" not in servers
    assert "--dangerously-load-development-channels" not in command


def test_the_sendmessage_setting_launches_without_the_channel(
    launch_env: dict, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.run_setup(launch_env["repo"])
    _write_settings(launch_env["repo"], "---\nwake_transport: sendmessage\n---\n")
    command, servers = _launched(launch_env)
    assert "swarm-events" not in servers
    assert "--dangerously-load-development-channels" not in command
    assert "development channel" not in capsys.readouterr().out


def test_a_spawned_role_never_carries_the_channel(ledger: Ledger) -> None:
    setup.run_setup(ledger.repo_root)
    options = agentfiles.session_options(ledger.repo_root, "coder", None, "http://x/mcp")
    assert "swarm-events" not in options[options.index("--mcp-config") + 1]
    assert "--dangerously-load-development-channels" not in options


# -- the shim --------------------------------------------------------------------------------


@pytest.fixture
def shim():
    spec = importlib.util.spec_from_file_location(
        "sentinel_swarm_hook_shim_wake", setup.SHIM_TEMPLATE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shim_runs_the_bridge_with_stdio_passed_through(shim, monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def ledger_command(repo: Path, module: str, *args: str):
        seen["module"] = module
        seen["args"] = args
        return ["uv", "run", module], {"X": "1"}

    def call(command: list[str], **kwargs: Any) -> int:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(shim, "ledger_command", ledger_command)
    monkeypatch.setattr(shim.subprocess, "call", call)
    assert shim.main(["channel"]) == 0
    assert seen["module"] == "swarm_ledger.bridge"
    assert seen["args"][0] == "--repo"
    assert "stdin" not in seen["kwargs"] and "stdout" not in seen["kwargs"]


def test_the_shim_reports_a_bridge_it_cannot_start(shim, monkeypatch, capsys) -> None:
    def broken(repo: Path, module: str, *args: str):
        raise shim.ShimError("uv is not on PATH")

    monkeypatch.setattr(shim, "ledger_command", broken)
    assert shim.main(["channel"]) == 1
    assert "swarm-events channel: uv is not on PATH" in capsys.readouterr().err


# -- the database ------------------------------------------------------------------------------


def test_an_older_ledger_gains_the_channel_and_pushed_at_columns(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE agents DROP COLUMN channel")
    raw.execute("ALTER TABLE wakeups DROP COLUMN pushed_at")
    raw.commit()
    raw.close()
    conn = connect(path)
    agents = {r["name"]: r for r in conn.execute("PRAGMA table_info(agents)")}
    wakeups = {r["name"] for r in conn.execute("PRAGMA table_info(wakeups)")}
    assert agents["channel"]["dflt_value"] == "'none'"
    assert "pushed_at" in wakeups
    conn.close()


# -- the ledger server -------------------------------------------------------------------------


@pytest.fixture
def served(host: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))
    monkeypatch.delenv("SENTINEL_SWARM_LEDGER_DB", raising=False)
    yield host
    info = serve.read_server_info(host)
    if info is not None:
        try:
            os.kill(int(info["pid"]), signal.SIGTERM)
        except OSError:
            pass


def test_the_server_streams_events_and_records_a_registered_oracle(served: Path) -> None:
    url = serve.ensure_server(served)
    base = url.removesuffix(serve.MCP_PATH)
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(f"{base}/events", timeout=10)
    assert refused.value.code == 400

    async def start_run() -> None:
        from fastmcp import Client

        async with Client(url) as client:
            await client.call_tool("run_start", {"prd": "Build X", "session_id": ORACLE})

    with urllib.request.urlopen(f"{base}/events?session={ORACLE}", timeout=30) as stream:
        assert stream.status == 200
        assert stream.headers["Content-Type"].startswith("application/x-ndjson")
        asyncio.run(start_run())
        conn = connect(served / ".sentinel-swarm" / "ledger.db")
        row = conn.execute("SELECT channel FROM agents WHERE agent_id = ?", (ORACLE,)).fetchone()
        conn.close()
    assert row["channel"] == "launched"
