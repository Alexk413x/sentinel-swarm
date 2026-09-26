from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .identity import ROLES, LedgerError

LEDGER_SERVER = "swarm-ledger"
SESSION_SETTINGS = '{"worktree":{"bgIsolation":"none"}}'
_SETUP_HINT = "run /sentinel-swarm:setup"
OPTIONAL_SERVERS = {"a11y@accessibility-tools": ("a11y-tools", "a11y-kg")}


def _registry_path() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(raw) if raw else Path.home() / ".claude"
    return base / "plugins" / "installed_plugins.json"


def _same_path(raw: object, repo_root: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return os.path.normcase(str(Path(raw).resolve())) == os.path.normcase(
            str(repo_root.resolve())
        )
    except OSError:
        return False


def plugin_installed(repo_root: Path, plugin_id: str) -> bool:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    return any(
        isinstance(entry, dict)
        and (entry.get("scope") == "user" or _same_path(entry.get("projectPath"), repo_root))
        for entry in entries or []
    )


def optional_servers(repo_root: Path) -> dict[str, Any]:
    return {
        server: {"command": "python", "args": [".sentinel-swarm/hook.py", "mcp", plugin, server]}
        for plugin, servers in OPTIONAL_SERVERS.items()
        if plugin_installed(repo_root, plugin)
        for server in servers
    }


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


def shared_servers(repo_root: Path) -> dict[str, Any]:
    # Imported here, not at the top: serve and shared import this module.
    from .serve import read_server_info
    from .shared import answering_urls

    info = read_server_info(repo_root)
    urls = answering_urls(info.get("servers") if info else None)
    return {name: {"type": "http", "url": url} for name, url in urls.items()}


def session_options(
    repo_root: Path,
    role: str,
    model: str | None,
    ledger_url: str,
    *,
    effort: str | None = None,
    prompt_cache_ttl: str | None = None,
) -> list[str]:
    agent_file = read_agent_file(repo_root, role)
    extra = optional_servers(repo_root)
    servers = {
        LEDGER_SERVER: {"type": "http", "url": ledger_url},
        **mcp_servers(agent_file),
        **extra,
    }
    shared = shared_servers(repo_root)
    servers.update({name: entry for name, entry in shared.items() if name in servers})
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
