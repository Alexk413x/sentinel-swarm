from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .identity import ROLES, LedgerError

LEDGER_SERVER = "swarm-ledger"
CHANNEL_SERVER = "swarm-events"
CHANNEL_CONFIG = {"command": "python", "args": [".sentinel-swarm/hook.py", "channel"]}
SESSION_SETTINGS = '{"worktree":{"bgIsolation":"none"}}'
_SETUP_HINT = "run /sentinel-swarm:setup"
OPTIONAL_SERVERS: dict[str, tuple[str, ...]] = {
    "a11y@accessibility-tools": ("a11y-tools", "a11y-kg")
}
# Joined only into the Driver's own session, never any other role's, under the
# plugin-install key, so cartographer's agents and gates see the tool names they grant.
CARTOGRAPHER_PLUGIN = "cartographer@cartographer"
DRIVER_PLUGINS = (
    "android-driver@accessibility-tools",
    "ios-driver@accessibility-tools",
    "web-driver@accessibility-tools",
)
DRIVER_OPTIONAL_SERVERS: dict[str, tuple[str, ...]] = {
    CARTOGRAPHER_PLUGIN: ("cartographer",),
    "android-driver@accessibility-tools": ("android-driver-kg",),
    "ios-driver@accessibility-tools": ("ios-driver-kg",),
    "web-driver@accessibility-tools": ("web-driver-kg",),
}


RELAY_ARGS = [".sentinel-swarm/hook.py", "mcp"]
SCOPES = ("local", "project", "user")
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_OPTION_VALUE = re.compile(r"[0-9A-Za-z._-]{1,64}")


def _config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else Path.home() / ".claude"


def _registry_path() -> Path:
    return _config_dir() / "plugins" / "installed_plugins.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _same_path(raw: object, repo_root: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return os.path.normcase(str(Path(raw).resolve())) == os.path.normcase(
            str(repo_root.resolve())
        )
    except OSError:
        return False


def _installs(repo_root: Path, plugin_id: str) -> list[dict[str, Any]]:
    data = _read_json(_registry_path())
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    return [
        entry
        for entry in entries or []
        if isinstance(entry, dict)
        and (entry.get("scope") == "user" or _same_path(entry.get("projectPath"), repo_root))
    ]


def plugin_installed(repo_root: Path, plugin_id: str) -> bool:
    return bool(_installs(repo_root, plugin_id))


def _install_path(repo_root: Path, plugin_id: str) -> Path | None:
    entries = _installs(repo_root, plugin_id)
    for scope in SCOPES:
        for entry in entries:
            install = entry.get("installPath")
            if entry.get("scope") == scope and isinstance(install, str) and Path(install).is_dir():
                return Path(install)
    return None


def _plugin_option(plugin_id: str, install: Path, key: str) -> str | None:
    """The user's setting for a plugin option, else its default; None unless it is a plain token.

    Only the user's own settings count: a host repo's `.claude/settings.json` could
    otherwise point the URL at another host or put shell text into `headersHelper`.
    """
    manifest = _read_json(install / ".claude-plugin" / "plugin.json")
    user_config = manifest.get("userConfig") if isinstance(manifest, dict) else None
    option = user_config.get(key) if isinstance(user_config, dict) else None
    if not isinstance(option, dict):
        return None
    data = _read_json(_config_dir() / "settings.json")
    configs = data.get("pluginConfigs") if isinstance(data, dict) else None
    config = configs.get(plugin_id) if isinstance(configs, dict) else None
    options = config.get("options") if isinstance(config, dict) else None
    value = options.get(key) if isinstance(options, dict) else None
    if value is None:
        value = option.get("default")
    if option.get("type") == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value != int(value):
            return None
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    return text if _OPTION_VALUE.fullmatch(text) else None


def http_entry(repo_root: Path, plugin_id: str, server: str) -> dict[str, Any] | None:
    """The plugin's HTTP entry for `server`, expanded for `--mcp-config`; None if it has none."""
    install = _install_path(repo_root, plugin_id)
    if install is None:
        return None
    data = _read_json(install / ".mcp.json")
    if not isinstance(data, dict):
        data = _read_json(install / ".claude-plugin" / "plugin.json")
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(server) if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "http":
        return None

    def expand(text: str, options: bool) -> str | None:
        missing = False

        def substitute(match: re.Match[str]) -> str:
            nonlocal missing
            name = match.group(1)
            if name == "CLAUDE_PLUGIN_ROOT":
                return str(install)
            if options and name.startswith("user_config."):
                value = _plugin_option(plugin_id, install, name[len("user_config.") :])
                if value is not None:
                    return value
            missing = True
            return ""

        out = _PLACEHOLDER.sub(substitute, text)
        return None if missing else out

    url = entry.get("url")
    if not isinstance(url, str) or (resolved_url := expand(url, options=True)) is None:
        return None
    try:
        same_host = urlsplit(resolved_url).hostname == urlsplit(_PLACEHOLDER.sub("0", url)).hostname
    except ValueError:
        return None
    if not same_host:
        return None
    resolved: dict[str, Any] = {"type": "http", "url": resolved_url}
    helper = entry.get("headersHelper")
    if helper is not None:
        if (
            not isinstance(helper, str)
            or (resolved_helper := expand(helper, options=False)) is None
        ):
            return None
        resolved["headersHelper"] = resolved_helper
    return resolved


def _direct(repo_root: Path, entry: Any) -> Any:
    """A relay entry as the plugin's own HTTP entry when it has one, so no relay process starts."""
    args = entry.get("args") if isinstance(entry, dict) else None
    if not isinstance(args, list) or len(args) != 4 or args[:2] != RELAY_ARGS:
        return entry
    return http_entry(repo_root, str(args[2]), str(args[3])) or entry


def _plugin_key(plugin_id: str, server: str) -> str:
    return f"plugin_{plugin_id.split('@', 1)[0]}_{server}"


def optional_servers(repo_root: Path, role: str | None = None) -> dict[str, Any]:
    def entry(plugin: str, server: str) -> dict[str, Any]:
        return {"command": "python", "args": [".sentinel-swarm/hook.py", "mcp", plugin, server]}

    servers = {
        server: entry(plugin, server)
        for plugin, names in OPTIONAL_SERVERS.items()
        if plugin_installed(repo_root, plugin)
        for server in names
    }
    if role == "driver":
        servers.update(
            {
                _plugin_key(plugin, server): entry(plugin, server)
                for plugin, names in DRIVER_OPTIONAL_SERVERS.items()
                if plugin_installed(repo_root, plugin)
                for server in names
            }
        )
    return servers


def driver_available(repo_root: Path) -> bool:
    return plugin_installed(repo_root, CARTOGRAPHER_PLUGIN) and any(
        plugin_installed(repo_root, plugin) for plugin in DRIVER_PLUGINS
    )


def agent_file_path(repo_root: Path, role: str) -> Path:
    return repo_root / ".claude" / "agents" / f"swarm-{role}.md"


def read_agent_file(repo_root: Path, role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise LedgerError(f"unknown role {role!r}; roles are {ROLES}")
    path = agent_file_path(repo_root, role)
    if not path.is_file():
        raise LedgerError(f"the role file {path} is missing; {_SETUP_HINT}")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise LedgerError(f"the role file {path} has no frontmatter; {_SETUP_HINT}")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise LedgerError(f"the role file {path} has an unclosed frontmatter block; {_SETUP_HINT}")
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise LedgerError(f"the role file {path} has invalid YAML: {exc}; {_SETUP_HINT}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"the role file {path} frontmatter is not a mapping; {_SETUP_HINT}")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return {**data, "body": body}


def oracle_model(repo_root: Path, approved: list[str]) -> str | None:
    try:
        model = read_agent_file(repo_root, "oracle").get("model")
    except LedgerError:
        model = None
    if model:
        return str(model)
    return approved[0] if approved else None


def tool_list(agent_file: dict[str, Any]) -> list[str]:
    raw = agent_file.get("tools")
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else [str(item) for item in raw]
    return [item.strip() for item in items if item.strip()]


def mcp_servers(agent_file: dict[str, Any]) -> dict[str, Any]:
    raw = agent_file.get("mcpServers")
    entries = raw if isinstance(raw, list) else [raw]
    servers: dict[str, Any] = {}
    for entry in entries:
        if isinstance(entry, dict):
            servers.update(entry)
    return servers


def session_options(
    repo_root: Path,
    role: str,
    model: str | None,
    ledger_url: str,
    *,
    effort: str | None = None,
    prompt_cache_ttl: str | None = None,
    channel: bool = False,
) -> list[str]:
    agent_file = read_agent_file(repo_root, role)
    extra = optional_servers(repo_root, role)
    servers = {
        LEDGER_SERVER: {"type": "http", "url": ledger_url},
        **{
            name: _direct(repo_root, entry)
            for name, entry in {**mcp_servers(agent_file), **extra}.items()
        },
    }
    if channel:
        servers[CHANNEL_SERVER] = dict(CHANNEL_CONFIG)
    config = {"mcpServers": servers}
    options = ["--agent", f"swarm-{role}"]
    if model:
        options += ["--model", model]
    if effort:
        options += ["--effort", effort]
    permission_mode = agent_file.get("permissionMode")
    if permission_mode:
        options += ["--permission-mode", str(permission_mode)]
    options += ["--strict-mcp-config", "--mcp-config", json.dumps(config)]
    tools = tool_list(agent_file)
    if tools:
        tools += [f"mcp__{server}" for server in extra if f"mcp__{server}" not in tools]
        options += ["--allowedTools", ",".join(tools)]
    settings_json = SESSION_SETTINGS
    if prompt_cache_ttl:
        payload = json.loads(SESSION_SETTINGS)
        payload["promptCacheTtl"] = prompt_cache_ttl
        settings_json = json.dumps(payload)
    options += ["--settings", settings_json]
    return options
