from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import env
from .identity import LedgerError

SOURCES = ("skill", "outside_session", "user_chat")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m swarm_ledger.directive",
        description="Send the live run's Oracle a directive through the ledger.",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--source", choices=SOURCES, default="skill")
    parser.add_argument("--sender", default=None, help="the sender's name")
    parser.add_argument("--reply-to", type=int, default=None, help="the directive this answers")
    parser.add_argument("body", help="the directive text")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    try:
        row = env.open_ledger(root).directive_submit(
            source=args.source, sender_name=args.sender, body=args.body, reply_to=args.reply_to
        )
    except LedgerError as exc:
        sys.stderr.write(f"cannot submit the directive: {exc}\n")
        return 1
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
