from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .. import env, serve
from ..clock import parse_stamp, utcnow
from ..db import write_tx
from ..identity import SERVER_RESTART, SERVER_RESTART_COOLDOWN_S, SERVER_RESTART_LIMIT
from ..ledger import Ledger
from . import HANDLERS, events

_HISTORY = (
    "SELECT MAX(e.at) AS last, COUNT(*) AS n FROM agent_events e "
    "JOIN agents a ON a.agent_id = e.agent_id WHERE a.run_id = ? AND e.to_state = ?"
)


def _allowed(conn: sqlite3.Connection, run_id: int, now: datetime) -> bool:
    row = conn.execute(_HISTORY, (run_id, SERVER_RESTART)).fetchone()
    if row["n"] >= SERVER_RESTART_LIMIT:
        return False
    last = parse_stamp(row["last"])
    return last is None or now - last >= timedelta(seconds=SERVER_RESTART_COOLDOWN_S)


def _caller_in_active_run(ledger: Ledger, data: dict) -> dict | None:
    caller = events._swarm_caller(ledger, data.get("session_id"))
    if caller is None or caller["run_id"] is None:
        return None
    run = ledger.conn.execute(
        "SELECT state FROM runs WHERE run_id = ?", (caller["run_id"],)
    ).fetchone()
    return caller if run is not None and run["state"] == "active" else None


def _claim(ledger: Ledger, caller: dict, event_name: str, now: datetime) -> bool:
    # Checked again inside the transaction, so two concurrent fallbacks never both claim.
    with write_tx(ledger.conn) as conn:
        if not _allowed(conn, caller["run_id"], now):
            return False
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) VALUES (?, ?, ?, ?)",
            (caller["agent_id"], caller["state"], SERVER_RESTART, event_name),
        )
    return True


def claim_restart(root: Path, event_name: str, raw: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        return False
    ledger = env.open_ledger(root)
    try:
        caller = _caller_in_active_run(ledger, data)
        if caller is None or not _allowed(ledger.conn, caller["run_id"], now):
            return False
        # The probe goes last: a closed port costs about 2 s on Windows, and the shim waits.
        if serve.answering(root) is not None:
            return False
        return _claim(ledger, caller, event_name, now)
    finally:
        ledger.conn.close()


def restart_server(root: Path, event_name: str, raw: str) -> bool:
    if event_name not in HANDLERS or not claim_restart(root, event_name, raw):
        return False
    serve.spawn_server(root)
    return True
