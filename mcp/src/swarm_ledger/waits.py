from __future__ import annotations

import sqlite3

from .db import NOW, dict_rows, row_dict, write_tx
from .host import LedgerHost
from .identity import Caller, LedgerError, require_role, resolve

LANDED_STATES = ("approved", "incomplete")

_LATEST_FILE_STATE = (
    "SELECT f.state FROM files f JOIN modules m ON m.module_id = f.module_id "
    "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = w.run_id "
    "AND f.path = w.path ORDER BY f.file_id DESC LIMIT 1"
)


def latest_file(conn: sqlite3.Connection, run_id: int | None, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ? AND f.path = ? "
        "ORDER BY f.file_id DESC LIMIT 1",
        (run_id, path),
    ).fetchone()


def open_file_waits(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    return dict_rows(
        conn.execute(
            "SELECT w.wait_id, a.name AS waiter, a.role, w.path, w.reason, "
            f"w.created_at, ({_LATEST_FILE_STATE}) AS file_state "
            "FROM file_waits w JOIN agents a ON a.agent_id = w.agent_id "
            "WHERE w.run_id = ? AND w.closed_at IS NULL AND a.ended_at IS NULL "
            "ORDER BY w.wait_id",
            (run_id,),
        )
    )


def landed_waits(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    return dict_rows(
        conn.execute(
            "SELECT w.wait_id, w.agent_id, w.path, w.reason, "
            f"({_LATEST_FILE_STATE}) AS file_state "
            "FROM file_waits w JOIN agents a ON a.agent_id = w.agent_id "
            "WHERE w.run_id = ? AND w.closed_at IS NULL AND a.ended_at IS NULL "
            "AND file_state IN ('approved', 'incomplete') ORDER BY w.wait_id",
            (run_id,),
        )
    )


class WaitsMixin(LedgerHost):
    def wait_for(self, caller: str, agent_id: str, path: str, reason: str) -> dict:
        if not reason.strip():
            raise LedgerError("wait_for needs a reason: what you cannot do until the file lands")
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "coder", "lead")
            file_row = latest_file(conn, c.run_id, path)
            if file_row is None:
                raise LedgerError(
                    f"no module plans {path!r}; ask your parent to plan it before you wait on it"
                )
            if c.role == "coder" and file_row["file_id"] == c.file_id:
                raise LedgerError(f"{path!r} is your own file; hand it off instead of waiting")
            if c.role == "lead" and file_row["module_id"] == c.module_id:
                raise LedgerError(
                    f"{path!r} belongs to your module: its Coder's handoff wakes you, so "
                    "review it when it arrives"
                )
            if file_row["state"] in LANDED_STATES:
                return {
                    "waiting": False,
                    "path": path,
                    "file_state": file_row["state"],
                    "note": f"{path} is already {file_row['state']}. Carry on; do not wait.",
                }
            wait = conn.execute(
                "SELECT wait_id FROM file_waits WHERE agent_id = ? AND path = ? "
                "AND closed_at IS NULL",
                (c.agent_id, path),
            ).fetchone()
            if wait is None:
                cur = conn.execute(
                    "INSERT INTO file_waits (run_id, agent_id, path, reason) VALUES (?, ?, ?, ?)",
                    (c.run_id, c.agent_id, path, reason),
                )
                wait_id = cur.lastrowid
            else:
                wait_id = wait["wait_id"]
                conn.execute(
                    "UPDATE file_waits SET reason = ? WHERE wait_id = ?", (reason, wait_id)
                )

        result = row_dict(self.conn, "file_waits", "wait_id", wait_id)
        result["waiting"] = True
        result["file_state"] = file_row["state"]
        result["note"] = (
            f"End your turn. The Lead that approves {path} owes you a wake-up, and the "
            "ledger names it when the file lands."
        )
        return result

    def _wake_file_waiters(
        self, conn: sqlite3.Connection, c: Caller, path: str, state: str
    ) -> list[dict]:
        waits = conn.execute(
            "SELECT w.agent_id, w.reason FROM file_waits w "
            "JOIN agents a ON a.agent_id = w.agent_id "
            "WHERE w.run_id = ? AND w.path = ? AND w.closed_at IS NULL AND a.ended_at IS NULL "
            "ORDER BY w.wait_id",
            (c.run_id, path),
        ).fetchall()
        conn.execute(
            f"UPDATE file_waits SET closed_at = {NOW} WHERE run_id = ? AND path = ? "
            "AND closed_at IS NULL",
            (c.run_id, path),
        )
        landed = "approved" if state == "approved" else "accepted as incomplete"
        wakeups = []
        for wait in waits:
            wakeup = self._owe_wakeup(
                conn,
                c,
                wait["agent_id"],
                "file_wait",
                f"{path} was {landed} by {c.name}; you waited on it ({wait['reason']}). "
                "Carry on with that work.",
            )
            if wakeup is not None:
                wakeups.append(wakeup)
        return wakeups
