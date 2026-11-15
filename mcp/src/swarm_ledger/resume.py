from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import env, pauses, serve
from .clock import utcnow
from .db import row_dict, write_tx
from .identity import LedgerError
from .queries import live_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledger.py resume",
        description="Resume a paused run from outside the Oracle's session.",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--sender", default="the user", help="who resumes the run")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    ledger = env.open_ledger(root)
    try:
        with write_tx(ledger.conn) as conn:
            run = live_run(conn)
            if run is None:
                raise LedgerError("no active run")
            if run["state"] != "paused":
                raise LedgerError("the run is not paused")
            pauses.user_resume(
                conn, run, pauses.user_command_oracle(conn, run["run_id"]), args.sender, utcnow()
            )
    except LedgerError as exc:
        sys.stderr.write(f"cannot resume the run: {exc}\n")
        return 1
    # The watchdog that wakes the Oracle runs in the ledger server, which exits while paused.
    try:
        serve.ensure_server(root)
    except LedgerError as exc:
        sys.stderr.write(
            f"the run is active, but the ledger server did not start: {exc}\n"
            "Run ledger.py serve --detach, then type continue in the Oracle's row.\n"
        )
        return 1
    print(json.dumps(row_dict(ledger.conn, "runs", "run_id", run["run_id"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
