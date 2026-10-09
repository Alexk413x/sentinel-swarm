from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import graph, sessions
from swarm_ledger.identity import LedgerError


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_claude_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The machine-level run lock (lock.py) writes under CLAUDE_CONFIG_DIR; tests must never
    # touch the real ~/.claude. A fixture that needs the real registry sets its own value.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    monkeypatch.delenv("CLAUDE_DEV_CHANNELS", raising=False)


@pytest.fixture(autouse=True)
def os_notifications(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    # The default settings turn OS notifications on; no test may pop a real one.
    from swarm_ledger import notify

    # A Linux host without notify-send skips the OS path, so tests assume one is installed.
    real_command = notify.os_command

    def os_command(message: str, **kwargs: Any) -> list[str] | None:
        kwargs.setdefault("which", lambda name: f"/usr/bin/{name}")
        return real_command(message, **kwargs)

    shown: list[list[str]] = []
    monkeypatch.setattr(notify, "os_command", os_command)
    monkeypatch.setattr(notify, "runner", shown.append)
    return shown


def _check_in(root: Path, *session_ids: str) -> None:
    from swarm_ledger import env

    ledger = env.open_ledger(root)
    try:
        for session_id in session_ids:
            ledger.mod_session(session_id)
    finally:
        ledger.conn.close()


@pytest.fixture
def check_in() -> Callable[..., None]:
    # Records sessions as ones the mod checked in for, as its SessionStart post does.
    return _check_in


@pytest.fixture
def kg_root(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # This repo's own codebase-kg install, from the real registry; then the override, so the
    # test's host repo finds it while every other lookup keeps the isolated config.
    isolated = os.environ["CLAUDE_CONFIG_DIR"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    try:
        root = graph.codebase_kg_root(repo_root)
    except LedgerError:
        pytest.skip("codebase-kg is not installed for this repo")
    finally:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", isolated)
    monkeypatch.setenv("SENTINEL_SWARM_KG_ROOT", str(root))
    return root


@pytest.fixture
def claude_sessions(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    listing: list[dict] = []

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return json.dumps(listing)

    monkeypatch.setattr(sessions, "_run", fake_run)
    return listing


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    return root
