from __future__ import annotations

import json
import os
import shutil
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


def unwrappable(install: Path, server: str) -> str | None:
    try:
        entry = json.loads((install / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        command = Path(str(entry[server]["command"])).name
    except (OSError, ValueError, KeyError, TypeError):
        return f"{install} declares no MCP server named {server}"
    if command.lower().removesuffix(".exe") in ("uv", "uvx"):
        return None
    return f"{install.name} runs {server} with {command}, which mcp-http cannot wrap"


def http_host(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch, plugin_id: str, install: Path
) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    shim = root / ".sentinel-swarm" / "hook.py"
    shim.parent.mkdir()
    shutil.copyfile(repo_root / "templates" / "hook_shim.py", shim)
    registry = tmp_path / "config" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(install)}
    registry.write_text(json.dumps({"plugins": {plugin_id: [entry]}}), "utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))
    monkeypatch.delenv("SENTINEL_SWARM_LEDGER_DB", raising=False)
    return root


@pytest.fixture
def kg_host(tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install = installed_plugin("codebase-kg@codebase-kg", "mcp/pyproject.toml")
    if install is None or shutil.which("uv") is None:
        pytest.skip("the codebase-kg plugin or uv is not installed")
    reason = unwrappable(install, "codebase-kg")
    if reason is not None:
        pytest.skip(reason)
    root = http_host(tmp_path, repo_root, monkeypatch, "codebase-kg@codebase-kg", install)
    (root / "knowledge").mkdir()
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", root / "knowledge" / "code_graph.db")
    return root


@pytest.fixture
def a11y_kg_host(tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install = installed_plugin("a11y@accessibility-tools", "mcp-kg/pyproject.toml")
    if install is None or shutil.which("uvx") is None:
        pytest.skip("the a11y plugin or uvx is not installed")
    reason = unwrappable(install, "a11y-kg")
    if reason is not None:
        pytest.skip(reason)
    return http_host(tmp_path, repo_root, monkeypatch, "a11y@accessibility-tools", install)
