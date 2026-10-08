from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import serve, setup

MCP_DIR = Path(__file__).resolve().parents[1]
LAUNCHER = MCP_DIR / "launch" / "ledger.py"


@pytest.fixture
def shim() -> Any:
    spec = importlib.util.spec_from_file_location("sentinel_swarm_shim_launch", setup.SHIM_TEMPLATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_server_starts_through_the_launcher() -> None:
    assert serve.LAUNCHER == LAUNCHER
    assert LAUNCHER.is_file()


def test_the_launcher_runs_a_ledger_module_on_an_isolated_interpreter() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER), "directive", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "ledger.py directive" in done.stdout


def test_the_launcher_refuses_a_module_name_that_is_not_a_ledger_module() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER), "../x"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode != 0
    assert "usage" in done.stderr


def test_the_shim_runs_the_launcher_on_the_base_interpreter(
    shim: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "install"
    (install / "mcp" / "launch").mkdir(parents=True)
    (install / "mcp" / "launch" / "ledger.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(shim, "find_install", lambda name, repo: install)
    command, env = shim.ledger_command(tmp_path, "hooks", "stop")
    assert command[1:] == [
        "-I",
        "-S",
        str(install / "mcp" / "launch" / "ledger.py"),
        "hooks",
        "stop",
    ]
    assert env["CLAUDE_PLUGIN_ROOT"] == str(install)


def test_the_shim_reports_a_missing_launcher(
    shim: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(shim, "find_install", lambda name, repo: install)
    with pytest.raises(shim.ShimError, match="launcher"):
        shim.ledger_command(tmp_path, "hooks", "stop")


def test_the_shim_finds_the_plugin_from_any_marketplace(shim: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    registry = tmp_path / "installed_plugins.json"
    entry = {"scope": "project", "projectPath": str(repo), "installPath": str(install)}
    registry.write_text(
        json.dumps({"plugins": {"other@alexk413x": [], "sentinel-swarm@alexk413x": [entry]}}),
        encoding="utf-8",
    )
    assert shim.find_install(shim.PLUGIN_NAME, repo, registry) == install


def test_serve_detach_starts_the_server_and_prints_its_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[Path] = []

    def fake_ensure(root: Path) -> str:
        started.append(root)
        return "http://127.0.0.1:1234/mcp"

    monkeypatch.setattr(serve, "ensure_server", fake_ensure)
    assert serve.main(["--repo", str(tmp_path), "--detach"]) == 0
    assert started == [tmp_path.resolve()]
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:1234/mcp"
