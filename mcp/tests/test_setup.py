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


def test_a_hook_new_in_the_template_joins_an_existing_role_file(repo: Path):
    template = setup.template_file("oracle").read_text(encoding="utf-8")
    monitor_entry = (
        '    - matcher: "Monitor"\n'
        "      hooks:\n"
        "        - type: command\n"
        '          command: "python3 .sentinel-swarm/hook.py hook pre_monitor || '
        'python .sentinel-swarm/hook.py hook pre_monitor"\n'
        "          timeout: 60\n"
    )
    assert monitor_entry in template
    old = template.replace(monitor_entry, "").replace("color: cyan", "color: pink")
    path = setup.role_file(repo, "oracle")
    path.parent.mkdir(parents=True)
    path.write_bytes(old.encode("utf-8"))

    report = setup.run_setup(repo)

    merged = path.read_text(encoding="utf-8")
    frontmatter, _ = setup.split_document(merged)
    pre_tool_use = frontmatter.index("  PreToolUse:")
    assert frontmatter.index(monitor_entry) > pre_tool_use
    assert frontmatter.index(monitor_entry) < frontmatter.index("  PostToolUse:")
    assert "color: pink" in frontmatter
    assert frontmatter.count("pre_monitor ||") == 1
    line = next(line for line in report.lines if "swarm-oracle.md" in line)
    assert "hook pre_monitor" in line

    setup.run_setup(repo)
    assert path.read_text(encoding="utf-8") == merged


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

    for role in setup.ROLES:
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


def test_shim_http_command_wraps_a_uv_run_server(shim):
    command = ["/bin/uv", "run", "--project", "/p/mcp", "--frozen", "--no-dev", "codebase-kg"]
    wrapped = shim.http_command(command, 5000, 42)
    entry = ["python", str(Path(shim.__file__).resolve()), "mcp-entry", "5000", "42"]
    assert wrapped == [*command[:6], *entry, "codebase-kg"]

    equals = ["C:/uv/uv.EXE", "run", "--project=/p/mcp", "kg", "graph.db"]
    assert shim.http_command(equals, 1, 0) == [*equals[:3], *entry[:3], "1", "0", "kg", "graph.db"]


def test_shim_http_command_wraps_a_uvx_server(shim):
    command = ["/bin/uvx", "--from", "/p/mcp-kg", "a11y-kg", "/p/mcp-kg/data/graph.json"]
    wrapped = shim.http_command(command, 5000, 42)
    entry = ["python", str(Path(shim.__file__).resolve()), "mcp-entry", "5000", "42"]
    assert wrapped == [*command[:3], *entry, "a11y-kg", "/p/mcp-kg/data/graph.json"]

    assert shim.http_command(["uvx", "pkg"], 5000, 42) == ["uvx", "--from", "pkg", *entry, "pkg"]


@pytest.mark.parametrize(
    "command, problem",
    [
        (["node", "server.js"], "only a uv run or uvx command"),
        (["uv", "pip", "install"], "only a uv run or uvx command"),
        (["uv", "run", "--project", "/p", "--frozen"], "names no console script"),
    ],
)
def test_shim_http_command_refuses_what_it_cannot_wrap(shim, command: list[str], problem: str):
    with pytest.raises(shim.ShimError, match=problem):
        shim.http_command(command, 5000, 0)


class _FakeFastMCP:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, transport: str | None = None, **kwargs: Any) -> None:
        self.calls.append({"transport": transport, **kwargs})


def _fake_server_module(monkeypatch: pytest.MonkeyPatch, shim, with_mcp: bool = True):
    import importlib.metadata
    import types

    module = types.ModuleType("sentinel_fake_kg_server")
    seen: dict[str, Any] = {}
    if with_mcp:
        module.mcp = _FakeFastMCP()  # type: ignore[attr-defined]

    def main() -> None:
        seen["argv"] = list(sys.argv)
        module.mcp.run()  # type: ignore[attr-defined]

    module.main = main  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    point = importlib.metadata.EntryPoint(
        name="fake-kg", value=f"{module.__name__}:main", group="console_scripts"
    )
    monkeypatch.setattr(shim, "console_script", lambda script: point)
    monkeypatch.setattr(sys, "argv", ["hook.py"])
    return module, seen


def test_shim_serve_entry_runs_main_over_http_with_the_original_arguments(
    shim, monkeypatch: pytest.MonkeyPatch
):
    module, seen = _fake_server_module(monkeypatch, shim)

    assert shim.serve_entry(5123, "fake-kg", ["/p/graph.json"]) == 0

    assert seen["argv"] == ["fake-kg", "/p/graph.json"]
    assert module.mcp.calls == [
        {"transport": "http", "host": "127.0.0.1", "port": 5123, "stateless_http": True}
    ]


def test_shim_serve_entry_overrides_a_transport_the_server_passes(
    shim, monkeypatch: pytest.MonkeyPatch
):
    module, _ = _fake_server_module(monkeypatch, shim)
    monkeypatch.setattr(module, "main", lambda: module.mcp.run("stdio", show_banner=False))

    shim.serve_entry(5123, "fake-kg", [])

    assert module.mcp.calls == [
        {
            "transport": "http",
            "host": "127.0.0.1",
            "port": 5123,
            "stateless_http": True,
            "show_banner": False,
        }
    ]


def test_shim_serve_entry_refuses_a_module_without_a_server(shim, monkeypatch: pytest.MonkeyPatch):
    _fake_server_module(monkeypatch, shim, with_mcp=False)
    with pytest.raises(shim.ShimError, match="no server named mcp"):
        shim.serve_entry(5123, "fake-kg", [])


def test_shim_console_script_names_a_missing_script(shim):
    with pytest.raises(shim.ShimError, match="no console script named no-such-script-sentinel"):
        shim.console_script("no-such-script-sentinel")


def test_shim_owner_gone_is_true_for_an_exited_process(shim):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert shim.owner_gone(process.pid) is True


def test_shim_mcp_http_refuses_a_bad_port_and_an_unwrappable_server(
    tmp_path: Path, repo: Path, config_dir: Path
):
    setup.run_setup(repo)
    install = _plugin(tmp_path, "srv", {"srv": {"command": sys.executable, "args": ["-V"]}})
    _register(config_dir, "srv@m", [{"scope": "user", "installPath": str(install)}])

    bad_port = _run_shim(repo, config_dir, "mcp-http", "srv@m", "srv", "port")
    unwrappable = _run_shim(repo, config_dir, "mcp-http", "srv@m", "srv", "5000", "1")

    assert bad_port.returncode == 1
    assert bad_port.stderr.startswith("usage:")
    assert unwrappable.returncode == 1
    assert "cannot serve the MCP server srv over HTTP" in unwrappable.stderr
    assert "only a uv run or uvx command" in unwrappable.stderr
