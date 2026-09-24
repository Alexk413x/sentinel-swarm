from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_GIT_EXCLUDE_LINE = ".sentinel-swarm/"
_GITDIR_PREFIX = "gitdir:"
# What an older version 1 ledger may lack. schema.sql declares them too, for a new ledger.
_ADDED_COLUMNS = (
    ("agents", "session_name", "TEXT"),
    ("agents", "bg_id", "TEXT"),
    ("runs", "watch_heartbeat_at", "TEXT"),
    ("runs", "watch_expires_at", "TEXT"),
    ("runs", "watch_owner", "TEXT"),
    ("directives", "notified_at", "TEXT"),
    ("change_requests", "path", "TEXT"),
    ("change_requests", "decision_reason", "TEXT"),
    ("change_requests", "accepted_at", "TEXT"),
    ("change_requests", "completed_at", "TEXT"),
    ("change_requests", "completion_notes", "TEXT"),
    ("change_requests", "evidence_test_run_id", "INTEGER"),
    ("change_requests", "verified_at", "TEXT"),
    ("change_requests", "verify_notes", "TEXT"),
    ("change_requests", "decided_at", "TEXT"),
    ("departures", "file_id", "INTEGER"),
    ("departures", "handoff_id", "INTEGER"),
    ("departures", "decided_by", "TEXT"),
    ("departures", "decided_at", "TEXT"),
    ("departures", "decision_reason", "TEXT"),
    ("departures", "solution", "TEXT"),
)
_ADDED_TABLES = ("wakeups", "watchdog_findings")


def _main_git_dir(repo_root: Path) -> Path:
    git_path = repo_root / ".git"
    if git_path.is_file():
        line = git_path.read_text(encoding="utf-8").strip()
        if line.startswith(_GITDIR_PREFIX):
            gitdir = Path(line[len(_GITDIR_PREFIX) :].strip())
            if not gitdir.is_absolute():
                gitdir = (repo_root / gitdir).resolve()
            for parent in (gitdir, *gitdir.parents):
                if parent.name == ".git":
                    return parent
    return git_path


def ledger_path(repo_root: Path) -> Path:
    return _main_git_dir(repo_root).parent / ".sentinel-swarm" / "ledger.db"


def ensure_git_exclude(repo_root: Path) -> None:
    exclude_path = _main_git_dir(repo_root) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if _GIT_EXCLUDE_LINE in existing.splitlines():
        return
    with exclude_path.open("a", encoding="utf-8", newline="\n") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(_GIT_EXCLUDE_LINE + "\n")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The server runs tool calls on worker threads and serializes them with a lock.
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is not None:
        if row["version"] != 1:
            raise RuntimeError(f"unsupported ledger schema version {row['version']}")
        _upgrade(conn)
        return
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1)")


def _upgrade(conn: sqlite3.Connection) -> None:
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # Create any table a later version added before adding columns to it: an older
    # ledger may lack a whole table that _ADDED_COLUMNS also targets, not just a column.
    needed_tables = set(_ADDED_TABLES) | {table for table, _, _ in _ADDED_COLUMNS}
    if any(table not in tables for table in needed_tables):
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    for table, column, declaration in _ADDED_COLUMNS:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
