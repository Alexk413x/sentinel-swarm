from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def installed_plugin(plugin_id: str, marker: str) -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw) if raw else Path.home() / ".claude"
    registry = config_dir / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in data.get("plugins", {}).get(plugin_id, []):
        install = Path(str(entry.get("installPath") or ""))
        if (install / marker).is_file():
            return install
    return None


@pytest.fixture
def kg_host(tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install = installed_plugin("codebase-kg@codebase-kg", "mcp/pyproject.toml")
    if install is None or shutil.which("uv") is None:
        pytest.skip("the codebase-kg plugin or uv is not installed")
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    (root / "knowledge").mkdir()
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", root / "knowledge" / "code_graph.db")
    shim = root / ".sentinel-swarm" / "hook.py"
    shim.parent.mkdir()
    shutil.copyfile(repo_root / "templates" / "hook_shim.py", shim)
    registry = tmp_path / "config" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(install)}
    registry.write_text(json.dumps({"plugins": {"codebase-kg@codebase-kg": [entry]}}), "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))
    monkeypatch.delenv("SENTINEL_SWARM_LEDGER_DB", raising=False)
    return root
