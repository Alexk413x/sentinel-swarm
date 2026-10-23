from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def config_override() -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else None


def config_dir() -> Path:
    return config_override() or Path.home() / ".claude"


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
