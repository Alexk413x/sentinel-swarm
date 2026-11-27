from __future__ import annotations

import sys

from .. import env
from . import restart, run_event


def main() -> None:
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    stdout, stderr = run_event(event_name, raw, fallback=True)
    if stderr:
        sys.stderr.write(stderr)
    if stdout:
        sys.stdout.write(stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    # After the answer is out: the hook's answer never depends on the restart.
    try:
        restart.restart_server(env.repo_root(), event_name, raw)
    except Exception as exc:
        sys.stderr.write(f"swarm_ledger.hooks {event_name}: server restart failed: {exc}\n")


if __name__ == "__main__":
    main()
