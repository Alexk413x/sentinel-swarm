from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_ledger import sessions


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


@pytest.fixture(autouse=True)
def claude_version(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int] | None]:
    # Setup reads `claude --version` to pick the hook transport; no test runs the real CLI.
    version: list[tuple[int, int, int] | None] = [None]
    monkeypatch.setattr(sessions, "claude_version", lambda: version[0])
    return version


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
