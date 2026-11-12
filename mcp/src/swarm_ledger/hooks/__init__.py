from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import env
from . import events

HANDLERS = {
    "session_start": events.handle_session_start,
    "pre_agent": events.handle_pre_agent,
    "pre_ask": events.handle_pre_ask,
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
    "owed": events.handle_owed,
    "wake_sent": events.handle_wake_sent,
    "self_owed": events.handle_self_owed,
    "inbox_take": events.handle_inbox_take,
    "inbox_ack": events.handle_inbox_ack,
    "inbox_release": events.handle_inbox_release,
    "send_target": events.handle_send_target,
    "permission_request": events.handle_permission_request,
    "permission_wait": events.handle_permission_wait,
    "permission_close": events.handle_permission_close,
}
# A resumed session's first hooks: only these may afford a claude agents --json call to match
# an unknown session id to the agent being resumed.
LISTING_EVENTS = frozenset({"session_start", "pre_ledger"})
# A session's last hooks can arrive after the daemon retired it, so they do not unpark it.
CLOSING_EVENTS = frozenset({"stop", "session_end"})


# The events that gate a tool call. A crash in one of these denies instead of allowing, so a
# broken ledger cannot let a call through unchecked. templates/hook_shim.py is standalone and
# keeps its own copy; a test holds the two equal.
GATING_EVENTS = frozenset(
    {
        "pre_agent",
        "pre_write",
        "pre_shell",
        "pre_monitor",
        "pre_send_message",
        "pre_skill",
        "pre_ledger",
    }
)
_LOCKED = ("database is locked", "database table is locked", "database is busy")


def _is_locked(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        phrase in str(exc).lower() for phrase in _LOCKED
    )


def _attempt(event_name: str, handler, raw: str, root: Path | None) -> dict | None:
    data = json.loads(raw) if raw.strip() else {}
    ledger = env.open_ledger(root)
    try:
        if isinstance(data, dict) and not data.get("agent_id"):
            ledger.adopt_session(
                data.get("session_id"),
                use_listing=event_name in LISTING_EVENTS,
                unpark=event_name not in CLOSING_EVENTS,
            )
        return handler(ledger, data)
    finally:
        ledger.conn.close()


def _cannot_check(exc: Exception) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"sentinel-swarm cannot check this call: {exc}; run /sentinel-swarm:setup"
            ),
        }
    }


def run_event(event_name: str, raw: str, root: Path | None = None) -> tuple[str, str]:
    handler = HANDLERS.get(event_name)
    if handler is None:
        return "", f"swarm_ledger.hooks: unknown event {event_name!r}\n"

    # A gating event that crashes fails closed: a locked database gets one more attempt on a
    # fresh ledger, and any other failure (or a second lock) denies the call. Any other event
    # swallows the failure and answers empty, which allows, with only a stderr line.
    result: dict | None
    try:
        try:
            result = _attempt(event_name, handler, raw, root)
        except Exception as exc:
            if event_name not in GATING_EVENTS or not _is_locked(exc):
                raise
            result = _attempt(event_name, handler, raw, root)
    except Exception as exc:
        answer = json.dumps(_cannot_check(exc)) if event_name in GATING_EVENTS else ""
        return answer, f"swarm_ledger.hooks {event_name}: {exc}\n"

    return (json.dumps(result) if result is not None else ""), ""
