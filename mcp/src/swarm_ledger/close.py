from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import env
from .identity import LedgerError


def _file_reason(raw: str) -> dict:
    file_id, sep, reason = raw.partition("=")
    if not sep or not file_id.strip().isdigit():
        raise argparse.ArgumentTypeError(f"--file takes ID=REASON, not {raw!r}")
    return {"file_id": int(file_id), "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledger.py close",
        description=(
            "Close the live run that the ledger cannot finish: record the outcome and each "
            "unsettled file's reason, release every agent, and stop their sessions."
        ),
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        type=_file_reason,
        default=[],
        metavar="ID=REASON",
        help="an unsettled file and why it stops short; repeat for each",
    )
    parser.add_argument("outcome", help="what the run delivered")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    try:
        row = env.open_ledger(root).close_run(args.outcome, args.files, closed_by="launcher")
    except LedgerError as exc:
        sys.stderr.write(f"cannot close the run: {exc}\n")
        return 1
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
