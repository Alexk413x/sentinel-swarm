from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import launch, setup

URL = "http://127.0.0.1:5123/mcp"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write(b'{"type":"result"}\n')
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", "claude-under-test")
    repo = tmp_path / "My Host"
    (repo / ".git" / "info").mkdir(parents=True)
    servers: list[Path] = []

    def ensure_server(root: Path) -> str:
        servers.append(root)
        return URL

    monkeypatch.setattr(launch, "_ensure_server", ensure_server)
    recorder = Recorder()
    monkeypatch.setattr(launch.subprocess, "run", recorder)
    return {"repo": repo, "config": config, "servers": servers, "run": recorder}


def _trust(config: Path, repo: Path) -> None:
    (config / ".claude.json").write_text(
        json.dumps({"projects": {repo.resolve().as_posix(): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )


def _option(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_interactive_puts_the_prompt_first_and_builds_the_flags(env):
    code = launch.main(["--repo", str(env["repo"]), "Build hello.py"])

    assert code == 0
    call = env["run"].calls[-1]
    command = call["command"]
    assert command[:2] == ["claude-under-test", "Build hello.py"]
    assert "--bg" not in command
    assert _option(command, "--agent") == "swarm-oracle"
    assert _option(command, "--model") == "opus"
    assert _option(command, "--permission-mode") == "default"
    assert "--strict-mcp-config" in command
    config = json.loads(_option(command, "--mcp-config"))
    assert config["mcpServers"]["swarm-ledger"] == {"type": "http", "url": URL}
    assert config["mcpServers"]["codebase-kg"] == {
        "command": "python",
        "args": [".sentinel-swarm/hook.py", "mcp", "codebase-kg@codebase-kg", "codebase-kg"],
    }
    tools = _option(command, "--allowedTools").split(",")
    assert "mcp__swarm-ledger" in tools
    assert "ToolSearch" in tools
    assert "Agent" not in tools
    assert json.loads(_option(command, "--settings")) == {"worktree": {"bgIsolation": "none"}}
    assert call["cwd"] == env["repo"].resolve()
    assert env["servers"] == [env["repo"].resolve()]


def test_the_role_file_model_and_permission_mode_are_used(env):
    setup.run_setup(env["repo"])
    path = setup.role_file(env["repo"], "oracle")
    text = path.read_text(encoding="utf-8")
    text = text.replace("model: opus", "model: fable").replace(
        "permissionMode: default", "permissionMode: plan"
    )
    path.write_text(text, encoding="utf-8")

    launch.main(["--repo", str(env["repo"]), "go"])

    command = env["run"].calls[-1]["command"]
    assert _option(command, "--model") == "fable"
    assert _option(command, "--permission-mode") == "plan"


def test_a_broken_role_file_is_reported(env, capsys: pytest.CaptureFixture[str]):
    setup.run_setup(env["repo"])
    setup.role_file(env["repo"], "oracle").write_text("no frontmatter\n", encoding="utf-8")

    assert launch.main(["--repo", str(env["repo"]), "go"]) == 1
    assert env["run"].calls == []
    assert "cannot start the Oracle" in capsys.readouterr().err


def test_missing_role_files_run_setup_first(env):
    launch.main(["--repo", str(env["repo"]), "go"])

    for role in setup.ROLES:
        assert setup.role_file(env["repo"], role).is_file()
    assert (env["repo"] / ".sentinel-swarm" / "hook.py").is_file()


def test_present_role_files_skip_setup(env, monkeypatch: pytest.MonkeyPatch):
    setup.run_setup(env["repo"])

    def fail(repo: Path):
        raise AssertionError("setup ran again")

    monkeypatch.setattr(launch.setup, "run_setup", fail)

    assert launch.main(["--repo", str(env["repo"]), "go"]) == 0


def test_bg_refuses_an_untrusted_repo(env, capsys: pytest.CaptureFixture[str]):
    code = launch.main(["--repo", str(env["repo"]), "--bg", "go"])

    assert code == 1
    assert env["run"].calls == []
    assert env["servers"] == []
    err = capsys.readouterr().err
    assert "not trusted" in err
    assert f'cd "{env["repo"].resolve()}" && claude' in err


def test_bg_prints_the_trust_instructions_once(env, capsys: pytest.CaptureFixture[str]):
    launch.main(["--repo", str(env["repo"]), "--bg", "go"])

    captured = capsys.readouterr()
    assert (captured.out + captured.err).count("is not trusted yet") == 1
    assert "wrote .claude/agents/swarm-oracle.md" in captured.out


def test_interactive_setup_reports_the_trust_step(env, capsys: pytest.CaptureFixture[str]):
    launch.main(["--repo", str(env["repo"]), "go"])

    assert capsys.readouterr().out.count("is not trusted yet") == 1


def test_bg_names_the_oracle_session_after_the_repo(env, capsys: pytest.CaptureFixture[str]):
    _trust(env["config"], env["repo"])

    code = launch.main(["--repo", str(env["repo"]), "--bg", "Build hello.py"])

    assert code == 0
    command = env["run"].calls[-1]["command"]
    assert command[:4] == ["claude-under-test", "Build hello.py", "--bg", "--name"]
    assert re.fullmatch(r"my-host-oracle-\d{4}-\d{6}", command[4])
    assert command[4] in capsys.readouterr().out


def test_headless_pipes_the_prompt_into_the_transcript(env, tmp_path: Path):
    transcript = tmp_path / "out" / "transcript.jsonl"

    code = launch.main(
        ["--repo", str(env["repo"]), "--headless", "--transcript", str(transcript), "Build it"]
    )

    assert code == 0
    call = env["run"].calls[-1]
    command = call["command"]
    assert command[:2] == ["claude-under-test", "-p"]
    assert "Build it" not in command
    assert call["input"] == b"Build it"
    assert command[-3:] == ["--output-format", "stream-json", "--verbose"]
    assert transcript.read_text(encoding="utf-8") == '{"type":"result"}\n'


def test_transcript_needs_headless(env):
    with pytest.raises(SystemExit):
        launch.main(["--repo", str(env["repo"]), "--transcript", "t.jsonl", "go"])


def test_bg_and_headless_exclude_each_other(env):
    with pytest.raises(SystemExit):
        launch.main(["--repo", str(env["repo"]), "--bg", "--headless", "go"])


def test_a_missing_claude_binary_is_reported(env, monkeypatch, capsys):
    def missing(command: list[str], **kwargs: Any):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(launch.subprocess, "run", missing)

    assert launch.main(["--repo", str(env["repo"]), "go"]) == 1
    assert "SENTINEL_SWARM_CLAUDE" in capsys.readouterr().err
