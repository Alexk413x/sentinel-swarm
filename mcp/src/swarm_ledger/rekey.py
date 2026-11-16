from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import sessions
from .db import NOW, write_tx
from .queries import UNPARK_SET

# Session-id columns that neither end in agent_id nor reference agents.
SESSION_COLUMNS = frozenset({("mod_sessions", "session_id")})
RESUME_WINDOW_S = 120


def claim_resume(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Take the one claim every resume path holds: the watchdog, the wake-up path and agent_resume.

    The claim is a fresh `resume_started_at`, set by one conditional UPDATE, so two actors
    never both win it. It lapses after the window.
    """
    return bool(
        conn.execute(
            f"UPDATE agents SET resume_started_at = {NOW} WHERE agent_id = ? "
            "AND ended_at IS NULL AND (resume_started_at IS NULL OR resume_started_at < "
            f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-{RESUME_WINDOW_S} seconds'))",
            (agent_id,),
        ).rowcount
    )


def release_resume(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute("UPDATE agents SET resume_started_at = NULL WHERE agent_id = ?", (agent_id,))


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


def prior_ids(raw: object) -> list[str]:
    try:
        ids = json.loads(raw) if isinstance(raw, str) and raw else []
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def adopt(conn: sqlite3.Connection, old_id: str, new_id: str) -> bool:
    row = conn.execute(
        "SELECT transcript_path, resume_from_id, prior_session_ids FROM agents WHERE agent_id = ?",
        (old_id,),
    ).fetchone()
    if row is None:
        return False
    resume_from = row["resume_from_id"]
    if old_id != new_id and row["transcript_path"] and Path(row["transcript_path"]).is_file():
        resume_from = old_id
    prior = prior_ids(row["prior_session_ids"])
    rekeyed = rekey(conn, old_id, new_id)
    if rekeyed:
        if old_id not in prior:
            prior.append(old_id)
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES (?, NULL, 'rekeyed', ?)",
            (new_id, f"resumed from {old_id}"),
        )
    transcript = row["transcript_path"]
    if transcript and Path(transcript).stem == old_id:
        transcript = str(Path(transcript).with_name(f"{new_id}.jsonl"))
    conn.execute(
        f"UPDATE agents SET resume_started_at = NULL, {UNPARK_SET}, "
        "session_pid = NULL, session_pid_created = NULL, bg_id = ?, transcript_path = ?, "
        "resume_from_id = ?, prior_session_ids = ? WHERE agent_id = ?",
        (new_id[:8], transcript, resume_from, json.dumps(prior) if prior else None, new_id),
    )
    return rekeyed


def stop_earlier_sessions(conn: sqlite3.Connection, agent_id: str) -> list[str]:
    row = conn.execute(
        "SELECT bg_id, prior_session_ids FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        return []
    stopped = sessions.stop_listed(prior_ids(row["prior_session_ids"]), keep_bg_id=row["bg_id"])
    if stopped:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
                "VALUES (?, NULL, 'earlier_session_stopped', ?)",
                (agent_id, "; ".join(stopped)),
            )
    return stopped
