from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from .db import NOW

SETTLED_STATES = ("approved", "incomplete", "superseded")
# A deferral of one of these kinds, once agreed, moves a file's remaining work out of the
# plan, so the file no longer holds its module open. A file or module deferral does not:
# it trims the file's task while its Coder keeps working.
DEFERRING_KINDS = ("phase", "plan", "prd")


def deferred(conn: sqlite3.Connection, file_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deferrals WHERE file_id = ? AND state = 'agreed' "
        f"AND kind IN ({', '.join('?' for _ in DEFERRING_KINDS)}) LIMIT 1",
        (file_id, *DEFERRING_KINDS),
    ).fetchone()
    return row is not None


def stray_release(repo_root: Path, row: sqlite3.Row | dict) -> bool:
    return row["state"] == "released" and not (repo_root / row["path"]).exists()


def file_settled(conn: sqlite3.Connection, repo_root: Path, row: sqlite3.Row | dict) -> bool:
    if row["state"] in SETTLED_STATES or stray_release(repo_root, row):
        return True
    return deferred(conn, row["file_id"])


def unsettled_files(conn: sqlite3.Connection, repo_root: Path, module_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM files WHERE module_id = ? ORDER BY file_id", (module_id,)
        )
        if not file_settled(conn, repo_root, row)
    ]


def close_settled(conn: sqlite3.Connection, repo_root: Path, module_id: int) -> None:
    for row in conn.execute(
        "SELECT * FROM files WHERE module_id = ? AND state NOT IN "
        f"({', '.join('?' for _ in SETTLED_STATES)}) ORDER BY file_id",
        (module_id, *SETTLED_STATES),
    ).fetchall():
        if stray_release(repo_root, row):
            conn.execute(
                "UPDATE files SET state = 'superseded' WHERE file_id = ?", (row["file_id"],)
            )
        elif deferred(conn, row["file_id"]):
            conn.execute(
                "UPDATE files SET state = 'incomplete', "
                f"released_at = COALESCE(released_at, {NOW}) WHERE file_id = ?",
                (row["file_id"],),
            )
            sync_products(conn, row["file_id"], "incomplete", released="set")


def sync_products(
    conn: sqlite3.Connection,
    source_file_id: int,
    state: str,
    *,
    released: Literal["keep", "set", "clear"] = "keep",
) -> None:
    released_sql = {
        "keep": "released_at",
        "set": f"COALESCE(released_at, {NOW})",
        "clear": "NULL",
    }[released]
    conn.execute(
        f"UPDATE files SET state = ?, released_at = {released_sql} "
        "WHERE source_file_id = ? AND state != 'superseded'",
        (state, source_file_id),
    )
