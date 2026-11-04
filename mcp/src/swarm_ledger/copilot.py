from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from . import panel
from .db import ledger_path

UNSUPPORTED = (
    "Native Copilot workers are unsupported. Sentinel Swarm's worker sessions and "
    "enforcement mod require Claude Code. Installing skills or starting the Python "
    "ledger does not install those gates. Use status to inspect a Claude run. "
    "A Claude-worker bridge requires an explicit user choice; it is not native "
    "Copilot support. No setup, server, or worker was started."
)


def status(repo: Path, run_id: int | None = None) -> dict:
    repo = repo.resolve()
    base = {"repo": str(repo), "worker_runtime": "claude"}
    try:
        path = ledger_path(repo)
        if not path.is_file():
            return {**base, "runs": [], "error": "no ledger"}
        # Unlike the Claude panel, this entry point never retries with a writable open.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return {**base, **panel.snapshot(conn, panel.live_sessions(), run_id)}
    except (OSError, sqlite3.Error) as exc:
        return {**base, "runs": [], "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py copilot")
    parser.add_argument(
        "action",
        choices=("check", "status", "setup", "run", "resume"),
        help="read Claude run status or report unsupported native workers",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--run", type=int, default=None, help="with status: select a run")
    args = parser.parse_args(argv)
    if args.run is not None and args.action != "status":
        parser.error("--run works only with status")
    if args.action == "status":
        doc = status(args.repo or Path.cwd(), args.run)
    else:
        doc = {
            "native_workers": False,
            "read_only_status": True,
            "worker_runtime": "claude",
            "error": UNSUPPORTED,
        }
    sys.stdout.write(json.dumps(doc) + "\n")
    return 1 if "error" in doc else 0


if __name__ == "__main__":
    sys.exit(main())
