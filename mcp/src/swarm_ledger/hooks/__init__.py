from __future__ import annotations

import json
from pathlib import Path

from .. import env
from . import events

HANDLERS = {
    "session_start": events.handle_session_start,
    "pre_agent": events.handle_pre_agent,
    "pre_write": events.handle_pre_write,
    "pre_shell": events.handle_pre_shell,
    "pre_monitor": events.handle_pre_monitor,
    "pre_send_message": events.handle_pre_send_message,
    "pre_skill": events.handle_pre_skill,
    "pre_ledger": events.handle_pre_ledger,
    "post_any": events.handle_post_any,
    "post_activity": events.handle_post_activity,
    "post_shell": events.handle_post_shell,
    "pre_compact": events.handle_pre_compact,
    "stop": events.handle_stop,
    "session_end": events.handle_session_end,
}


def run_event(event_name: str, raw: str, root: Path | None = None) -> tuple[str, str]:
    handler = HANDLERS.get(event_name)
    if handler is None:
        return "", f"swarm_ledger.hooks: unknown event {event_name!r}\n"

    # A hook must never lock the user out: any failure here is swallowed and
    # allows silently, with only a stderr line for a developer to find.
    try:
        data = json.loads(raw) if raw.strip() else {}
        ledger = env.open_ledger(root)
        try:
            result = handler(ledger, data)
        finally:
            ledger.conn.close()
    except Exception as exc:
        return "", f"swarm_ledger.hooks {event_name}: {exc}\n"

    return (json.dumps(result) if result is not None else ""), ""
