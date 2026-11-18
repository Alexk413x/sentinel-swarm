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
DEFAULT_MODE = "default"
DEFAULT_SOURCE = "Claude Code's default: no settings file sets permissions.defaultMode"
SETTINGS_FILE = "sentinel-swarm.local.md"


@dataclass(frozen=True)
class StartMode:
    mode: str
    flag: str | None
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


def inherited_mode(repo_root: Path) -> tuple[str, str] | None:
    for label, path in _sources(repo_root):
        mode = _permissions(path).get("defaultMode")
        if isinstance(mode, str) and mode in PERMISSION_MODES:
            return mode, f"permissions.defaultMode in {label}"
    return None


def start_mode(repo_root: Path, role: str, configured: str | None) -> StartMode:
    """The mode a role's session starts in. Only `permission_mode.<role>` becomes a
    `--permission-mode` flag; otherwise Claude Code applies the user's own settings, and the
    mode here only reports which one."""
    if configured in PERMISSION_MODES:
        mode = str(configured)
        return StartMode(mode, mode, f"permission_mode.{role} in {SETTINGS_FILE}")
    inherited = inherited_mode(repo_root)
    if inherited is not None:
        return StartMode(inherited[0], None, inherited[1])
    return StartMode(DEFAULT_MODE, None, DEFAULT_SOURCE)
