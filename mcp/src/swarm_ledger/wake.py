from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .agentfiles import CHANNEL_SERVER
from .clock import parse_stamp, stamp, utcnow
from .db import write_tx

CHANNEL_ENTRY = f"server:{CHANNEL_SERVER}"
EVENTS_PATH = "/events"
WAKEUP_EVENT = "wakeup"
PING_EVENT = "ping"
PING_S = 15.0
CONFIRM_WINDOW = timedelta(seconds=30)
CONFIRM_POLL_S = 1.0
# A transcript line can be stamped a moment before the ledger records the push.
_CLOCK_SKEW = timedelta(seconds=5)

Kind = Literal["pushed", "send", "resume", "send_or_resume"]


@dataclass(frozen=True)
class Delivery:
    kind: Kind
    target: str
    send: str | None
    resume: str

    @property
    def pushed(self) -> bool:
        return self.kind == "pushed"

    @property
    def next(self) -> str:
        if self.kind == "pushed":
            return (
                f"Nothing to send: the ledger delivered this wake-up to {self.target} "
                "through its channel."
            )
        if self.kind == "send" and self.send is not None:
            return self.send
        if self.kind == "send_or_resume" and self.send is not None:
            return f"{self.send}, or {self.resume} if that session is not running"
        return self.resume


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[
            str, dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]]
        ] = {}
        self._next_token = 0

    def subscribe(
        self, session_id: str, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue
    ) -> int:
        with self._lock:
            self._next_token += 1
            self._subscribers.setdefault(session_id, {})[self._next_token] = (loop, queue)
            return self._next_token

    def unsubscribe(self, session_id: str, token: int) -> None:
        with self._lock:
            subscribers = self._subscribers.get(session_id, {})
            subscribers.pop(token, None)
            if not subscribers:
                self._subscribers.pop(session_id, None)

    def connected(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._subscribers.get(session_id))

    def push(self, session_id: str, event: Mapping[str, Any]) -> bool:
        with self._lock:
            subscribers = list(self._subscribers.get(session_id, {}).items())
        delivered = False
        for token, (loop, queue) in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, dict(event))
            except RuntimeError:
                self.unsubscribe(session_id, token)
                continue
            delivered = True
        return delivered


HUB = EventHub()


def wakeup_event(wakeup: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": WAKEUP_EVENT,
        "content": str(wakeup["pointer"]),
        "meta": {"wakeup_id": str(wakeup["wakeup_id"]), "reason": str(wakeup["reason"])},
    }


def _calls(wakeup: Mapping[str, Any]) -> tuple[str | None, str]:
    session_name = wakeup.get("to_session_name")
    send = None
    if session_name:
        pointer = wakeup.get("pointer")
        message = f", message={json.dumps(pointer)}" if pointer else ""
        send = f"SendMessage(to={json.dumps(session_name)}{message})"
    return send, f"agent_resume(target_name={json.dumps(wakeup['to_name'])})"


def route_wakeup(
    wakeup: Mapping[str, Any],
    *,
    transport: str,
    live: bool | None,
    channel: str = "none",
    hub: EventHub | None = None,
) -> Delivery:
    send, resume = _calls(wakeup)
    target = str(wakeup["to_name"])

    def instruction() -> Delivery:
        if send is None or live is False:
            return Delivery("resume", target, send, resume)
        return Delivery("send" if live else "send_or_resume", target, send, resume)

    if transport != "channel" or live is False:
        return instruction()
    if (
        hub is not None
        and channel in ("launched", "confirmed")
        and wakeup.get("wakeup_id") is not None
        and hub.push(str(wakeup["to_agent_id"]), wakeup_event(wakeup))
    ):
        return Delivery("pushed", target, send, resume)
    return instruction()


def fallback(wakeup: Mapping[str, Any], live: bool | None) -> Delivery:
    return route_wakeup(wakeup, transport="sendmessage", live=live)


async def event_stream(
    hub: EventHub, session_id: str, *, ping_s: float = PING_S
) -> AsyncGenerator[bytes, None]:
    queue: asyncio.Queue = asyncio.Queue()
    token = hub.subscribe(session_id, asyncio.get_running_loop(), queue)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), ping_s)
            except asyncio.TimeoutError:
                event = {"kind": PING_EVENT}
            yield (json.dumps(event) + "\n").encode("utf-8")
    finally:
        hub.unsubscribe(session_id, token)


def mark_launched(conn: sqlite3.Connection, session_id: str) -> None:
    with write_tx(conn):
        conn.execute(
            "UPDATE agents SET channel = 'launched' WHERE agent_id = ? AND channel = 'none'",
            (session_id,),
        )


def _text_of(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _from_channel(record: dict, text: str) -> bool:
    origin = record.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "channel":
        return True
    if "channel" in (record.get("turnOrigin"), record.get("promptSource")):
        return True
    return f'<channel source="{CHANNEL_SERVER}"' in text


def transcript_confirms(
    path: Path, wakeup_id: int, pointer: str, pushed_at: datetime | None = None
) -> bool:
    marker = f'wakeup_id="{wakeup_id}"'
    earliest = pushed_at - _CLOCK_SKEW if pushed_at is not None else None
    try:
        with path.open("rb") as handle:
            for raw in handle:
                if b"channel" not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "user":
                    continue
                if record.get("isSidechain"):
                    continue
                when = parse_stamp(record.get("timestamp"))
                if earliest is not None and when is not None and when < earliest:
                    continue
                text = _text_of(record.get("message"))
                if _from_channel(record, text) and (marker in text or pointer in text):
                    return True
    except OSError:
        return False
    return False


def _transcript_of(conn: sqlite3.Connection, agent_id: str) -> Path | None:
    row = conn.execute(
        "SELECT transcript_path FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return Path(row["transcript_path"]) if row is not None and row["transcript_path"] else None


def confirm(conn: sqlite3.Connection, wakeup: Mapping[str, Any]) -> bool:
    path = _transcript_of(conn, str(wakeup["to_agent_id"]))
    if path is None or not transcript_confirms(
        path, int(wakeup["wakeup_id"]), str(wakeup["pointer"]), parse_stamp(wakeup["pushed_at"])
    ):
        return False
    with write_tx(conn):
        conn.execute(
            "UPDATE wakeups SET sent_at = COALESCE(sent_at, ?) WHERE wakeup_id = ?",
            (stamp(utcnow()), wakeup["wakeup_id"]),
        )
        conn.execute(
            "UPDATE agents SET channel = 'confirmed' WHERE agent_id = ?", (wakeup["to_agent_id"],)
        )
    return True


def confirm_pushed(conn: sqlite3.Connection) -> list[int]:
    pending = conn.execute(
        "SELECT * FROM wakeups WHERE pushed_at IS NOT NULL AND sent_at IS NULL ORDER BY wakeup_id"
    ).fetchall()
    return [row["wakeup_id"] for row in pending if confirm(conn, dict(row))]


def await_confirmation(
    conn: sqlite3.Connection,
    pushed: list[dict],
    *,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    window: timedelta = CONFIRM_WINDOW,
) -> list[dict]:
    waiting = list(pushed)
    while waiting:
        waiting = [w for w in waiting if not confirm(conn, w)]
        now = (clock or utcnow)()
        young = [
            w
            for w in waiting
            if (at := parse_stamp(w["pushed_at"])) is not None and now - at < window
        ]
        if not young:
            return waiting
        (sleep or time.sleep)(CONFIRM_POLL_S)
    return waiting
