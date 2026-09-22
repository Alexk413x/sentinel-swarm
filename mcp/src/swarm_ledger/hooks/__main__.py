from __future__ import annotations

import json
import sys

from .. import env
from . import events

_HANDLERS = {
    "session_start": events.handle_session_start,
    "pre_agent": events.handle_pre_agent,
    "subagent_start": events.handle_subagent_start,
    "pre_write": events.handle_pre_write,
    "pre_shell": events.handle_pre_shell,
    "pre_ledger": events.handle_pre_ledger,
    "post_any": events.handle_post_any,
    "post_shell": events.handle_post_shell,
    "pre_compact": events.handle_pre_compact,
    "subagent_stop": events.handle_subagent_stop,
    "stop": events.handle_stop,
    "session_end": events.handle_session_end,
}


def main() -> None:
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    handler = _HANDLERS.get(event_name)
    if handler is None:
        sys.stderr.write(f"swarm_ledger.hooks: unknown event {event_name!r}\n")
        return

    # A hook must never lock the user out: any failure here is swallowed and
    # allows silently, with only a stderr line for a developer to find.
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        ledger = env.open_ledger()
        result = handler(ledger, data)
    except Exception as exc:
        sys.stderr.write(f"swarm_ledger.hooks {event_name}: {exc}\n")
        return

    if result is not None:
        sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
