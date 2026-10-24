"""SQL the hooks, the watchdog and the watch command share. Stdlib only."""

from __future__ import annotations

import sqlite3

LIVE_RUN_STATES = "state IN ('active', 'paused')"


def live_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM runs WHERE {LIVE_RUN_STATES} ORDER BY run_id DESC LIMIT 1"
    ).fetchone()


def latest_live_oracle(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()


def submitted_handoffs(
    conn: sqlite3.Connection, run_id: int, *, unpaused_only: bool = False
) -> list[sqlite3.Row]:
    paused = " AND p.paused_at IS NULL" if unpaused_only else ""
    return conn.execute(
        "SELECT h.handoff_id, f.path, coder.parent_agent_id AS reviewer_id FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "LEFT JOIN agents coder ON coder.agent_id = h.agent_id "
        f"WHERE p.run_id = ? AND h.state = 'submitted'{paused} ORDER BY h.handoff_id",
        (run_id,),
    ).fetchall()


def unread_count(conn: sqlite3.Connection, run_id: int, name: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE run_id = ? AND to_name = ? AND read_at IS NULL",
        (run_id, name),
    ).fetchone()["n"]
