from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import notify
from .clock import stamp
from .identity import LedgerError
from .paths import claude_json_path

CONFIG_CHECK = "config_parses"
RESUME_HINT = "Say continue or run /sentinel-swarm:resume to continue it."


@dataclass(frozen=True)
class ClearCheck:
    name: str
    label: str
    passes: Callable[[Path], bool]


def _config_parses(repo_root: Path) -> bool:
    del repo_root
    try:
        return isinstance(json.loads(claude_json_path().read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


CHECKS: dict[str, ClearCheck] = {
    check.name: check
    for check in (
        ClearCheck(CONFIG_CHECK, "the config file ~/.claude.json parses", _config_parses),
    )
}


def pause_message(reason: str, check: str | None) -> str:
    clears = f" It resumes by itself once {CHECKS[check].label}." if check in CHECKS else ""
    return f"Run paused: {reason.strip().rstrip('.')}.{clears} {RESUME_HINT}"


def user_command_oracle(conn: sqlite3.Connection, run_id: int) -> str:
    """The agent id a pause or resume from outside the Oracle's session records its event on."""
    row = conn.execute(
        "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' "
        "ORDER BY (ended_at IS NULL) DESC, started_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        raise LedgerError("the run has no Oracle")
    return str(row["agent_id"])


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


def user_pause(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    agent_id: str,
    reason: str,
    who: str,
    source: str,
    channels: Sequence[str],
    now: datetime,
) -> dict | None:
    """Pauses an active run for a user outside the Oracle, in the caller's write transaction.

    Records the pause as `run_pause` does and files a directive that tells the Oracle who paused.
    """
    reason = reason.strip()
    notice = record_pause(
        conn, run["run_id"], agent_id, f"{reason} (paused by {who})", None, channels, now
    )
    conn.execute(
        "INSERT INTO directives (run_id, source, sender_name, body, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run["run_id"],
            source,
            who,
            f"{who} paused the run from outside your session: {reason}. Finish the step in "
            "hand and stop; the run stays paused until the user resumes it. Resolve this "
            "directive with directive_resolve after the run resumes.",
            stamp(now),
        ),
    )
    return notice


def user_resume(
    conn: sqlite3.Connection, run: sqlite3.Row, agent_id: str, who: str, now: datetime
) -> str:
    """Resumes a paused run for a user outside the Oracle, in the caller's write transaction."""
    at = stamp(now)
    conn.execute(
        "UPDATE runs SET state = 'active', pause_check = NULL, paused_at = NULL "
        "WHERE run_id = ? AND state = 'paused'",
        (run["run_id"],),
    )
    note = f"{who} resumed the run"
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
            f"{who} resumed the run from outside your session, which cleared the pause. Arm "
            "the watchdog Monitor again and continue the plan. Then resolve this directive "
            "and any open pause directive with directive_resolve.",
            at,
        ),
    )
    return note
