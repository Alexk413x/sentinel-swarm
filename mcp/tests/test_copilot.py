from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import bootstrap_module

from swarm_ledger import copilot, launch, serve, sessions, setup
from swarm_ledger.ledger import Ledger

LAUNCHER = Path(__file__).resolve().parents[1] / "launch" / "ledger.py"


@pytest.mark.parametrize("action", ["check", "setup", "run", "resume"])
def test_worker_actions_never_enter_claude_flows(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Copilot preflight entered a Claude flow")

    monkeypatch.setattr(launch, "launch", forbidden)
    monkeypatch.setattr(setup, "run_setup", forbidden)
    monkeypatch.setattr(serve, "ensure_server", forbidden)
    monkeypatch.setattr(sessions, "_run", forbidden)
    repo = tmp_path / "not-created"

    assert copilot.main([action, "--repo", str(repo)]) == 1

    doc = json.loads(capsys.readouterr().out)
    assert doc["native_workers"] is False
    assert doc["read_only_status"] is True
    assert doc["worker_runtime"] == "claude"
    assert "explicit user choice" in doc["error"]
    assert not repo.exists()


@pytest.mark.parametrize("action", ["check", "setup", "run", "resume", "status"])
def test_companion_runs_on_base_python_without_writing_host_or_config(
    tmp_path: Path, action: str
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    repo = tmp_path / "host"
    repo.mkdir()

    done = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER), "copilot", action, "--repo", str(repo)],
        cwd=repo,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert done.returncode == 1
    doc = json.loads(done.stdout)
    assert doc["worker_runtime"] == "claude"
    assert doc["error"] == ("no ledger" if action == "status" else copilot.UNSUPPORTED)
    assert done.stderr == ""
    assert list(repo.iterdir()) == []
    assert list(config.iterdir()) == []


@pytest.mark.usefixtures("claude_sessions")
def test_status_reads_a_claude_run_without_changing_records(ledger: Ledger, capsys) -> None:
    bootstrap_module(ledger)
    before = "\n".join(ledger.conn.iterdump())

    assert copilot.main(["status", "--repo", str(ledger.repo_root), "--run", "1"]) == 0

    doc = json.loads(capsys.readouterr().out)
    assert "error" not in doc
    assert doc["worker_runtime"] == "claude"
    assert len(doc["runs"]) == 1
    assert doc["runs"][0]["run"]["run_id"] == 1
    assert "\n".join(ledger.conn.iterdump()) == before


def test_status_empty_ledger_is_not_a_completed_run(ledger: Ledger, capsys) -> None:
    assert copilot.main(["status", "--repo", str(ledger.repo_root)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["runs"] == []
    assert "error" not in doc


def test_status_never_retries_with_a_writable_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    records = tmp_path / ".sentinel-swarm"
    records.mkdir()
    (records / "ledger.db").write_bytes(b"not a database")
    opened: list[str] = []
    connect = sqlite3.connect

    def readonly_only(database: str, *, uri: bool):
        opened.append(database)
        assert uri
        assert database.endswith("?mode=ro")
        return connect(database, uri=uri)

    monkeypatch.setattr(copilot.sqlite3, "connect", readonly_only)

    assert copilot.main(["status", "--repo", str(tmp_path)]) == 1

    assert len(opened) == 1
    assert "not a database" in json.loads(capsys.readouterr().out)["error"]
    assert (records / "ledger.db").read_bytes() == b"not a database"


def test_status_connection_rejects_writes_and_closes_after_read(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[sqlite3.Connection] = []

    def snapshot(conn: sqlite3.Connection, sessions: dict, run_id: int | None):
        connections.append(conn)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM runs")
        return {"runs": []}

    monkeypatch.setattr(copilot.panel, "snapshot", snapshot)

    assert copilot.status(ledger.repo_root)["runs"] == []

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_status_handles_uri_characters_in_host_path(tmp_path: Path) -> None:
    repo = tmp_path / "host #1"
    records = repo / ".sentinel-swarm"
    records.mkdir(parents=True)
    with sqlite3.connect(records / "ledger.db") as conn:
        conn.executescript(
            (LAUNCHER.parents[1] / "src" / "swarm_ledger" / "schema.sql").read_text(
                encoding="utf-8"
            )
        )

    doc = copilot.status(repo)

    assert "error" not in doc
    assert doc["runs"] == []


def test_status_reports_filesystem_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(repo: Path):
        raise PermissionError("ledger access denied")

    monkeypatch.setattr(copilot, "ledger_path", denied)

    doc = copilot.status(tmp_path)

    assert doc["error"] == "ledger access denied"
    assert doc["runs"] == []


def test_run_selection_requires_status() -> None:
    with pytest.raises(SystemExit) as exc:
        copilot.main(["run", "--run", "1"])
    assert exc.value.code == 2


def test_copilot_skill_stays_separate_from_claude_flows(repo_root: Path) -> None:
    text = (repo_root / "copilot" / "skills" / "sentinel-swarm" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Native Copilot workers are unsupported" in text
    assert "explicit user choice" in text
    assert "copilot status" in text
    assert "mode=ro" in text
    assert "CLAUDE_PLUGIN_ROOT" in text
    assert "launch --bg" not in text
    assert "serve --detach" not in text
