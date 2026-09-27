from __future__ import annotations

from pathlib import Path

import pytest


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

    shown: list[list[str]] = []
    monkeypatch.setattr(notify, "runner", shown.append)
    return shown
