from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from . import env
from .db import connect, write_tx
from .watchdog import stamp, utcnow

POLL_SECONDS = 2.0


def _live_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE state IN ('active', 'paused') ORDER BY run_id DESC LIMIT 1"
    ).fetchone()


def claim(conn: sqlite3.Connection, owner: str) -> None:
    with write_tx(conn):
        run = _live_run(conn)
        if run is not None:
            conn.execute("UPDATE runs SET watch_owner = ? WHERE run_id = ?", (owner, run["run_id"]))


def poll(
    conn: sqlite3.Connection, now: datetime, owner: str | None = None
) -> tuple[bool, list[str]]:
    run = _live_run(conn)
    if run is None or run["state"] != "active":
        return False, []
    # Windows does not kill a killed Monitor's child processes, so an expired listener can
    # outlive its Monitor. The newest listener owns the run; an older one exits instead of
    # taking the directives meant for the listener the Oracle re-armed.
    if owner is not None and run["watch_owner"] != owner:
        return False, []
    at = stamp(now)
    with write_tx(conn):
        conn.execute("UPDATE runs SET watch_heartbeat_at = ? WHERE run_id = ?", (at, run["run_id"]))
        rows = conn.execute(
            "SELECT directive_id, body FROM directives WHERE run_id = ? AND source = 'watchdog' "
            "AND state = 'open' AND notified_at IS NULL ORDER BY directive_id",
            (run["run_id"],),
        ).fetchall()
        conn.executemany(
            "UPDATE directives SET notified_at = ? WHERE directive_id = ?",
            [(at, row["directive_id"]) for row in rows],
        )
    return True, [
        f"Watchdog directive {row['directive_id']}: {' '.join(row['body'].split())}" for row in rows
    ]


def watch(
    conn: sqlite3.Connection,
    out: TextIO,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = utcnow,
    owner: str | None = None,
) -> int:
    owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    claim(conn, owner)
    while True:
        keep, lines = poll(conn, clock(), owner)
        for line in lines:
            out.write(line + "\n")
            out.flush()
        if not keep:
            return 0
        sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarm_ledger.watch")
    parser.add_argument("--repo", type=Path, default=None, help="the host repo root")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    conn = connect(env.db_path_for(root))
    try:
        return watch(conn, sys.stdout)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
