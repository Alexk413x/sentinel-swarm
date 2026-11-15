from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import env, notify, pauses
from .clock import utcnow
from .db import row_dict, write_tx
from .identity import LedgerError
from .queries import live_run

SOURCES = ("outside_session", "user_chat", "skill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledger.py pause",
        description="Pause the live run from outside the Oracle's session.",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--reason", required=True, help="why the run pauses")
    parser.add_argument("--sender", default="the user", help="who pauses the run")
    parser.add_argument("--source", choices=SOURCES, default="outside_session")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    ledger = env.open_ledger(root)
    try:
        if not args.reason.strip():
            raise LedgerError("a pause needs a reason the user can act on")
        with write_tx(ledger.conn) as conn:
            run = live_run(conn)
            if run is None:
                raise LedgerError("no active run")
            if run["state"] == "paused":
                raise LedgerError("the run is already paused")
            notice = pauses.user_pause(
                conn,
                run,
                pauses.user_command_oracle(conn, run["run_id"]),
                args.reason,
                args.sender,
                args.source,
                ledger.settings.channels_for("run_paused"),
                utcnow(),
            )
    except LedgerError as exc:
        sys.stderr.write(f"cannot pause the run: {exc}\n")
        return 1
    notify.deliver(notice)
    paused = row_dict(ledger.conn, "runs", "run_id", run["run_id"])
    print(json.dumps(paused | {"reason": args.reason.strip()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
