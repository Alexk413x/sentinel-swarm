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
        prog="ledger.py directive",
        description="Send the live run's Oracle a directive through the ledger.",
    )
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    parser.add_argument("--source", choices=SOURCES, default="skill")
    parser.add_argument("--sender", default=None, help="the sender's name")
    parser.add_argument("--reply-to", type=int, default=None, help="the directive this answers")
    parser.add_argument(
        "--status",
        action="store_true",
        help="print the live run's recent user directives with their state instead of sending one",
    )
    parser.add_argument("body", nargs="?", help="the directive text")
    args = parser.parse_args(argv)
    if not args.status and args.body is None:
        parser.error("the directive text (body) is required unless --status is given")
    root = (args.repo or env.repo_root()).resolve()
    try:
        ledger = env.open_ledger(root)
        if args.status:
            print(json.dumps({"directives": ledger.directive_status()}))
            return 0
        row = ledger.directive_submit(
            source=args.source, sender_name=args.sender, body=args.body, reply_to=args.reply_to
        )
    except LedgerError as exc:
        action = "read the directives" if args.status else "submit the directive"
        sys.stderr.write(f"cannot {action}: {exc}\n")
        return 1
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
