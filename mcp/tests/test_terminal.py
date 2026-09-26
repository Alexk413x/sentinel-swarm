from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from swarm_ledger import terminal


def test_write_spec_passes_only_the_swarms_own_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", "claude-x")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    spec = json.loads(terminal.write_spec(tmp_path, ["claude", "hi"]).read_text("utf-8"))
    assert spec["argv"] == ["claude", "hi"]
    assert spec["cwd"] == str(tmp_path)
    assert spec["env"]["SENTINEL_SWARM_CLAUDE"] == "claude-x"
    assert "CLAUDE_CODE_CHILD_SESSION" not in spec["env"]


def test_host_env_drops_the_parent_sessions_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in terminal.CHILD_MARKERS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("CLAUDE_DEV_CHANNELS", "plugin:q@m")
    env = terminal.host_env({"SENTINEL_SWARM_X": "y"})
    assert not set(terminal.CHILD_MARKERS) & set(env)
    assert env["CLAUDE_DEV_CHANNELS"] == "plugin:q@m"
    assert env["SENTINEL_SWARM_X"] == "y"


def test_main_runs_the_spec_in_its_folder_and_deletes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_call(argv: list[str], cwd: str, env: dict[str, str]) -> int:
        del env
        calls.append((argv, cwd))
        return 3

    monkeypatch.setattr(subprocess, "call", fake_call)
    spec = terminal.write_spec(tmp_path, ["claude", "--name", "n-1"])
    assert terminal.main([str(spec)]) == 3
    assert calls == [(["claude", "--name", "n-1"], str(tmp_path))]
    assert not spec.exists()


def test_windows_opens_a_tab_in_the_swarm_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(terminal.shutil, "which", lambda name: f"C:/bin/{name}")
    command = terminal.terminal_command("n-1", tmp_path, ["py", "-m", "x"])
    assert command == [
        "C:/bin/wt.exe",
        "-w",
        "sentinel-swarm",
        "new-tab",
        "--title",
        "n-1",
        "-d",
        str(tmp_path),
        "py",
        "-m",
        "x",
    ]


def test_windows_without_windows_terminal_uses_a_new_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(terminal.shutil, "which", lambda name: None)
    assert terminal.terminal_command("n-1", tmp_path, ["py"]) is None


def test_macos_opens_terminal_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    command = terminal.terminal_command("n-1", tmp_path, ["py", "-m", "x"])
    assert command is not None and command[:2] == ["osascript", "-e"]
    assert "do script" in command[2] and "py -m x" in command[2]
