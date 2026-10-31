from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import env
from .clock import stamp, utcnow
from .db import row_dict, write_tx
from .identity import LedgerError

OPEN = "open"
ANSWERS = {True: "allowed", False: "denied"}
CLOSES = ("expired", "withdrawn")
ANSWERED_BY = "run panel"
WAIT_S = 25.0
POLL_S = 0.5
SUMMARY_CHARS = 300
_NAMED_INPUTS = ("file_path", "command", "path", "url")


def input_text(tool_input: object) -> str:
    fields = tool_input if isinstance(tool_input, dict) else {}
    for key in _NAMED_INPUTS:
        if fields.get(key):
            return str(fields[key])
    return json.dumps(fields, ensure_ascii=False)


def one_line(text: str, limit: int = SUMMARY_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _row(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM permission_requests WHERE request_id = ?", (request_id,)
    ).fetchone()


def open_request(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    agent_id: str,
    tool: str,
    tool_input: object,
    reason: str | None,
    hold_seconds: int,
    now: datetime,
) -> dict:
    with write_tx(conn):
        cur = conn.execute(
            "INSERT INTO permission_requests (run_id, agent_id, tool, summary, reason, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                agent_id,
                tool,
                one_line(input_text(tool_input)),
                one_line(reason) if reason else None,
                stamp(now),
                stamp(now + timedelta(seconds=hold_seconds)),
            ),
        )
    return row_dict(conn, "permission_requests", "request_id", cur.lastrowid)


def close(
    conn: sqlite3.Connection, request_id: int, agent_id: str, state: str, now: datetime
) -> bool:
    if state not in CLOSES:
        raise ValueError(f"a request closes as one of {CLOSES}, not {state!r}")
    with write_tx(conn):
        cur = conn.execute(
            "UPDATE permission_requests SET state = ?, answered_at = ? "
            "WHERE request_id = ? AND agent_id = ? AND state = 'open'",
            (state, stamp(now), request_id, agent_id),
        )
    return cur.rowcount > 0


def current(conn: sqlite3.Connection, request_id: int, agent_id: str, now: datetime) -> dict | None:
    row = _row(conn, request_id)
    if row is None or row["agent_id"] != agent_id:
        return None
    if row["state"] == OPEN and row["expires_at"] <= stamp(now):
        close(conn, request_id, agent_id, "expired", now)
        row = _row(conn, request_id)
    return dict(row) if row is not None else None


def wait_answer(
    conn: sqlite3.Connection,
    request_id: int,
    agent_id: str,
    wait_s: float = WAIT_S,
    *,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    deadline = time.monotonic() + wait_s
    while True:
        row = current(conn, request_id, agent_id, clock())
        left = deadline - time.monotonic()
        if row is None or row["state"] != OPEN or left <= 0:
            return row
        sleep(min(POLL_S, left))


def answer(
    conn: sqlite3.Connection, request_id: int, allow: bool, note: str | None, now: datetime
) -> dict:
    at = stamp(now)
    refusal: str | None = None
    with write_tx(conn):
        row = _row(conn, request_id)
        if row is None:
            refusal = f"no permission request {request_id}"
        elif row["state"] == OPEN and row["expires_at"] <= at:
            conn.execute(
                "UPDATE permission_requests SET state = 'expired', answered_at = ? "
                "WHERE request_id = ?",
                (at, request_id),
            )
            refusal = (
                f"permission request {request_id} expired at {row['expires_at']}; its session "
                "shows its own prompt: answer it in agent view"
            )
        elif row["state"] != OPEN:
            refusal = f"permission request {request_id} is {row['state']}, not open"
        else:
            conn.execute(
                "UPDATE permission_requests SET state = ?, answered_at = ?, answered_by = ?, "
                "note = ? WHERE request_id = ?",
                (ANSWERS[allow], at, ANSWERED_BY, note or None, request_id),
            )
    if refusal is not None:
        raise LedgerError(refusal)
    return row_dict(conn, "permission_requests", "request_id", request_id)


def open_requests(conn: sqlite3.Connection, run_id: int, now: datetime) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT p.*, a.name, a.session_name FROM permission_requests p "
            "JOIN agents a ON a.agent_id = p.agent_id "
            "WHERE p.run_id = ? AND p.state = 'open' AND p.expires_at > ? "
            "AND a.ended_at IS NULL ORDER BY p.request_id",
            (run_id, stamp(now)),
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledger.py permission",
        description="Answer a role session's held permission request from the run panel.",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--id", type=int, required=True, help="the permission request")
    verdict = parser.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--allow", action="store_true", help="let the call run")
    verdict.add_argument("--deny", action="store_true", help="refuse the call")
    parser.add_argument("--note", default=None, help="a note the session reads on a deny")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    ledger = env.open_ledger(root)
    try:
        row = answer(ledger.conn, args.id, args.allow, args.note, utcnow())
    except LedgerError as exc:
        sys.stderr.write(f"cannot answer the permission request: {exc}\n")
        return 1
    finally:
        ledger.conn.close()
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
