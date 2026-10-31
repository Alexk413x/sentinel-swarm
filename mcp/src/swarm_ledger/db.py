from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_GIT_EXCLUDE_LINE = ".sentinel-swarm/"
_GITDIR_PREFIX = "gitdir:"


def main_git_dir(repo_root: Path) -> Path:
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
    return main_git_dir(repo_root).parent / ".sentinel-swarm" / "ledger.db"


def append_git_excludes(repo_root: Path, lines: Sequence[str]) -> list[str]:
    path = main_git_dir(repo_root) / "info" / "exclude"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in lines if line not in present]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("".join(line + "\n" for line in missing))
    return missing


def ensure_git_exclude(repo_root: Path) -> bool:
    if not main_git_dir(repo_root).is_dir():
        return False
    return bool(append_git_excludes(repo_root, (_GIT_EXCLUDE_LINE,)))


def dict_rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def in_clause(values: Collection[object]) -> str:
    return ",".join("?" for _ in values)


def row_dict(conn: sqlite3.Connection, table: str, pk_column: str, value: object) -> dict:
    return dict(conn.execute(f"SELECT * FROM {table} WHERE {pk_column} = ?", (value,)).fetchone())


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # With max_workers 0 the server runs tool calls on threads and serializes them with a lock.
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
    if row is not None and row["version"] != 1:
        raise RuntimeError(f"unsupported ledger schema version {row['version']}")
    # Every statement in schema.sql is IF NOT EXISTS, so this adds new tables to an
    # existing ledger; a new column in an existing table still needs a new ledger.
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1)")
