from __future__ import annotations

import importlib.util
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

    for role in setup.CORE_ROLES:
        path = setup.role_file(repo, role)
        assert path.read_text(encoding="utf-8") == setup.template_file(role).read_text(
            encoding="utf-8"
        )
        assert f"wrote .claude/agents/swarm-{role}.md" in report.lines
    assert any(line.startswith("skipped .claude/agents/swarm-driver.md") for line in report.lines)
    assert (repo / ".sentinel-swarm" / "hook.py").read_text(
        encoding="utf-8"
    ) == setup.SHIM_TEMPLATE.read_text(encoding="utf-8")
    settings = json.loads((repo / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert settings == {
        "worktree": {"bgIsolation": "none"},
        "permissions": {"deny": [f"Agent(swarm-{role})" for role in setup.ROLES]},
    }
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".sentinel-swarm/" in exclude
    assert ".claude/agents/swarm-*.md" in exclude


def test_setup_is_idempotent(repo: Path):
    setup.run_setup(repo)
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}

    report = setup.run_setup(repo)

    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert after == before
    assert all(
        line.startswith("unchanged") or line.startswith("skipped") for line in report.lines[:8]
    )
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert exclude.count(".sentinel-swarm/") == 1


def test_role_files_carry_no_ledger_command_hooks(repo: Path):
    setup.run_setup(repo)

    for role in setup.CORE_ROLES:
        fields = _frontmatter(setup.role_file(repo, role))
        assert "hooks" not in fields
        assert fields["name"] == f"swarm-{role}"
        assert _body(setup.role_file(repo, role)) == _body(setup.template_file(role))


def test_setup_strips_only_the_ledger_command_hooks_from_a_role_file(repo: Path):
    path = setup.role_file(repo, "lead")
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: swarm-lead\nhooks:\n  Notification:\n    - hooks:\n"
        '        - type: command\n          command: "notify-me"\n'
        "  Stop:\n    - hooks:\n        - type: command\n"
        '          command: "python3 .sentinel-swarm/hook.py hook stop || '
        'python .sentinel-swarm/hook.py hook stop"\n'
        "---\n\nOld body.\n",
        encoding="utf-8",
    )

    report = setup.run_setup(repo)

    assert _frontmatter(path)["hooks"] == {
        "Notification": [{"hooks": [{"type": "command", "command": "notify-me"}]}]
    }
    line = next(line for line in report.lines if "swarm-lead.md" in line)
    assert "removed the ledger command hooks the mod runs: stop" in line


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
    assert "hooks" not in fields
    assert "mcpServers" in fields
    assert fields["permissionMode"] == "default"
    assert _body(path) == _body(setup.template_file("lead"))
    line = next(line for line in report.lines if "swarm-lead.md" in line)
    assert line.startswith("updated")
    assert "replaced the prompt body" in line
    assert "permissionMode" in line


def test_setup_narrows_a_whole_server_ledger_grant_and_keeps_other_tools(repo: Path):
    from swarm_ledger.identity import ROLE_TOOLS

    path = setup.role_file(repo, "coder")
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: swarm-coder\ntools: Read, mcp__swarm-ledger, mcp__swarm-ledger__brief_get, "
        "mcp__custom__tool\n---\n\nOld body.\n",
        encoding="utf-8",
    )

    report = setup.run_setup(repo)

    tools = [t.strip() for t in _frontmatter(path)["tools"].split(",")]
    assert "mcp__swarm-ledger" not in tools
    assert tools[0] == "Read" and tools[-1] == "mcp__custom__tool"
    assert tools.count("mcp__swarm-ledger__brief_get") == 1
    ledger = {t.removeprefix("mcp__swarm-ledger__") for t in tools if t.startswith("mcp__swarm-")}
    assert ledger == ROLE_TOOLS["coder"]
    line = next(line for line in report.lines if "swarm-coder.md" in line)
    assert "the role's swarm-ledger tools in place of mcp__swarm-ledger" in line


def test_setup_keeps_an_explicit_ledger_tool_list(repo: Path):
    path = setup.role_file(repo, "lead")
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: swarm-lead\ntools: Read, mcp__swarm-ledger__brief_get\n---\n\nOld body.\n",
        encoding="utf-8",
    )

    setup.run_setup(repo)

    assert _frontmatter(path)["tools"] == "Read, mcp__swarm-ledger__brief_get"


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
        "permissions": {"allow": ["Read"], "deny": list(setup.ROLE_AGENT_DENY)},
        "worktree": {"other": 1, "bgIsolation": "none"},
    }


def test_settings_local_merge_keeps_existing_deny_rules_once(repo: Path):
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm:*)", "Agent(swarm-coder)"]}}),
        encoding="utf-8",
    )

    setup.run_setup(repo)
    setup.run_setup(repo)

    deny = json.loads(path.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert deny[:2] == ["Bash(rm:*)", "Agent(swarm-coder)"]
    assert sorted(deny[2:] + ["Agent(swarm-coder)"]) == sorted(setup.ROLE_AGENT_DENY)


def test_settings_local_with_a_non_list_deny_is_left_alone(repo: Path):
    path = repo / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"deny": "Bash"}}), encoding="utf-8")

    report = setup.run_setup(repo)

    assert json.loads(path.read_text(encoding="utf-8")) == {"permissions": {"deny": "Bash"}}
    assert any("permissions.deny is not a list" in line for line in report.lines)


def test_setup_turns_off_the_codebase_kg_nudge(repo: Path):
    path = repo / ".claude" / "codebase-kg.local.md"

    report = setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == "---\npost_edit_nudge: false\n---\n"
    assert any(line.startswith("set post_edit_nudge: false") for line in report.lines)
    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".claude/codebase-kg.local.md" in exclude

    again = setup.run_setup(repo)
    assert path.read_text(encoding="utf-8") == "---\npost_edit_nudge: false\n---\n"
    assert any("post_edit_nudge is already set" in line for line in again.lines)


def test_setup_adds_the_nudge_key_to_existing_codebase_kg_settings(repo: Path):
    path = repo / ".claude" / "codebase-kg.local.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nroot: app/src\n---\nnotes\n", encoding="utf-8")

    setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == (
        "---\npost_edit_nudge: false\nroot: app/src\n---\nnotes\n"
    )


def test_setup_keeps_a_codebase_kg_nudge_the_host_set(repo: Path):
    path = repo / ".claude" / "codebase-kg.local.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\npost_edit_nudge: true\n---\n", encoding="utf-8")

    setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == "---\npost_edit_nudge: true\n---\n"


def test_setup_prepends_frontmatter_to_codebase_kg_settings_without_it(repo: Path):
    path = repo / ".claude" / "codebase-kg.local.md"
    path.parent.mkdir(parents=True)
    path.write_text("plain notes\n", encoding="utf-8")

    setup.run_setup(repo)

    assert path.read_text(encoding="utf-8") == "---\npost_edit_nudge: false\n---\nplain notes\n"


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
    assert lines == [
        "# git ls-files --others",
        ".sentinel-swarm/",
        ".claude/agents/swarm-*.md",
        ".claude/codebase-kg.local.md",
    ]


def test_not_a_git_checkout_is_reported(tmp_path: Path, config_dir: Path):
    root = tmp_path / "plain"
    root.mkdir()

    report = setup.run_setup(root)

    assert any(line.startswith("not a git checkout") for line in report.lines)


def test_enabled_project_plugins_do_not_join_new_role_files(
    tmp_path: Path, repo: Path, config_dir: Path
):
    a11y = _plugin(
        tmp_path, "a11y", {"a11y-tools": {"command": "uv"}, "a11y-kg": {"command": "uv"}}
    )
    driver = _plugin(tmp_path, "web-driver", {"web-driver-kg": {"command": "uv"}})
    _register(
        config_dir,
        "a11y@accessibility-tools",
        [{"scope": "project", "projectPath": str(repo), "installPath": str(a11y)}],
    )
    _register(
        config_dir,
        "web-driver@web-driver",
        [{"scope": "project", "projectPath": str(repo), "installPath": str(driver)}],
    )
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "a11y@accessibility-tools": True,
                    "web-driver@web-driver": True,
                    "sentinel-swarm@sentinel-swarm": True,
                }
            }
        ),
        encoding="utf-8",
    )

    report = setup.run_setup(repo)

    for role in setup.CORE_ROLES:
        written = setup.role_file(repo, role).read_text(encoding="utf-8")
        template = setup.template_file(role).read_text(encoding="utf-8")
        assert written == setup.render_default(template)
    assert not any("MCP servers" in line for line in report.lines)


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
    assert "not trusted" not in report.text(with_trust=False)


def test_check_trust_prints_only_the_trust_command(
    repo: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    assert setup.main(["--repo", str(repo), "--check-trust"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("is not trusted yet") == 1
    assert not setup.role_file(repo, "oracle").exists()

    key = repo.resolve().as_posix()
    (config_dir / ".claude.json").write_text(
        json.dumps({"projects": {key: {"hasTrustDialogAccepted": True}}}), encoding="utf-8"
    )
    assert setup.main(["--repo", str(repo), "--check-trust"]) == 0


@pytest.fixture
def shim():
    # The shim is a standalone standard-library file, not a module of the package.
    spec = importlib.util.spec_from_file_location("sentinel_swarm_hook_shim", setup.SHIM_TEMPLATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_shim_answers_an_unknown_mode_with_its_usage(repo: Path, config_dir: Path):
    setup.run_setup(repo)

    result = _run_shim(repo, config_dir, "mcp-http", "srv@m", "srv", "5000")

    assert result.returncode == 1
    assert result.stderr.startswith("usage:")
    assert "mcp-http" not in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT resolution is Windows only")
def test_shim_prefers_a_cmd_twin_over_an_extensionless_script(tmp_path: Path, shim):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kg-shim").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "kg-shim.cmd").write_text("@echo off\n", encoding="utf-8")
    found = shim.find_executable(str(bin_dir / "kg-shim"), None)
    assert found is not None
    assert Path(found).name.lower() == "kg-shim.cmd"
