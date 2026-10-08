from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_ledger import auth
from swarm_ledger.agentfiles import (
    SESSION_SETTINGS,
    agent_file_path,
    mcp_servers,
    read_agent_file,
    session_options,
    tool_list,
)
from swarm_ledger.identity import LedgerError

_CODER = """---
name: swarm-coder
description: Writes one file.
model: sonnet
color: orange
permissionMode: acceptEdits
tools: Read, Write, SendMessage, mcp__swarm-ledger
mcpServers:
  - codebase-kg:
      command: python
      args: [".sentinel-swarm/hook.py", "mcp", "codebase-kg@alexk413x", "codebase-kg"]
hooks:
  Stop:
    - hooks:
        - type: command
          command: "python .sentinel-swarm/hook.py hook stop"
---

# Coder

You own one file.
"""


def _write(root: Path, role: str, text: str) -> Path:
    path = agent_file_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    auth.ensure_token(root)
    return path


def _ledger_entry(root: Path, url: str) -> dict:
    return {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {auth.read_token(root)}"},
    }


def test_read_agent_file_returns_the_frontmatter_and_the_body(tmp_path: Path) -> None:
    _write(tmp_path, "coder", _CODER)
    data = read_agent_file(tmp_path, "coder")
    assert data["name"] == "swarm-coder"
    assert data["permissionMode"] == "acceptEdits"
    assert data["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"
    assert data["body"] == "# Coder\n\nYou own one file."


def test_read_agent_file_names_setup_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match=r"swarm-lead\.md is missing; run /sentinel-swarm:setup"):
        read_agent_file(tmp_path, "lead")


@pytest.mark.parametrize(
    "text, problem",
    [
        ("# no frontmatter\n", "has no frontmatter"),
        ("---\nname: swarm-lead\n", "unclosed frontmatter"),
        ("---\nname: [unclosed\n---\n", "invalid YAML"),
        ("---\n- a list\n---\n", "is not a mapping"),
    ],
)
def test_read_agent_file_refuses_a_malformed_file(tmp_path: Path, text: str, problem: str) -> None:
    _write(tmp_path, "lead", text)
    with pytest.raises(LedgerError, match=problem) as excinfo:
        read_agent_file(tmp_path, "lead")
    assert "run /sentinel-swarm:setup" in str(excinfo.value)


def test_read_agent_file_refuses_an_unknown_role(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="unknown role"):
        read_agent_file(tmp_path, "intern")


def test_tool_list_accepts_a_comma_string_or_a_list() -> None:
    assert tool_list({"tools": "Read, Write ,,Grep"}) == ["Read", "Write", "Grep"]
    assert tool_list({"tools": ["Read", " Write "]}) == ["Read", "Write"]
    assert tool_list({}) == []


def test_mcp_servers_accepts_a_list_of_entries_or_a_mapping() -> None:
    entries = {"mcpServers": [{"a": {"command": "x"}}, "by-name-only", {"b": {"command": "y"}}]}
    assert mcp_servers(entries) == {"a": {"command": "x"}, "b": {"command": "y"}}
    assert mcp_servers({"mcpServers": {"a": {"command": "x"}}}) == {"a": {"command": "x"}}
    assert mcp_servers({}) == {}


def test_session_options_builds_the_flags_from_the_role_file(tmp_path: Path) -> None:
    _write(tmp_path, "coder", _CODER)
    options = session_options(tmp_path, "coder", "haiku", "http://127.0.0.1:5000/mcp")

    config = json.loads(options[options.index("--mcp-config") + 1])
    assert config == {
        "mcpServers": {
            "swarm-ledger": _ledger_entry(tmp_path, "http://127.0.0.1:5000/mcp"),
            "codebase-kg": {
                "command": "python",
                "args": [
                    ".sentinel-swarm/hook.py",
                    "mcp",
                    "codebase-kg@alexk413x",
                    "codebase-kg",
                ],
            },
        }
    }
    assert options == [
        "--agent",
        "swarm-coder",
        "--model",
        "haiku",
        "--permission-mode",
        "acceptEdits",
        "--strict-mcp-config",
        "--mcp-config",
        options[options.index("--mcp-config") + 1],
        "--allowedTools",
        "Read,Write,SendMessage,mcp__swarm-ledger",
        "--settings",
        SESSION_SETTINGS,
    ]
    assert json.loads(SESSION_SETTINGS) == {"worktree": {"bgIsolation": "none"}}


def test_session_options_leaves_out_what_the_file_does_not_set(tmp_path: Path) -> None:
    _write(tmp_path, "lead", "---\nname: swarm-lead\n---\nbody\n")
    options = session_options(tmp_path, "lead", None, "http://127.0.0.1:1/mcp")
    assert "--model" not in options
    assert "--permission-mode" not in options
    assert "--allowedTools" not in options
    assert options[:2] == ["--agent", "swarm-lead"]


def _registry(config_dir: Path, entries: list[dict]) -> None:
    path = config_dir / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": {"a11y@accessibility-tools": entries}}), "utf-8")


def test_session_options_add_a11y_only_when_the_host_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _write(host, "coder", _CODER)

    def a11y_parts() -> tuple[set[str], str]:
        options = session_options(host, "coder", None, "http://127.0.0.1:1/mcp")
        servers = json.loads(options[options.index("--mcp-config") + 1])["mcpServers"]
        return set(servers), options[options.index("--allowedTools") + 1]

    _registry(config_dir, [{"scope": "project", "projectPath": str(tmp_path / "other")}])
    servers, tools = a11y_parts()
    assert "a11y-tools" not in servers
    assert "mcp__a11y" not in tools

    _registry(config_dir, [{"scope": "project", "projectPath": str(host)}])
    servers, tools = a11y_parts()
    assert {"a11y-tools", "a11y-kg"} <= servers
    assert tools.endswith(",mcp__a11y-tools,mcp__a11y-kg")


def test_session_options_run_every_plugin_server_through_the_stdio_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _registry(config_dir, [{"scope": "user"}])
    _write(host, "coder", _CODER)

    options = session_options(host, "coder", None, "http://127.0.0.1:1/mcp")
    servers = json.loads(options[options.index("--mcp-config") + 1])["mcpServers"]

    assert servers["swarm-ledger"] == _ledger_entry(host, "http://127.0.0.1:1/mcp")
    assert servers["codebase-kg"]["args"] == [
        ".sentinel-swarm/hook.py",
        "mcp",
        "codebase-kg@alexk413x",
        "codebase-kg",
    ]
    for name in ("a11y-tools", "a11y-kg"):
        assert servers[name] == {
            "command": "python",
            "args": [".sentinel-swarm/hook.py", "mcp", "a11y@accessibility-tools", name],
        }


def _kg_install(root: Path, port_default: int = 47821) -> Path:
    install = root / "kg-install"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "codebase-kg": {
                        "type": "http",
                        "url": "http://127.0.0.1:${user_config.server_port}/mcp",
                        "headersHelper": 'py -3 "${CLAUDE_PLUGIN_ROOT}/mcp/launch/kg_headers.py"',
                    }
                }
            }
        ),
        "utf-8",
    )
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "codebase-kg",
                "userConfig": {"server_port": {"type": "number", "default": port_default}},
            }
        ),
        "utf-8",
    )
    return install


def _kg_registry(config_dir: Path, install: Path) -> None:
    path = config_dir / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"plugins": {"codebase-kg@alexk413x": [{"scope": "user", "installPath": str(install)}]}}
        ),
        "utf-8",
    )


def _servers(host: Path) -> dict:
    options = session_options(host, "coder", None, "http://127.0.0.1:1/mcp")
    return json.loads(options[options.index("--mcp-config") + 1])["mcpServers"]


def test_session_options_connect_an_http_plugin_server_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    install = _kg_install(tmp_path)
    _kg_registry(config_dir, install)
    _write(host, "coder", _CODER)

    assert _servers(host)["codebase-kg"] == {
        "type": "http",
        "url": "http://127.0.0.1:47821/mcp",
        "headersHelper": f'py -3 "{install}/mcp/launch/kg_headers.py"',
    }


def test_session_options_use_the_users_port_setting_not_the_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _kg_registry(config_dir, _kg_install(tmp_path))
    _write(host, "coder", _CODER)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {"pluginConfigs": {"codebase-kg@alexk413x": {"options": {"server_port": 47900}}}}
        ),
        "utf-8",
    )
    (host / ".claude" / "settings.local.json").write_text(
        json.dumps(
            {"pluginConfigs": {"codebase-kg@alexk413x": {"options": {"server_port": 47901}}}}
        ),
        "utf-8",
    )

    assert _servers(host)["codebase-kg"]["url"] == "http://127.0.0.1:47900/mcp"


@pytest.mark.parametrize("port", ["1@evil.example", "47821/x", "47821; calc", True, 47821.5])
def test_session_options_keep_the_relay_for_an_unsafe_port_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: object
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    _kg_registry(config_dir, _kg_install(tmp_path))
    _write(host, "coder", _CODER)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {"pluginConfigs": {"codebase-kg@alexk413x": {"options": {"server_port": port}}}}
        ),
        "utf-8",
    )

    assert _servers(host)["codebase-kg"]["args"][:2] == [".sentinel-swarm/hook.py", "mcp"]


def test_session_options_keep_the_relay_for_an_unresolved_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    install = _kg_install(tmp_path)
    manifest = install / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "codebase-kg"}), "utf-8")
    _kg_registry(config_dir, install)
    _write(host, "coder", _CODER)

    assert _servers(host)["codebase-kg"]["args"][:2] == [".sentinel-swarm/hook.py", "mcp"]


def test_session_options_send_the_repos_token_as_a_static_header(tmp_path: Path) -> None:
    _write(tmp_path, "coder", _CODER)
    token = auth.read_token(tmp_path)
    options = session_options(tmp_path, "coder", None, "http://127.0.0.1:5000/mcp")
    entry = json.loads(options[options.index("--mcp-config") + 1])["mcpServers"]["swarm-ledger"]
    assert entry["headers"] == {"Authorization": f"Bearer {token}"}
    assert "headersHelper" not in entry
    assert auth.ensure_token(tmp_path) == token


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:5000/mcp",
        "http://10.0.0.2:5000/mcp",
        "https://127.0.0.1:5000/mcp",
        "http://127.0.0.1:5000/other",
        "http://127.0.0.1:99999/mcp",
        "http://127.0.0.1:5000@evil.example/mcp",
    ],
)
def test_session_options_refuse_a_ledger_url_off_loopback(tmp_path: Path, url: str) -> None:
    _write(tmp_path, "coder", _CODER)
    with pytest.raises(LedgerError, match="is not http://127.0.0.1:<port>/mcp"):
        session_options(tmp_path, "coder", None, url)


@pytest.mark.parametrize("token", ["", "short", "a" * 31, "a" * 129, "has space " + "a" * 40])
def test_session_options_refuse_a_missing_or_malformed_token(tmp_path: Path, token: str) -> None:
    _write(tmp_path, "coder", _CODER)
    path = auth.token_path(tmp_path)
    if token:
        path.write_text(token, "utf-8")
    else:
        path.unlink()
    with pytest.raises(LedgerError, match="ledger token"):
        session_options(tmp_path, "coder", None, "http://127.0.0.1:5000/mcp")
