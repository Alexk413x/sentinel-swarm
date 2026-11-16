from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify
from .clock import parse_stamp, stamp
from .identity import LedgerError
from .paths import claude_json_path

CONFIG_CHECK = "config_parses"
USAGE_CHECK = "usage_limit"
USAGE_WAKE_REASON = "usage_limit_reset"
RESUMED_WAKE_REASON = "run_resumed"
RESUME_HINT = "Say continue or run /sentinel-swarm:resume to continue it."
# The limit can lift a little after the time the message names.
_RESET_GRACE = timedelta(minutes=2)
_RESET = re.compile(
    r"resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<half>am|pm)",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}


@dataclass(frozen=True)
class ClearCheck:
    name: str
    label: str
    passes: Callable[[Path, str, datetime], bool]


def _config_parses(repo_root: Path, arg: str, now: datetime) -> bool:
    del repo_root, arg, now
    try:
        return isinstance(json.loads(claude_json_path().read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


def _usage_reset_passed(repo_root: Path, arg: str, now: datetime) -> bool:
    del repo_root
    reset = parse_stamp(arg)
    return reset is not None and now >= reset + _RESET_GRACE


CHECKS: dict[str, ClearCheck] = {
    check.name: check
    for check in (
        ClearCheck(CONFIG_CHECK, "the config file ~/.claude.json parses", _config_parses),
        ClearCheck(USAGE_CHECK, "the account's usage limit resets", _usage_reset_passed),
    )
}


def split_check(value: str | None) -> tuple[ClearCheck | None, str]:
    name, _, arg = (value or "").partition("@")
    return CHECKS.get(name), arg


def usage_reset(text: str, now: datetime) -> datetime | None:
    match = _RESET.search(text)
    if match is None:
        return None
    hour = int(match["hour"]) % 12 + (12 if match["half"].lower() == "pm" else 0)
    minute = int(match["min"] or 0)
    local = now.astimezone()
    month = _MONTHS.get(match["mon"].title()) if match["mon"] else None
    if match["mon"] and month is None:
        return None
    try:
        if month is not None:
            reset = local.replace(
                month=month, day=int(match["day"]), hour=hour, minute=minute, second=0
            )
            if reset < local - timedelta(days=1):
                reset = reset.replace(year=reset.year + 1)
        else:
            reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if reset <= local:
                reset += timedelta(days=1)
    except ValueError:
        return None
    return reset.replace(microsecond=0).astimezone(timezone.utc)


def pause_message(reason: str, check: str | None) -> str:
    found, arg = split_check(check)
    clears = ""
    if found is not None:
        at = parse_stamp(arg) if found.name == USAGE_CHECK else None
        when = f", at {at.astimezone().strftime('%b %d %H:%M')}" if at is not None else ""
        clears = f" It resumes by itself once {found.label}{when}."
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
    found, _ = split_check(run["pause_check"])
    label = found.label if found is not None else "its clear-check passed"
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
    _owe_resume_wakeup(conn, run["run_id"], agent_id, now)
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


def owe_oracle_wakeup(
    conn: sqlite3.Connection, run_id: int, oracle_id: str, reason: str, pointer: str, now: datetime
) -> bool:
    """Owes the live Oracle a wake-up from itself; its own mod delivers it from the poll."""
    oracle = conn.execute(
        "SELECT name, session_name FROM agents WHERE agent_id = ? AND ended_at IS NULL",
        (oracle_id,),
    ).fetchone()
    if oracle is None:
        return False
    pending = conn.execute(
        "SELECT 1 FROM wakeups WHERE from_agent_id = ? AND to_agent_id = ? AND reason = ? "
        "AND sent_at IS NULL",
        (oracle_id, oracle_id, reason),
    ).fetchone()
    if pending is not None:
        return False
    conn.execute(
        "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
        "reason, pointer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            oracle_id,
            oracle_id,
            oracle["name"],
            oracle["session_name"] or oracle["name"],
            reason,
            pointer,
            stamp(now),
        ),
    )
    return True


def _owe_resume_wakeup(
    conn: sqlite3.Connection, run_id: int, oracle_id: str, now: datetime
) -> None:
    # Imported here: watchdog imports this module.
    from .watchdog import ARM_WATCHDOG

    owe_oracle_wakeup(
        conn, run_id, oracle_id, RESUMED_WAKE_REASON, f"The run resumed. {ARM_WATCHDOG}", now
    )


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
    _owe_resume_wakeup(conn, run["run_id"], agent_id, now)
    return note
