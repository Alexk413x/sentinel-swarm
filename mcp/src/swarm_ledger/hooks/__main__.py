from __future__ import annotations

import sys

from . import run_event


def main() -> None:
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    stdout, stderr = run_event(event_name, sys.stdin.read())
    if stderr:
        sys.stderr.write(stderr)
    if stdout:
        sys.stdout.write(stdout)


if __name__ == "__main__":
    main()
