from __future__ import annotations

import os
import shutil
from contextlib import suppress
from pathlib import Path


def config_override() -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else None


def config_dir() -> Path:
    return config_override() or Path.home() / ".claude"


def claude_json_path() -> Path:
    return (config_override() or Path.home()) / ".claude.json"


def same_path(raw: object, root: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        return os.path.normcase(str(Path(raw).resolve())) == os.path.normcase(str(root.resolve()))
    except OSError:
        return False


def unlink_quiet(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def python_command() -> str:
    # The command an MCP entry that runs the shim starts. On Windows, python3 on PATH is often
    # the Microsoft Store stub, which only prints a hint.
    if os.name == "nt":
        return "python"
    return "python3" if shutil.which("python3") else "python"
