from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import setup

MCP_DIR = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shim() -> Any:
    return _load("sentinel_swarm_hook_shim_venv", setup.SHIM_TEMPLATE)


@pytest.fixture
def venv_script() -> Any:
    return _load("sentinel_swarm_ledger_venv", MCP_DIR / "ledger_venv.py")


def test_the_venv_lives_in_the_plugin_data_folder_keyed_by_the_lock(
    venv_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    digest = hashlib.sha256((MCP_DIR / "uv.lock").read_bytes()).hexdigest()[:12]
    expected = tmp_path / "config" / "plugins" / "data" / "sentinel-swarm-sentinel-swarm"
    assert venv_script.ledger_venv(MCP_DIR) == expected / f"venv-{digest}"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert venv_script.ledger_venv(MCP_DIR).parent.parent.parent.parent == Path.home() / ".claude"


def test_the_shim_and_the_script_name_the_same_venv(
    shim: Any, venv_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    assert shim.ledger_venv(MCP_DIR) == venv_script.ledger_venv(MCP_DIR)
    other = tmp_path / "other"
    other.mkdir()
    (other / "uv.lock").write_text("a different lock\n", encoding="utf-8")
    assert shim.ledger_venv(other) == venv_script.ledger_venv(other)
    assert shim.ledger_venv(other) != shim.ledger_venv(MCP_DIR)


def test_the_script_prints_the_venv_path(tmp_path: Path) -> None:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path / "config")}
    done = subprocess.run(
        [sys.executable, str(MCP_DIR / "ledger_venv.py")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    digest = hashlib.sha256((MCP_DIR / "uv.lock").read_bytes()).hexdigest()[:12]
    assert done.stdout.strip().endswith(f"sentinel-swarm-sentinel-swarm{os.sep}venv-{digest}")


def test_the_shim_runs_uv_with_the_shared_venv(
    shim: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    install = tmp_path / "install"
    (install / "mcp").mkdir(parents=True)
    (install / "mcp" / "pyproject.toml").write_text("", encoding="utf-8")
    (install / "mcp" / "uv.lock").write_text("lock\n", encoding="utf-8")
    monkeypatch.setattr(shim, "find_install", lambda plugin_id, repo: install)
    monkeypatch.setattr(shim.shutil, "which", lambda name: "uv")
    command, env = shim.ledger_command(tmp_path, "swarm_ledger.hooks", "stop")
    assert command[:3] == ["uv", "run", "--project"]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(shim.ledger_venv(install / "mcp"))


def test_the_shim_reports_a_missing_lock_file(
    shim: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "install"
    (install / "mcp").mkdir(parents=True)
    (install / "mcp" / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(shim, "find_install", lambda plugin_id, repo: install)
    with pytest.raises(shim.ShimError, match="lock file"):
        shim.ledger_command(tmp_path, "swarm_ledger.hooks", "stop")


@pytest.mark.parametrize(("own_venv", "kept"), [(True, False), (False, True)])
def test_the_package_drops_only_its_own_venv_from_the_environment(
    tmp_path: Path, own_venv: bool, kept: bool
) -> None:
    value = sys.prefix if own_venv else str(tmp_path / "host-venv")
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": value}
    code = "import os, swarm_ledger; print(os.environ.get('UV_PROJECT_ENVIRONMENT', ''))"
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    assert done.stdout.strip() == (value if kept else "")
