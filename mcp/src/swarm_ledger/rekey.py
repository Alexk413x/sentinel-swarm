from __future__ import annotations

import sqlite3
from pathlib import Path

# Session-id columns that neither end in agent_id nor reference agents.
SESSION_COLUMNS = frozenset({("mod_sessions", "session_id")})


def agent_id_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    columns: set[tuple[str, str]] = set(SESSION_COLUMNS)
    tables = [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        for column in conn.execute(f"PRAGMA table_info({table})"):
            if column["name"].endswith("agent_id"):
                columns.add((table, column["name"]))
        for key in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if key["table"] == "agents":
                columns.add((table, key["from"]))
    return sorted(columns)


def rekey(conn: sqlite3.Connection, old_id: str, new_id: str) -> bool:
    if old_id == new_id:
        return False
    if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (old_id,)).fetchone() is None:
        return False
    if conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (new_id,)).fetchone() is not None:
        return False
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for table, column in agent_id_columns(conn):
        conn.execute(
            f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?", (new_id, old_id)
        )
    conn.execute("DELETE FROM mod_sessions WHERE session_id = ?", (old_id,))
    return True


def adopt(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    row = conn.execute(
        "SELECT transcript_path FROM agents WHERE agent_id = ?", (old_id,)
    ).fetchone()
    if row is None:
        return
    if rekey(conn, old_id, new_id):
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES (?, NULL, 'rekeyed', ?)",
            (new_id, f"resumed from {old_id}"),
        )
    transcript = row["transcript_path"]
    if transcript and Path(transcript).stem == old_id:
        transcript = str(Path(transcript).with_name(f"{new_id}.jsonl"))
    conn.execute(
        "UPDATE agents SET resume_started_at = NULL, bg_id = ?, transcript_path = ? "
        "WHERE agent_id = ?",
        (new_id[:8], transcript, new_id),
    )
