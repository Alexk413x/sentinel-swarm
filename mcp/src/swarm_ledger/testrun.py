from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import testing
from .clock import parse_stamp, stamp, utcnow
from .db import connect, write_tx
from .lock import pid_alive
from .pool import base_python, hidden_console_flags
from .settings import DEFAULT_TEST_TIMEOUT_S

LAUNCHER = Path(__file__).resolve().parents[2] / "launch" / "ledger.py"
LOG_DIR = "test-runs"
SERVER_LOG = "server.log"
WAKE_REASON = "test_run"
LAUNCH_GRACE_S = 60
OVERRUN_GRACE_S = 120
NO_RUNNER = "the test runner process ended without recording a result"


def log_path(db_path: Path, test_run_id: int) -> Path:
    return db_path.parent / LOG_DIR / f"{test_run_id}.log"


def runner_command(repo_root: Path, db_path: Path, test_run_id: int) -> list[str]:
    return [
        base_python(),
        "-I",
        "-S",
        str(LAUNCHER),
        "testrun",
        "--repo",
        str(repo_root),
        "--db",
        str(db_path),
        "--id",
        str(test_run_id),
    ]


def spawn(repo_root: Path, db_path: Path, test_run_id: int) -> int | None:
    log = db_path.parent / SERVER_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as errors:
        proc = subprocess.Popen(
            runner_command(repo_root, db_path, test_run_id),
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            creationflags=hidden_console_flags(new_process_group=True),
            start_new_session=sys.platform != "win32",
        )
    return proc.pid


launcher: Callable[[Path, Path, int], int | None] = spawn


def _outcome(row: sqlite3.Row) -> str:
    if row["state"] == "abandoned":
        return f"abandoned: {row['reason']}"
    if row["state"] == "stale":
        return f"not counted: {row['reason']}"
    counts = (
        f"{row['passed'] or 0} passed, {row['failed'] or 0} failed, {row['skipped'] or 0} skipped"
    )
    if row["reason"]:
        return f"failed ({row['reason']}): {counts}"
    return f"passed: {counts}"


def wake_pointer(row: sqlite3.Row) -> str:
    target = f" {row['target']}" if row["target"] else ""
    return (
        f"Test run {row['test_run_id']} ({row['scope']}{target}) is recorded, "
        f"{_outcome(row)}. Read it with test_run_get(test_run_id={row['test_run_id']})."
    )


def _owe_wakeup(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    agent = conn.execute(
        "SELECT * FROM agents WHERE agent_id = ? AND ended_at IS NULL", (row["agent_id"],)
    ).fetchone()
    if agent is None or not agent["session_name"]:
        return
    conn.execute(
        "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
        "reason, pointer) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["run_id"],
            agent["agent_id"],
            agent["agent_id"],
            agent["name"],
            agent["session_name"],
            WAKE_REASON,
            wake_pointer(row),
        ),
    )


def _finish(conn: sqlite3.Connection, test_run_id: int, columns: dict[str, object]) -> bool:
    with write_tx(conn):
        sets = ", ".join(f"{name} = ?" for name in columns)
        cur = conn.execute(
            f"UPDATE test_runs SET {sets}, finished_at = ? "
            "WHERE test_run_id = ? AND state = 'running'",
            (*columns.values(), stamp(utcnow()), test_run_id),
        )
        if not cur.rowcount:
            return False
        row = conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ?", (test_run_id,)
        ).fetchone()
        _owe_wakeup(conn, row)
    return True


def record(
    conn: sqlite3.Connection, test_run_id: int, result: testing.TestResult, stale: str | None
) -> bool:
    return _finish(
        conn,
        test_run_id,
        {
            "state": "stale" if stale else "done",
            "reason": stale or result.reason,
            "exit_code": result.exit_code,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "errors": result.errors,
            "allowed_skipped": result.allowed_skipped,
            "output": result.output,
            "duration_ms": result.duration_ms,
        },
    )


def abandon(conn: sqlite3.Connection, test_run_id: int, reason: str = NO_RUNNER) -> bool:
    return _finish(conn, test_run_id, {"state": "abandoned", "reason": reason})


def _changed_tree(repo_root: Path, row: sqlite3.Row) -> str | None:
    if row["scope"] != "full" or row["fingerprint"] is None:
        return None
    claimed = json.loads(row["claimed_json"] or "[]")
    if testing.tree_fingerprint(repo_root, claimed) == row["fingerprint"]:
        return None
    return (
        "the working tree changed while the full run ran, so its result does not describe "
        "one tree; run it again while no agent edits"
    )


def run(repo_root: Path, db_path: Path, test_run_id: int) -> None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ? AND state = 'running'", (test_run_id,)
        ).fetchone()
        if row is None:
            return
        log = log_path(db_path, test_run_id)
        executed = testing.execute(row["command"], repo_root, row["timeout_s"], log)
        patterns = json.loads(row["skip_patterns_json"] or "[]")
        result = testing.result_of(row["command"], executed, patterns)
        record(conn, test_run_id, result, _changed_tree(repo_root, row))
        log.unlink(missing_ok=True)
    finally:
        conn.close()


def _runner_gone(row: sqlite3.Row, now: datetime) -> bool:
    created = parse_stamp(row["created_at"])
    age = (now - created).total_seconds() if created is not None else 0.0
    if age > (row["timeout_s"] or DEFAULT_TEST_TIMEOUT_S) + OVERRUN_GRACE_S:
        return True
    if row["runner_pid"] is None:
        return age > LAUNCH_GRACE_S
    return not pid_alive(row["runner_pid"])


def sweep(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    moment = now or utcnow()
    gone = [
        row["test_run_id"]
        for row in conn.execute("SELECT * FROM test_runs WHERE state = 'running'").fetchall()
        if _runner_gone(row, moment)
    ]
    return [test_run_id for test_run_id in gone if abandon(conn, test_run_id)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py testrun")
    parser.add_argument("--repo", type=Path, required=True, help="the host repo root")
    parser.add_argument("--db", type=Path, required=True, help="the ledger database")
    parser.add_argument("--id", type=int, required=True, help="the test_run_id to run")
    args = parser.parse_args(argv)
    run(args.repo.resolve(), args.db, args.id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
