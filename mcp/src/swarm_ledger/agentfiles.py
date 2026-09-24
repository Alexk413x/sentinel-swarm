from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .identity import ROLES, LedgerError

LEDGER_SERVER = "swarm-ledger"
SESSION_SETTINGS = '{"worktree":{"bgIsolation":"none"}}'
_SETUP_HINT = "run /sentinel-swarm:setup"


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


def session_options(repo_root: Path, role: str, model: str | None, ledger_url: str) -> list[str]:
    agent_file = read_agent_file(repo_root, role)
    config = {
        "mcpServers": {
            LEDGER_SERVER: {"type": "http", "url": ledger_url},
            **mcp_servers(agent_file),
        }
    }
    options = ["--agent", f"swarm-{role}"]
    if model:
        options += ["--model", model]
    permission_mode = agent_file.get("permissionMode")
    if permission_mode:
        options += ["--permission-mode", str(permission_mode)]
    options += ["--strict-mcp-config", "--mcp-config", json.dumps(config)]
    tools = tool_list(agent_file)
    if tools:
        options += ["--allowedTools", ",".join(tools)]
    options += ["--settings", SESSION_SETTINGS]
    return options
