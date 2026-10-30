from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import config_dir

PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
BYPASS = "bypassPermissions"
FLOOR = "acceptEdits"
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


def inherited_mode(repo_root: Path) -> tuple[str, str]:
    for label, path in _sources(repo_root):
        mode = _permissions(path).get("defaultMode")
        if isinstance(mode, str) and mode in PERMISSION_MODES:
            return mode, f"inherited from {label}"
    return "default", "inherited: no settings file sets permissions.defaultMode"


def bypass_disabled(repo_root: Path) -> bool:
    return any(
        _permissions(path).get("disableBypassPermissionsMode") == "disable"
        for _, path in _sources(repo_root)
    )


def start_mode(repo_root: Path, role: str, configured: str | None) -> StartMode:
    if configured in PERMISSION_MODES:
        mode, flag, source = configured, configured, f"permission_mode.{role} in {SETTINGS_FILE}"
    else:
        mode, source = inherited_mode(repo_root)
        flag = None
        if role == "coder" and mode == "default":
            mode, flag, source = FLOOR, FLOOR, f"the Coder's floor over {mode} ({source})"
    if mode == BYPASS and bypass_disabled(repo_root):
        mode, flag = FLOOR, FLOOR
        source = f"{source}; policy sets permissions.disableBypassPermissionsMode"
    return StartMode(mode=mode, flag=flag, source=source)
