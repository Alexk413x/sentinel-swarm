from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .db import ledger_path

if TYPE_CHECKING:
    from .ledger import Ledger

REPO_ROOT_VAR = "CLAUDE_PROJECT_DIR"
DB_PATH_VAR = "SENTINEL_SWARM_LEDGER_DB"


def repo_root() -> Path:
    raw = os.environ.get(REPO_ROOT_VAR)
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def db_path_for(root: Path) -> Path:
    raw = os.environ.get(DB_PATH_VAR)
    return Path(raw).resolve() if raw else ledger_path(root)


def utf8_stdout() -> None:
    # The launcher runs Python with -I -S, so a piped stdout takes the locale's encoding: the
    # ANSI code page on Windows. Text outside it would raise UnicodeEncodeError mid-print.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")


def open_ledger(root: Path | None = None) -> Ledger:
    from .ledger import Ledger

    root = root or repo_root()
    return Ledger(root, db_path=db_path_for(root))
