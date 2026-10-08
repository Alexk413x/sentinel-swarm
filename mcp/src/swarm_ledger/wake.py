from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .clock import parse_stamp, utcnow

# Only the Opus leads get the elapsed time: Sonnet can read text appended to a message as a
# possible injection (knowledge/prd/05-sessions.md, "Time signal").
TIMED_ROLES = ("oracle", "manager")

Kind = Literal["send", "resume", "send_or_resume"]


@dataclass(frozen=True)
class Delivery:
    kind: Kind
    target: str
    send: str | None
    resume: str

    @property
    def next(self) -> str:
        if self.kind == "send" and self.send is not None:
            return self.send
        if self.kind == "send_or_resume" and self.send is not None:
            return f"{self.send}, or {self.resume} if that session is not running"
        return self.resume


def time_signal(
    conn: sqlite3.Connection, run_id: int | None, now: datetime | None = None
) -> str | None:
    run = conn.execute(
        "SELECT started_at, settings_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    started = parse_stamp(run["started_at"]) if run is not None else None
    if run is None or started is None:
        return None
    elapsed = max(0, int(((now or utcnow()) - started).total_seconds()))
    try:
        budget = json.loads(run["settings_json"] or "{}").get("time_budget_minutes")
    except (ValueError, AttributeError):
        budget = None
    if isinstance(budget, int) and budget > 0:
        return f"elapsed {elapsed}s / {budget * 60}s"
    return f"elapsed {elapsed}s"


def signal_for(
    conn: sqlite3.Connection, agent_id: str | None, now: datetime | None = None
) -> str | None:
    agent = conn.execute(
        "SELECT run_id, role FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if agent is None or agent["role"] not in TIMED_ROLES:
        return None
    return time_signal(conn, agent["run_id"], now)


def timed(text: str, signal: str | None) -> str:
    return f"{text} {signal}" if text and signal else text


def _calls(wakeup: Mapping[str, Any], signal: str | None = None) -> tuple[str | None, str]:
    session_name = wakeup.get("to_session_name")
    send = None
    if session_name:
        pointer = timed(str(wakeup.get("pointer") or ""), signal)
        message = f", message={json.dumps(pointer)}" if pointer else ""
        send = f"SendMessage(to={json.dumps(session_name)}{message})"
    return send, f"agent_resume(target_name={json.dumps(wakeup['to_name'])})"


def route_wakeup(
    wakeup: Mapping[str, Any], *, live: bool | None, signal: str | None = None
) -> Delivery:
    send, resume = _calls(wakeup, signal)
    target = str(wakeup["to_name"])
    if send is None or live is False:
        return Delivery("resume", target, send, resume)
    return Delivery("send" if live else "send_or_resume", target, send, resume)
