from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarm_ledger import setup


def _frontmatter(path: Path) -> dict:
    head, _ = setup.split_document(path.read_text(encoding="utf-8"))
    return yaml.safe_load(head)


def _body(path: Path) -> str:
    return setup.split_document(path.read_text(encoding="utf-8"))[1]


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    (config / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture
def repo(tmp_path: Path, config_dir: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git" / "info").mkdir(parents=True)
    return root


def _register(config: Path, plugin_id: str, entries: list[dict]) -> None:
    path = config / "plugins" / "installed_plugins.json"
    data: dict[str, Any] = {"plugins": {}}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 2
    data["plugins"][plugin_id] = entries
    path.write_text(json.dumps(data), encoding="utf-8")


def _plugin(tmp_path: Path, name: str, servers: dict) -> Path:
    install = tmp_path / "cache" / name
    install.mkdir(parents=True)
    (install / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return install


def test_fresh_repo_gets_every_file(repo: Path):
    report = setup.run_setup(repo)

    for role in setup.ROLES:
        path = setup.role_file(repo, role)
        assert path.read_text(encoding="utf-8") == setup.template_file(role).read_text(
            encoding="utf-8"
        )
        assert f"wrote .claude/agents/swarm-{role}.md" in report.lines
    assert (repo / ".sentinel-swarm" / "hook.py").read_text(
        encoding="utf-8"
    ) == setup.SHIM_TEMPLATE.read_text(encoding="utf-8")
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert settings == {"worktree": {"bgIsolation": "none"}}
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".sentinel-swarm/" in exclude
    assert ".claude/agents/swarm-*.md" in exclude


def test_setup_is_idempotent(repo: Path):
    setup.run_setup(repo)
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}

    report = setup.run_setup(repo)

    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert after == before
    assert all(line.startswith("unchanged") for line in report.lines[:7])
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert exclude.count(".sentinel-swarm/") == 1


def test_existing_role_file_keeps_frontmatter_and_gets_new_body(repo: Path):
    path = setup.role_file(repo, "lead")
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: swarm-lead\nmodel: opus\ncolor: red\ntools: Read, Grep\n---\n\nOld body.\n",
        encoding="utf-8",
    )

    report = setup.run_setup(repo)

    fields = _frontmatter(path)
    assert fields["model"] == "opus"
    assert fields["color"] == "red"
    assert fields["tools"] == "Read, Grep"
    assert "hooks" in fields
    assert "mcpServers" in fields
    assert fields["permissionMode"] == "default"
    assert _body(path) == _body(setup.template_file("lead"))
    line = next(line for line in report.lines if "swarm-lead.md" in line)
    assert line.startswith("updated")
    assert "replaced the prompt body" in line
    assert "permissionMode" in line and "hooks" in line


def test_role_file_without_frontmatter_is_left_alone(repo: Path):
    path = setup.role_file(repo, "coder")
    path.parent.mkdir(parents=True)
    path.write_text("just text\n", encoding="utf-8")

    report = setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == "just text\n"
    assert any(
        line.startswith("left .claude/agents/swarm-coder.md unchanged") for line in report.lines
    )


def test_settings_local_merge_keeps_other_keys(repo: Path):
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "worktree": {"other": 1}}), encoding="utf-8"
    )

    setup.run_setup(repo)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "permissions": {"allow": ["Read"]},
        "worktree": {"other": 1, "bgIsolation": "none"},
    }


def test_invalid_settings_local_is_left_alone(repo: Path):
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    report = setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == "{not json"
    assert any("not valid JSON" in line for line in report.lines)


def test_excludes_resolve_a_worktree_git_file(tmp_path: Path, config_dir: Path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")

    setup.run_setup(worktree)

    exclude = (main / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".claude/agents/swarm-*.md" in exclude


def test_excludes_append_to_an_existing_file(repo: Path):
    path = repo / ".git" / "info" / "exclude"
    path.write_text("# git ls-files --others\n.sentinel-swarm/", encoding="utf-8")

    setup.run_setup(repo)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["# git ls-files --others", ".sentinel-swarm/", ".claude/agents/swarm-*.md"]


def test_not_a_git_checkout_is_reported(tmp_path: Path, config_dir: Path):
    root = tmp_path / "plain"
    root.mkdir()

    report = setup.run_setup(root)

    assert any(line.startswith("not a git checkout") for line in report.lines)


def test_project_plugin_servers_join_new_role_files(tmp_path: Path, repo: Path, config_dir: Path):
    extra = _plugin(
        tmp_path, "a11y", {"a11y-tools": {"command": "uv"}, "a11y-kg": {"command": "uv"}}
    )
    kg = _plugin(tmp_path, "kg", {"codebase-kg": {"command": "uv"}})
    off = _plugin(tmp_path, "off", {"off-server": {"command": "uv"}})
    other_repo = tmp_path / "elsewhere"
    _register(
        config_dir,
        "a11y@accessibility-tools",
        [
            {"scope": "project", "projectPath": str(other_repo), "installPath": str(off)},
            {"scope": "project", "projectPath": str(repo), "installPath": str(extra)},
        ],
    )
    _register(
        config_dir,
        "codebase-kg@codebase-kg",
        [
            {"scope": "project", "projectPath": str(repo), "installPath": str(kg)},
        ],
    )
    _register(config_dir, "off@x", [{"scope": "user", "installPath": str(off)}])
    _register(config_dir, "missing@x", [])
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "a11y@accessibility-tools": True,
                    "codebase-kg@codebase-kg": True,
                    "sentinel-swarm@sentinel-swarm": True,
                    "off@x": False,
                    "missing@x": True,
                }
            }
        ),
        encoding="utf-8",
    )

    report = setup.run_setup(repo)

    fields = _frontmatter(setup.role_file(repo, "manager"))
    tools = [t.strip() for t in fields["tools"].split(",")]
    assert "mcp__a11y-tools" in tools and "mcp__a11y-kg" in tools
    assert "mcp__codebase-kg" not in tools
    assert "mcp__off-server" not in tools
    servers = {name: config for entry in fields["mcpServers"] for name, config in entry.items()}
    assert set(servers) == {"codebase-kg", "a11y-tools", "a11y-kg"}
    assert servers["a11y-tools"] == {
        "command": "python",
        "args": [".sentinel-swarm/hook.py", "mcp", "a11y@accessibility-tools", "a11y-tools"],
    }
    assert "hooks" in fields
    assert _body(setup.role_file(repo, "manager")) == _body(setup.template_file("manager"))
    assert any("skipped the MCP servers of missing@x" in line for line in report.lines)


def test_trust_is_read_from_claude_json(repo: Path, config_dir: Path):
    assert not setup.run_setup(repo).trusted
    key = repo.resolve().as_posix()
    (config_dir / ".claude.json").write_text(
        json.dumps({"projects": {key: {"hasTrustDialogAccepted": True}}}), encoding="utf-8"
    )

    report = setup.run_setup(repo)

    assert report.trusted
    assert setup.is_trusted(repo)


def test_untrusted_repo_gets_the_trust_command(repo: Path):
    report = setup.run_setup(repo)

    assert not report.trusted
    assert f'cd "{repo.resolve()}" && claude' in report.text()


@pytest.fixture
def shim():
    return setup.load_shim()


def test_shim_lookup_prefers_local_then_project_then_user(tmp_path: Path, config_dir: Path, shim):
    repo = tmp_path / "host"
    repo.mkdir()
    local, project, user = (tmp_path / n for n in ("local", "project", "user"))
    for path in (local, project, user):
        path.mkdir()
    entries = [
        {"scope": "user", "installPath": str(user)},
        {"scope": "project", "projectPath": str(tmp_path / "other"), "installPath": str(local)},
        {"scope": "project", "projectPath": str(repo), "installPath": str(project)},
        {"scope": "local", "projectPath": str(repo), "installPath": str(local)},
    ]
    _register(config_dir, "p@m", entries)
    assert shim.find_install("p@m", repo) == local

    _register(config_dir, "p@m", entries[:3])
    assert shim.find_install("p@m", repo) == project

    _register(config_dir, "p@m", entries[:2])
    assert shim.find_install("p@m", repo) == user

    _register(config_dir, "p@m", entries[1:2])
    with pytest.raises(shim.ShimError, match="not installed"):
        shim.find_install("p@m", repo)


def test_shim_reports_a_missing_install_folder(tmp_path: Path, config_dir: Path, shim):
    repo = tmp_path / "host"
    repo.mkdir()
    _register(config_dir, "p@m", [{"scope": "user", "installPath": str(tmp_path / "gone")}])

    with pytest.raises(shim.ShimError, match="folder is missing"):
        shim.find_install("p@m", repo)


def test_shim_expands_plugin_root_and_defaults(shim):
    env = {"SET": "value", "EMPTY": ""}
    assert shim.expand("${CLAUDE_PLUGIN_ROOT}/mcp", "/root", env) == "/root/mcp"
    assert shim.expand("${SET:-x}", "/root", env) == "value"
    assert shim.expand("${EMPTY:-x}", "/root", env) == "x"
    assert shim.expand("${UNSET:-}", "/root", env) == ""
    assert shim.expand("${UNSET}-${SET}", "/root", env) == "-value"


def test_shim_watch_reports_a_missing_install_as_one_line(repo: Path, config_dir: Path):
    setup.run_setup(repo)

    result = _run_shim(repo, config_dir, "watch")

    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("sentinel-swarm watchdog cannot start: ")
    assert lines[0].endswith("run /sentinel-swarm:setup")


def test_shim_watch_runs_the_listener_and_passes_each_line_through(
    tmp_path: Path, shim, monkeypatch: pytest.MonkeyPatch, capfdbinary: pytest.CaptureFixture
):
    calls: list[tuple] = []
    script = "print('one', flush=True); print('two', flush=True); raise SystemExit(0)"

    def fake_command(repo: Path, module: str, *args: str):
        calls.append((repo, module, args))
        return [sys.executable, "-c", script], dict(os.environ)

    monkeypatch.setattr(shim, "ledger_command", fake_command)

    assert shim.main(["watch"]) == 0

    assert capfdbinary.readouterr().out.splitlines() == [b"one", b"two"]
    repo = shim.repo_root()
    assert calls == [(repo, "swarm_ledger.watch", ("--repo", str(repo)))]


def test_shim_failure_answer_denies_only_gating_events(shim):
    assert "pre_monitor" in shim.GATING_EVENTS
    for event in ("pre_agent", "pre_write", "pre_shell", "pre_ledger", "pre_monitor"):
        answer = shim.failure_answer(event, "broken")
        specific = answer["hookSpecificOutput"]
        assert specific["permissionDecision"] == "deny"
        assert "broken" in specific["permissionDecisionReason"]
        assert "broken" in answer["systemMessage"]
    for event in ("session_start", "post_any", "stop", "session_end"):
        answer = shim.failure_answer(event, "broken")
        assert set(answer) == {"systemMessage"}


def _run_shim(repo: Path, config_dir: Path, *args: str, stdin: str = "{}"):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return subprocess.run(
        [sys.executable, str(repo / ".sentinel-swarm" / "hook.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_shim_hook_without_registry_denies_a_gating_event(repo: Path, config_dir: Path):
    setup.run_setup(repo)

    gated = _run_shim(repo, config_dir, "hook", "pre_write")
    reported = _run_shim(repo, config_dir, "hook", "stop")

    assert gated.returncode == 0
    answer = json.loads(gated.stdout)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "installed_plugins.json" in answer["systemMessage"]
    assert reported.returncode == 0
    assert set(json.loads(reported.stdout)) == {"systemMessage"}


def test_shim_hook_without_install_names_the_plugin(repo: Path, config_dir: Path):
    setup.run_setup(repo)
    _register(config_dir, "other@x", [])

    result = _run_shim(repo, config_dir, "hook", "pre_ledger")

    answer = json.loads(result.stdout)
    assert "sentinel-swarm@sentinel-swarm is not installed for" in answer["systemMessage"]


def test_shim_hook_with_missing_ledger_code_denies(tmp_path: Path, repo: Path, config_dir: Path):
    setup.run_setup(repo)
    install = tmp_path / "swarm-install"
    install.mkdir()
    _register(
        config_dir,
        "sentinel-swarm@sentinel-swarm",
        [
            {"scope": "project", "projectPath": str(repo), "installPath": str(install)},
        ],
    )

    result = _run_shim(repo, config_dir, "hook", "pre_shell")

    answer = json.loads(result.stdout)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ledger code is missing" in answer["systemMessage"]


def test_shim_mcp_runs_the_expanded_server(tmp_path: Path, repo: Path, config_dir: Path):
    setup.run_setup(repo)
    script = "import os, sys; print(os.environ['KG']); print(sys.argv[1]); sys.exit(3)"
    install = _plugin(
        tmp_path,
        "srv",
        {
            "srv": {
                "command": sys.executable,
                "args": ["-c", script, "${CLAUDE_PLUGIN_ROOT}/mcp"],
                "env": {"KG": "${SENTINEL_TEST_UNSET:-fallback}"},
            }
        },
    )
    _register(
        config_dir,
        "srv@m",
        [
            {"scope": "local", "projectPath": str(repo), "installPath": str(install)},
        ],
    )

    result = _run_shim(repo, config_dir, "mcp", "srv@m", "srv")

    assert result.returncode == 3
    assert result.stdout.splitlines() == ["fallback", f"{install}/mcp"]


def test_shim_mcp_reports_a_command_it_cannot_start(tmp_path: Path, repo: Path, config_dir: Path):
    setup.run_setup(repo)
    install = _plugin(tmp_path, "srv", {"srv": {"command": "no-such-binary-sentinel"}})
    _register(config_dir, "srv@m", [{"scope": "user", "installPath": str(install)}])

    missing_server = _run_shim(repo, config_dir, "mcp", "srv@m", "other")
    missing_binary = _run_shim(repo, config_dir, "mcp", "srv@m", "srv")

    assert missing_server.returncode == 1
    assert "no MCP server named other" in missing_server.stderr
    assert missing_binary.returncode == 1
    assert "no-such-binary-sentinel is not on PATH" in missing_binary.stderr
