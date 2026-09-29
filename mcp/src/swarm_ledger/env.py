from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .db import ledger_path
from .ledger import Ledger

if TYPE_CHECKING:
    from .wake import EventHub

REPO_ROOT_VAR = "CLAUDE_PROJECT_DIR"
DB_PATH_VAR = "SENTINEL_SWARM_LEDGER_DB"


def repo_root() -> Path:
    raw = os.environ.get(REPO_ROOT_VAR)
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def db_path_for(root: Path) -> Path:
    raw = os.environ.get(DB_PATH_VAR)
    return Path(raw).resolve() if raw else ledger_path(root)


def open_ledger(root: Path | None = None, hub: EventHub | None = None) -> Ledger:
    root = root or repo_root()
    return Ledger(root, db_path=db_path_for(root), hub=hub)
