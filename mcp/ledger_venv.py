# Standard library only: skill commands run it to find the venv before it exists.
# templates/hook_shim.py mirrors ledger_venv, since the shim cannot import this file.

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

DATA_FOLDER = "sentinel-swarm-sentinel-swarm"


def config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else Path.home() / ".claude"


def ledger_venv(project: Path) -> Path:
    digest = hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest()[:12]
    return config_dir() / "plugins" / "data" / DATA_FOLDER / f"venv-{digest}"


if __name__ == "__main__":
    sys.stdout.write(f"{ledger_venv(Path(__file__).resolve().parent)}\n")
