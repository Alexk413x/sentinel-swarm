from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from helpers import enable_plugins, write_registry

from swarm_ledger import auth
from swarm_ledger.agentfiles import (
    SESSION_SETTINGS,
    agent_file_path,
    driver_available,
    http_entry,
    install_path,
    mcp_servers,
    plugin_installed,
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


def _registry(config_dir: Path, entries: list[dict], enable: bool = True) -> None:
    install = config_dir / "plugins" / "cache" / "a11y"
    install.mkdir(parents=True, exist_ok=True)
    installed = [{**entry, "installPath": str(install)} for entry in entries]
    write_registry({"a11y@accessibility-tools": installed}, config_dir, enable=enable)


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

    other = [{"scope": "project", "projectPath": str(tmp_path / "other")}]
    _registry(config_dir, other, enable=False)
    servers, tools = a11y_parts()
    assert "a11y-tools" not in servers
    assert "mcp__a11y" not in tools

    _registry(config_dir, other)
    servers, tools = a11y_parts()
    assert {"a11y-tools", "a11y-kg"} <= servers
    assert tools.endswith(",mcp__a11y-tools,mcp__a11y-kg")

    enable_plugins(["a11y@accessibility-tools"], host / ".claude" / "settings.local.json", False)
    servers, tools = a11y_parts()
    assert "a11y-tools" not in servers
    assert "mcp__a11y" not in tools


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
    entry = {"scope": "user", "installPath": str(install)}
    write_registry({"codebase-kg@alexk413x": [entry]}, config_dir)


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
            {
                "enabledPlugins": {"codebase-kg@alexk413x": True},
                "pluginConfigs": {"codebase-kg@alexk413x": {"options": {"server_port": 47900}}},
            }
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


_HELPER = (
    'command -v py >/dev/null 2>&1 && py -3 "${CLAUDE_PLUGIN_ROOT}/mcp/launch/@SCRIPT@" '
    '|| python3 "${CLAUDE_PLUGIN_ROOT}/mcp/launch/@SCRIPT@" '
    '|| python "${CLAUDE_PLUGIN_ROOT}/mcp/launch/@SCRIPT@"'
)


def _helper(script: str, install: Path | None = None) -> str:
    text = _HELPER.replace("@SCRIPT@", script)
    return text if install is None else text.replace("${CLAUDE_PLUGIN_ROOT}", str(install))


def _http_server(port_key: str, script: str) -> dict:
    return {
        "type": "http",
        "url": f"http://127.0.0.1:${{user_config.{port_key}}}/mcp",
        "headersHelper": _helper(script),
    }


def _manifest(
    name: str, ports: dict[str, int], mcp_servers: object = "./mcp/claude.mcp.json"
) -> dict:
    return {
        "name": name,
        "mcpServers": mcp_servers,
        "userConfig": {
            key: {"type": "number", "default": default} for key, default in ports.items()
        },
    }


def _plugin(root: Path, name: str, manifest: dict, files: dict[str, object] | None = None) -> Path:
    install = root / f"{name}-install"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), "utf-8")
    for rel, content in (files or {}).items():
        target = install / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(content), "utf-8")
    return install


def _register(config_dir: Path, installs: dict[str, Path]) -> None:
    plugins = {pid: [{"scope": "user", "installPath": str(p)}] for pid, p in installs.items()}
    write_registry(plugins, config_dir)


# plugin id, manifest ports, server -> (port key, helper script), server -> expected port
_LAYOUTS = [
    (
        "codebase-kg@alexk413x",
        {"server_port": 47821, "max_workers": 8},
        {"codebase-kg": ("server_port", "kg_headers.py")},
        {"codebase-kg": 47821},
    ),
    (
        "cartographer@cartographer",
        {"server_port": 47822},
        {"cartographer": ("server_port", "cartographer_headers.py")},
        {"cartographer": 47822},
    ),
    (
        "web-driver@accessibility-tools",
        {"server_port": 47827},
        {"web-driver-kg": ("server_port", "web_driver_kg_headers.py")},
        {"web-driver-kg": 47827},
    ),
    (
        "android-driver@accessibility-tools",
        {"server_port": 47825},
        {"android-driver-kg": ("server_port", "android_driver_kg_headers.py")},
        {"android-driver-kg": 47825},
    ),
    (
        "ios-driver@accessibility-tools",
        {"server_port": 47826},
        {"ios-driver-kg": ("server_port", "ios_driver_kg_headers.py")},
        {"ios-driver-kg": 47826},
    ),
    (
        "a11y@accessibility-tools",
        {"tools_server_port": 47823, "kg_server_port": 47824, "max_workers": 2},
        {
            "a11y-tools": ("tools_server_port", "a11y_mcp_headers.py"),
            "a11y-kg": ("kg_server_port", "a11y_kg_headers.py"),
        },
        {"a11y-tools": 47823, "a11y-kg": 47824},
    ),
]


@pytest.mark.parametrize(("plugin_id", "ports", "servers", "expected"), _LAYOUTS)
def test_http_entry_follows_the_manifests_mcp_file_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_id: str,
    ports: dict[str, int],
    servers: dict[str, tuple[str, str]],
    expected: dict[str, int],
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    name = plugin_id.split("@", 1)[0]
    mcp_file = {
        "mcpServers": {n: _http_server(key, script) for n, (key, script) in servers.items()}
    }
    install = _plugin(tmp_path, name, _manifest(name, ports), {"mcp/claude.mcp.json": mcp_file})
    _register(config_dir, {plugin_id: install})

    for server, port in expected.items():
        assert http_entry(tmp_path, plugin_id, server) == {
            "type": "http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "headersHelper": _helper(servers[server][1], install),
        }


def test_http_entry_resolves_only_the_plugin_root_in_the_headers_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {"mcpServers": {"web-driver-kg": _http_server("server_port", "h.py")}}
    install = _plugin(
        tmp_path,
        "web-driver",
        _manifest("web-driver", {"server_port": 47827}),
        {"mcp/claude.mcp.json": mcp_file},
    )
    _register(config_dir, {"web-driver@accessibility-tools": install})

    entry = http_entry(tmp_path, "web-driver@accessibility-tools", "web-driver-kg")

    assert entry is not None
    assert "${" not in entry["headersHelper"]
    assert entry["headersHelper"].count(f'"{install}/mcp/launch/h.py"') == 3


def test_http_entry_applies_a_users_override_to_the_port_that_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {
        "mcpServers": {
            "a11y-tools": _http_server("tools_server_port", "a.py"),
            "a11y-kg": _http_server("kg_server_port", "b.py"),
        }
    }
    ports = {"tools_server_port": 47823, "kg_server_port": 47824}
    install = _plugin(tmp_path, "a11y", _manifest("a11y", ports), {"mcp/claude.mcp.json": mcp_file})
    _register(config_dir, {"a11y@accessibility-tools": install})
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {"a11y@accessibility-tools": True},
                "pluginConfigs": {
                    "a11y@accessibility-tools": {"options": {"kg_server_port": 48000}}
                },
            }
        ),
        "utf-8",
    )

    tools = http_entry(tmp_path, "a11y@accessibility-tools", "a11y-tools")
    kg = http_entry(tmp_path, "a11y@accessibility-tools", "a11y-kg")

    assert tools is not None
    assert tools["url"] == "http://127.0.0.1:47823/mcp"
    assert kg is not None
    assert kg["url"] == "http://127.0.0.1:48000/mcp"


def test_http_entry_reads_an_inline_manifest_object_and_a_list_of_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    inline = {"inline-kg": _http_server("server_port", "i.py")}
    listed = {"mcpServers": {"listed-kg": _http_server("server_port", "l.py")}}
    install = _plugin(
        tmp_path,
        "mixed",
        _manifest("mixed", {"server_port": 47900}, [inline, "./extra.json"]),
        {"extra.json": listed},
    )
    _register(config_dir, {"mixed@m": install})

    for server in ("inline-kg", "listed-kg"):
        entry = http_entry(tmp_path, "mixed@m", server)
        assert entry is not None
        assert entry["url"] == "http://127.0.0.1:47900/mcp"


@pytest.mark.parametrize(
    "path", ["../outside.json", "./../outside.json", "./sub/../../outside.json", "ABSOLUTE", ""]
)
def test_http_entry_rejects_a_manifest_path_that_leaves_the_plugin_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    outside = {"mcpServers": {"kg": _http_server("server_port", "k.py")}}
    (tmp_path / "outside.json").write_text(json.dumps(outside), "utf-8")
    if path == "ABSOLUTE":
        path = str(tmp_path / "outside.json")
    install = _plugin(tmp_path, "kg", _manifest("kg", {"server_port": 47821}, path))
    (install / "sub").mkdir()
    _register(config_dir, {"kg@m": install})

    assert http_entry(tmp_path, "kg@m", "kg") is None


def test_http_entry_is_none_when_the_manifests_file_is_missing_or_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    install = _plugin(tmp_path, "kg", _manifest("kg", {"server_port": 47821}))
    _register(config_dir, {"kg@m": install})
    assert http_entry(tmp_path, "kg@m", "kg") is None

    (install / "mcp").mkdir()
    (install / "mcp" / "claude.mcp.json").write_text("{not json", "utf-8")
    assert http_entry(tmp_path, "kg@m", "kg") is None


def test_http_entry_is_none_for_a_stdio_entry_or_an_unknown_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {
        "mcpServers": {
            "kg": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/relay.py"]},
            "other": _http_server("server_port", "o.py"),
        }
    }
    install = _plugin(
        tmp_path, "kg", _manifest("kg", {"server_port": 47821}), {"mcp/claude.mcp.json": mcp_file}
    )
    _register(config_dir, {"kg@m": install})

    assert http_entry(tmp_path, "kg@m", "kg") is None
    assert http_entry(tmp_path, "kg@m", "missing") is None
    assert http_entry(tmp_path, "kg@m", "other") is not None


def test_http_entry_is_none_for_an_unresolved_placeholder_in_the_url_or_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {
        "mcpServers": {
            "url-var": {"type": "http", "url": "http://127.0.0.1:${SOME_PORT}/mcp"},
            "unknown-key": _http_server("no_such_key", "k.py"),
            "helper-option": {
                "type": "http",
                "url": "http://127.0.0.1:${user_config.server_port}/mcp",
                "headersHelper": 'py -3 "${user_config.server_port}"',
            },
        }
    }
    install = _plugin(
        tmp_path, "kg", _manifest("kg", {"server_port": 47821}), {"mcp/claude.mcp.json": mcp_file}
    )
    _register(config_dir, {"kg@m": install})

    for server in mcp_file["mcpServers"]:
        assert http_entry(tmp_path, "kg@m", server) is None


def test_session_options_connect_every_server_of_a_manifest_path_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {
        "mcpServers": {
            "a11y-tools": _http_server("tools_server_port", "a.py"),
            "a11y-kg": _http_server("kg_server_port", "b.py"),
        }
    }
    ports = {"tools_server_port": 47823, "kg_server_port": 47824}
    install = _plugin(tmp_path, "a11y", _manifest("a11y", ports), {"mcp/claude.mcp.json": mcp_file})
    _register(config_dir, {"a11y@accessibility-tools": install})
    _write(host, "coder", _CODER)

    servers = _servers(host)

    assert servers["a11y-tools"]["url"] == "http://127.0.0.1:47823/mcp"
    assert servers["a11y-kg"]["url"] == "http://127.0.0.1:47824/mcp"
    for name in ("a11y-tools", "a11y-kg"):
        assert servers[name]["type"] == "http"
        assert str(install) in servers[name]["headersHelper"]


@pytest.mark.parametrize(
    "folder",
    [
        "with$dollar",
        "with`tick",
        pytest.param(
            'with"quote',
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows folders cannot hold a quote"),
        ),
    ],
)
def test_http_entry_is_none_when_the_install_path_cannot_sit_inside_shell_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    mcp_file = {"mcpServers": {"kg": _http_server("server_port", "k.py")}}
    install = _plugin(
        tmp_path / folder,
        "kg",
        _manifest("kg", {"server_port": 47821}),
        {"mcp/claude.mcp.json": mcp_file},
    )
    _register(config_dir, {"kg@m": install})

    assert http_entry(tmp_path, "kg@m", "kg") is None


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


# -- plugin detection: enabled in settings and installed in the registry --------------------

_PLUGIN = "cartographer@cartographer"


@pytest.fixture
def detect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    host = tmp_path / "host"
    config_dir = tmp_path / "config"
    host.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return host, config_dir


def _install(config_dir: Path, name: str) -> Path:
    install = config_dir / "plugins" / "cache" / name
    install.mkdir(parents=True)
    return install


def _set_enabled(path: Path, value: bool, plugin_id: str = _PLUGIN) -> None:
    enable_plugins([plugin_id], path, value)


@pytest.mark.parametrize("enabled_in", ["user", "local"])
def test_a_plugin_the_user_enabled_counts_from_another_projects_install(
    detect: tuple[Path, Path], tmp_path: Path, enabled_in: str
) -> None:
    host, config_dir = detect
    install = _install(config_dir, "carto")
    entry = {
        "scope": "local",
        "projectPath": str(tmp_path / "elsewhere"),
        "installPath": str(install),
    }
    write_registry({_PLUGIN: [entry]}, config_dir, enable=False)
    settings = {
        "user": config_dir / "settings.json",
        "local": host / ".claude" / "settings.local.json",
    }[enabled_in]
    _set_enabled(settings, True)

    assert plugin_installed(host, _PLUGIN) is True
    assert install_path(host, _PLUGIN) == install


def test_committed_settings_cannot_reach_another_projects_install(
    detect: tuple[Path, Path], tmp_path: Path
) -> None:
    host, config_dir = detect
    elsewhere = {
        "scope": "local",
        "projectPath": str(tmp_path / "elsewhere"),
        "installPath": str(_install(config_dir, "carto")),
    }
    write_registry({_PLUGIN: [elsewhere]}, config_dir, enable=False)
    _set_enabled(host / ".claude" / "settings.json", True)
    assert plugin_installed(host, _PLUGIN) is False

    own = {
        "scope": "project",
        "projectPath": str(host),
        "installPath": str(_install(config_dir, "own")),
    }
    write_registry({_PLUGIN: [elsewhere, own]}, config_dir, enable=False)
    assert install_path(host, _PLUGIN) == config_dir / "plugins" / "cache" / "own"


def test_local_settings_that_disable_a_plugin_beat_project_settings_that_enable_it(
    detect: tuple[Path, Path],
) -> None:
    host, config_dir = detect
    install = _install(config_dir, "carto")
    entry = {"scope": "project", "projectPath": str(host), "installPath": str(install)}
    write_registry({_PLUGIN: [entry]}, config_dir, enable=False)
    _set_enabled(host / ".claude" / "settings.json", True)
    _set_enabled(host / ".claude" / "settings.local.json", False)

    assert plugin_installed(host, _PLUGIN) is False
    assert install_path(host, _PLUGIN) is None


def test_a_plugin_enabled_nowhere_does_not_count(detect: tuple[Path, Path]) -> None:
    host, config_dir = detect
    entry = {"scope": "user", "installPath": str(_install(config_dir, "carto"))}
    write_registry({_PLUGIN: [entry]}, config_dir, enable=False)
    _set_enabled(host / ".claude" / "settings.json", True, "other@plugin")

    assert plugin_installed(host, _PLUGIN) is False


@pytest.mark.parametrize(
    ("user", "project", "local", "expected"),
    [
        (True, None, None, True),
        (True, False, None, False),
        (False, True, None, True),
        (False, True, False, False),
        (False, False, True, True),
        (None, None, False, False),
        (True, None, False, False),
    ],
)
def test_the_highest_settings_file_that_names_the_plugin_decides(
    detect: tuple[Path, Path],
    user: bool | None,
    project: bool | None,
    local: bool | None,
    expected: bool,
) -> None:
    host, config_dir = detect
    entry = {"scope": "user", "installPath": str(_install(config_dir, "carto"))}
    write_registry({_PLUGIN: [entry]}, config_dir, enable=False)
    for path, value in (
        (config_dir / "settings.json", user),
        (host / ".claude" / "settings.json", project),
        (host / ".claude" / "settings.local.json", local),
    ):
        if value is not None:
            _set_enabled(path, value)

    assert plugin_installed(host, _PLUGIN) is expected


def test_an_enabled_plugin_without_a_usable_install_does_not_count(
    detect: tuple[Path, Path], tmp_path: Path
) -> None:
    host, config_dir = detect
    _set_enabled(host / ".claude" / "settings.json", True)

    assert plugin_installed(host, _PLUGIN) is False

    gone = {"scope": "user", "installPath": str(tmp_path / "gone")}
    write_registry({_PLUGIN: [gone, {"scope": "user"}]}, config_dir, enable=False)
    assert plugin_installed(host, _PLUGIN) is False
    assert install_path(host, _PLUGIN) is None


def test_install_path_prefers_this_repos_entry_then_falls_back_to_any_existing_one(
    detect: tuple[Path, Path], tmp_path: Path
) -> None:
    host, config_dir = detect
    other = {
        "scope": "local",
        "projectPath": str(tmp_path / "x"),
        "installPath": str(_install(config_dir, "other")),
    }
    mine = {
        "scope": "project",
        "projectPath": str(host),
        "installPath": str(_install(config_dir, "mine")),
    }
    user = {"scope": "user", "installPath": str(_install(config_dir, "user"))}
    _set_enabled(config_dir / "settings.json", True)

    write_registry({_PLUGIN: [other, user, mine]}, config_dir, enable=False)
    assert install_path(host, _PLUGIN) == config_dir / "plugins" / "cache" / "mine"

    write_registry({_PLUGIN: [other, user]}, config_dir, enable=False)
    assert install_path(host, _PLUGIN) == config_dir / "plugins" / "cache" / "user"

    write_registry({_PLUGIN: [other]}, config_dir, enable=False)
    assert install_path(host, _PLUGIN) == config_dir / "plugins" / "cache" / "other"


def test_driver_available_follows_the_enabled_and_installed_rule(
    detect: tuple[Path, Path], tmp_path: Path
) -> None:
    host, config_dir = detect
    elsewhere = str(tmp_path / "elsewhere")
    android = "android-driver@accessibility-tools"
    web = "web-driver@accessibility-tools"
    plugins = {
        pid: [
            {
                "scope": "project",
                "projectPath": elsewhere,
                "installPath": str(_install(config_dir, name)),
            }
        ]
        for pid, name in ((_PLUGIN, "carto"), (android, "android"), (web, "web"))
    }
    write_registry(plugins, config_dir, enable=False)
    user = config_dir / "settings.json"
    local = host / ".claude" / "settings.local.json"
    _set_enabled(user, True)
    _set_enabled(user, True, android)
    assert driver_available(host) is True

    _set_enabled(local, False, android)
    assert driver_available(host) is False

    _set_enabled(user, True, web)
    assert driver_available(host) is True

    _set_enabled(local, False)
    assert driver_available(host) is False
