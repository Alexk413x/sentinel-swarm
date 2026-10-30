from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import notify
from .clock import stamp
from .paths import config_override

RESUME_HINT = "Say continue or run /sentinel-swarm:resume to continue it."


@dataclass(frozen=True)
class ClearCheck:
    name: str
    label: str
    passes: Callable[[Path], bool]


def claude_json_path() -> Path:
    return (config_override() or Path.home()) / ".claude.json"


def _config_parses(repo_root: Path) -> bool:
    del repo_root
    try:
        return isinstance(json.loads(claude_json_path().read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


CHECKS: dict[str, ClearCheck] = {
    check.name: check
    for check in (
        ClearCheck("config_parses", "the config file ~/.claude.json parses", _config_parses),
    )
}


def pause_message(reason: str, check: str | None) -> str:
    clears = f" It resumes by itself once {CHECKS[check].label}." if check in CHECKS else ""
    return f"Run paused: {reason.strip().rstrip('.')}.{clears} {RESUME_HINT}"


def record_pause(
    conn: sqlite3.Connection,
    run_id: int,
    agent_id: str,
    reason: str,
    check: str | None,
    channels: Sequence[str],
    now: datetime,
) -> dict | None:
    """Pauses an active run in the caller's write transaction; returns the notification to show."""
    at = stamp(now)
    cur = conn.execute(
        "UPDATE runs SET state = 'paused', pause_check = ?, paused_at = ?, reminded_at = ? "
        "WHERE run_id = ? AND state = 'active'",
        (check, at, at, run_id),
    )
    if not cur.rowcount:
        return None
    event = conn.execute(
        "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
        "VALUES (?, 'active', 'paused', ?, ?)",
        (agent_id, reason, at),
    )
    return notify.record(
        conn,
        run_id,
        kind="warning",
        event_key=f"pause:{event.lastrowid}",
        message=pause_message(reason, check),
        channels=channels,
    )


def clear_resume(conn: sqlite3.Connection, run: sqlite3.Row, agent_id: str, now: datetime) -> str:
    """Resumes a paused run whose clear-check passed, in the caller's write transaction."""
    label = CHECKS[run["pause_check"]].label
    at = stamp(now)
    conn.execute(
        "UPDATE runs SET state = 'active', pause_check = NULL, paused_at = NULL "
        "WHERE run_id = ? AND state = 'paused'",
        (run["run_id"],),
    )
    note = f"the watchdog resumed the run: {label}"
    conn.execute(
        "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
        "VALUES (?, 'paused', 'active', ?, ?)",
        (agent_id, note, at),
    )
    conn.execute(
        "INSERT INTO directives (run_id, source, sender_name, body, created_at) "
        "VALUES (?, 'watchdog', 'watchdog', ?, ?)",
        (
            run["run_id"],
            f"The watchdog resumed the run: {label}, which cleared the pause. Arm the watchdog "
            "Monitor again and continue the plan. Then resolve this directive with "
            "directive_resolve.",
            at,
        ),
    )
    return note
