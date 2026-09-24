from __future__ import annotations

import multiprocessing
import os
import sqlite3
from pathlib import Path

import pytest

from swarm_ledger.db import connect, ensure_git_exclude, ledger_path, write_tx

EXPECTED_TABLES = {
    "runs",
    "phases",
    "modules",
    "files",
    "agents",
    "briefs",
    "reviews",
    "scores",
    "issues",
    "change_requests",
    "test_runs",
    "guidelines",
    "departures",
    "versions",
    "messages",
    "directives",
    "agent_events",
    "wakeups",
    "schema_version",
}


def test_connect_creates_file_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    conn = connect(db_path)
    try:
        assert db_path.exists()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row["name"] for row in rows}
        assert EXPECTED_TABLES <= tables
        version = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        assert version == 1
    finally:
        conn.close()


def test_connect_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    connect(db_path).close()
    conn = connect(db_path)
    try:
        version = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        assert version == 1
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_new_ledger_has_the_session_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        assert {"session_name", "bg_id"} <= _columns(conn, "agents")
    finally:
        conn.close()


def test_connect_upgrades_an_older_version_1_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL);"
        "INSERT INTO schema_version (id, version) VALUES (1, 1);"
        "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "role TEXT NOT NULL, state TEXT NOT NULL, ended_at TEXT);"
        "INSERT INTO agents (agent_id, name, role, state) VALUES ('a1', 'lead-1', 'lead', 'idle');"
    )
    old.close()

    conn = connect(db_path)
    try:
        assert {"session_name", "bg_id"} <= _columns(conn, "agents")
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "wakeups" in tables
        row = conn.execute("SELECT * FROM agents WHERE agent_id = 'a1'").fetchone()
        assert (row["name"], row["session_name"]) == ("lead-1", None)
    finally:
        conn.close()

    connect(db_path).close()


def test_write_tx_commits(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO agent_events (agent_id, to_state) VALUES (?, ?)",
                ("agent-1", "working"),
            )
        row = conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()
        assert row["n"] == 1
    finally:
        conn.close()


def test_write_tx_rolls_back_on_exception(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        with pytest.raises(ValueError):
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO agent_events (agent_id, to_state) VALUES (?, ?)",
                    ("agent-1", "working"),
                )
                raise ValueError("boom")
        row = conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()
        assert row["n"] == 0
    finally:
        conn.close()


def test_agents_unique_live_name_allows_one_live_agent_per_name(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ledger.db")
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, state) VALUES (?, ?, ?, ?)",
                ("a1", "coder-1", "coder", "working"),
            )

        with pytest.raises(sqlite3.IntegrityError):
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO agents (agent_id, name, role, state) VALUES (?, ?, ?, ?)",
                    ("a2", "coder-1", "coder", "working"),
                )

        with write_tx(conn):
            conn.execute(
                "UPDATE agents SET ended_at = ? WHERE agent_id = ?",
                ("2026-01-01T00:00:00.000Z", "a1"),
            )

        with write_tx(conn):
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, state) VALUES (?, ?, ?, ?)",
                ("a3", "coder-1", "coder", "working"),
            )

        row = conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE name = ?", ("coder-1",)
        ).fetchone()
        assert row["n"] == 2
    finally:
        conn.close()


def test_ledger_path_resolves_worktree_to_main_checkout(tmp_path: Path) -> None:
    main_root = tmp_path / "main"
    (main_root / ".git").mkdir(parents=True)
    wt_root = tmp_path / "wt"
    wt_root.mkdir()
    (wt_root / ".git").write_text(
        f"gitdir: {main_root / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8"
    )

    main_ledger = ledger_path(main_root)
    wt_ledger = ledger_path(wt_root)

    assert main_ledger == main_root / ".sentinel-swarm" / "ledger.db"
    assert wt_ledger == main_ledger


def test_ensure_git_exclude_adds_line_once(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    ensure_git_exclude(tmp_path)
    ensure_git_exclude(tmp_path)

    exclude_path = tmp_path / ".git" / "info" / "exclude"
    contents = exclude_path.read_text(encoding="utf-8")
    assert contents.count(".sentinel-swarm/") == 1


def _insert_events(args: tuple[str, int]) -> None:
    db_path, count = args
    conn = connect(Path(db_path))
    try:
        for i in range(count):
            with write_tx(conn):
                conn.execute(
                    "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
                    "VALUES (?, ?, ?, ?)",
                    (f"agent-{os.getpid()}", "idle", "working", f"event-{i}"),
                )
    finally:
        conn.close()


def test_agent_events_survive_concurrent_writers(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    connect(db_path).close()

    ctx = multiprocessing.get_context("spawn")
    jobs = [(str(db_path), 50) for _ in range(8)]
    with ctx.Pool(processes=8) as pool:
        pool.map(_insert_events, jobs)

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM agent_events").fetchone()
        assert row["n"] == 400
    finally:
        conn.close()
