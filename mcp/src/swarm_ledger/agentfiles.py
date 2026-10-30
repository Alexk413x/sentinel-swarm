from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import frontmatter
from .auth import ledger_entry
from .identity import ROLES, LedgerError
from .paths import config_dir, same_path
from .permissions import start_mode

LEDGER_SERVER = "swarm-ledger"
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
CODEBASE_KG_PLUGIN = "codebase-kg@alexk413x"
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_OPTION_VALUE = re.compile(r"[0-9A-Za-z._-]{1,64}")
# headersHelper runs in a shell with the plugin root inside double quotes; a root holding one of
# these would split or expand there, so such an install gets no direct entry.
_UNQUOTABLE = re.compile(r'["$`\r\n]')


def _registry_path() -> Path:
    return config_dir() / "plugins" / "installed_plugins.json"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _enablement(repo_root: Path, plugin_id: str) -> tuple[bool, str]:
    """Claude Code's `enabledPlugins` rule: the highest-precedence file naming the plugin decides.

    Order, highest first: the repo's `settings.local.json`, its `settings.json`, the user's
    `settings.json`. Managed settings and `--settings` also rank above these and are not read.
    Returns whether it is enabled and which file decided: `local`, `project`, `user`, or "".
    """
    for source, path in (
        ("local", repo_root / ".claude" / "settings.local.json"),
        ("project", repo_root / ".claude" / "settings.json"),
        ("user", config_dir() / "settings.json"),
    ):
        data = _read_json(path)
        enabled = data.get("enabledPlugins") if isinstance(data, dict) else None
        value = enabled.get(plugin_id) if isinstance(enabled, dict) else None
        if isinstance(value, bool):
            return value, source
    return False, ""


def _installs(repo_root: Path, plugin_id: str) -> list[dict[str, Any]]:
    """Registry entries for an enabled plugin whose `installPath` is a folder, this repo's first.

    Another project's install counts only when the user's own settings enabled the plugin:
    a repo's committed `settings.json` must not pull in a plugin the user installed elsewhere,
    since its servers are then wired into swarm sessions with their tools pre-allowed.
    """
    enabled, source = _enablement(repo_root, plugin_id)
    if not enabled:
        return []
    data = _read_json(_registry_path())
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    present = [
        entry
        for entry in entries or []
        if isinstance(entry, dict)
        and isinstance(entry.get("installPath"), str)
        and Path(entry["installPath"]).is_dir()
    ]

    def own(entry: dict[str, Any]) -> bool:
        return entry.get("scope") == "user" or same_path(entry.get("projectPath"), repo_root)

    ranked = [
        entry for scope in SCOPES for entry in present if own(entry) and entry.get("scope") == scope
    ]
    if source == "project":
        return ranked
    return ranked + [entry for entry in present if not any(entry is r for r in ranked)]


def plugin_installed(repo_root: Path, plugin_id: str) -> bool:
    return bool(_installs(repo_root, plugin_id))


def install_path(repo_root: Path, plugin_id: str) -> Path | None:
    entries = _installs(repo_root, plugin_id)
    return Path(entries[0]["installPath"]) if entries else None


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
    data = _read_json(config_dir() / "settings.json")
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


def _mcp_file(install: Path, raw: str) -> dict[str, Any] | None:
    """The `mcpServers` map of a manifest's path, None if the path leaves the plugin folder."""
    if not raw or Path(raw).is_absolute():
        return None
    root = install.resolve()
    try:
        target = (root / raw).resolve()
        if not target.is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    data = _read_json(target)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else None


def _declared_servers(install: Path) -> list[dict[str, Any]]:
    """Each `mcpServers` map the plugin declares: the root `.mcp.json`, then the manifest's.

    The manifest's `mcpServers` is an inline object, a path to a JSON file inside the
    plugin, or a list of either.
    """
    maps: list[dict[str, Any]] = []
    data = _read_json(install / ".mcp.json")
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        maps.append(data["mcpServers"])
    manifest = _read_json(install / ".claude-plugin" / "plugin.json")
    declared = manifest.get("mcpServers") if isinstance(manifest, dict) else None
    for item in declared if isinstance(declared, list) else [declared]:
        found = _mcp_file(install, item) if isinstance(item, str) else item
        if isinstance(found, dict):
            maps.append(found)
    return maps


def http_entry(repo_root: Path, plugin_id: str, server: str) -> dict[str, Any] | None:
    """The plugin's HTTP entry for `server`, expanded for `--mcp-config`; None if it has none."""
    install = install_path(repo_root, plugin_id)
    if install is None:
        return None
    entry = next((m[server] for m in _declared_servers(install) if server in m), None)
    if not isinstance(entry, dict) or entry.get("type") != "http":
        return None

    def expand(text: str, options: bool) -> str | None:
        missing = False

        def substitute(match: re.Match[str]) -> str:
            nonlocal missing
            name = match.group(1)
            if name == "CLAUDE_PLUGIN_ROOT" and not _UNQUOTABLE.search(str(install)):
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
    try:
        head, rest = frontmatter.split_document(path.read_text(encoding="utf-8"))
    except frontmatter.MissingFrontmatter:
        raise LedgerError(f"the role file {path} has no frontmatter; {_SETUP_HINT}") from None
    except frontmatter.UnclosedFrontmatter:
        raise LedgerError(
            f"the role file {path} has an unclosed frontmatter block; {_SETUP_HINT}"
        ) from None
    try:
        data = frontmatter.parse(head)
    except frontmatter.FrontmatterError as exc:
        raise LedgerError(f"the role file {path} has invalid YAML: {exc}; {_SETUP_HINT}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"the role file {path} frontmatter is not a mapping; {_SETUP_HINT}")
    body = "\n".join(rest.splitlines()).lstrip("\n")
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
    permission_mode: str | None = None,
) -> list[str]:
    agent_file = read_agent_file(repo_root, role)
    extra = optional_servers(repo_root, role)
    servers = {
        LEDGER_SERVER: ledger_entry(repo_root, ledger_url),
        **{
            name: _direct(repo_root, entry)
            for name, entry in {**mcp_servers(agent_file), **extra}.items()
        },
    }
    config = {"mcpServers": servers}
    options = ["--agent", f"swarm-{role}"]
    if model:
        options += ["--model", model]
    if effort:
        options += ["--effort", effort]
    flag = start_mode(repo_root, role, permission_mode).flag
    if flag:
        options += ["--permission-mode", flag]
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
