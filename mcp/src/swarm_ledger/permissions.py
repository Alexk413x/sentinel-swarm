from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import config_dir

PERMISSION_MODES = (
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "dontAsk",
    "bypassPermissions",
    "manual",
)
BYPASS = "bypassPermissions"
# Every role starts in this mode unless permission_mode.<role> names another: a background
# session that prompts stalls until a person answers, and the mod's gates hold in every mode.
DEFAULT_MODE = BYPASS
DEFAULT_SOURCE = "the swarm's default for every role"
FALLBACK = "acceptEdits"
SETTINGS_FILE = "sentinel-swarm.local.md"


@dataclass(frozen=True)
class StartMode:
    mode: str
    flag: str
    source: str


def managed_settings_path(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "win32":
        return Path("C:/Program Files/ClaudeCode/managed-settings.json")
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _permissions(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = data.get("permissions") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def _sources(repo_root: Path) -> list[tuple[str, Path]]:
    """Claude Code's settings files, highest precedence first. `--settings` is not read."""
    return [
        ("managed settings", managed_settings_path()),
        (".claude/settings.local.json", repo_root / ".claude" / "settings.local.json"),
        (".claude/settings.json", repo_root / ".claude" / "settings.json"),
        ("the user's settings.json", config_dir() / "settings.json"),
    ]


def bypass_disabled(repo_root: Path) -> bool:
    return any(
        _permissions(path).get("disableBypassPermissionsMode") == "disable"
        for _, path in _sources(repo_root)
    )


def start_mode(repo_root: Path, role: str, configured: str | None) -> StartMode:
    """The mode a role's session starts in. The launch always passes it as `--permission-mode`,
    so the user's own `permissions.defaultMode` never decides a role session's mode."""
    if configured in PERMISSION_MODES:
        mode, source = str(configured), f"permission_mode.{role} in {SETTINGS_FILE}"
    else:
        mode, source = DEFAULT_MODE, DEFAULT_SOURCE
    if mode == BYPASS and bypass_disabled(repo_root):
        mode = FALLBACK
        source = f"{source}; policy sets permissions.disableBypassPermissionsMode"
    return StartMode(mode=mode, flag=mode, source=source)
