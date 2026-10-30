from __future__ import annotations

import json
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import notify, pauses, rekey, sessions, testrun, wake
from .clock import parse_stamp, stamp, utcnow
from .db import NOW, write_tx
from .identity import ROLES, LedgerError
from .queries import latest_live_oracle, live_run, submitted_handoffs, unread_count
from .settings import NOTIFY_CHANNELS, WatchdogSettings
from .waits import landed_waits

if TYPE_CHECKING:
    from .ledger import Ledger

WATCH_COMMAND = "python3 .sentinel-swarm/hook.py watch || python .sentinel-swarm/hook.py watch"
MONITOR_TIMEOUT_MS = 1_800_000
MONITOR_CALL = (
    f"Monitor(command={json.dumps(WATCH_COMMAND)}, "
    f'description="sentinel-swarm watchdog", timeout_ms={MONITOR_TIMEOUT_MS})'
)
REGISTER_GRACE = timedelta(minutes=2)
WAKE_EVERY = timedelta(minutes=5)
MAX_WAKES = 3
WAKE_STATE = "watchdog_wake"
PAUSE_REASON = "the watchdog could not wake the Oracle"
_MEMBER_ROLES = ("manager", "lead", "coder")
_WINDOW_1M = 1_000_000
IDLE_STALL = timedelta(minutes=2)
DRIVER_UNSENT_AFTER = timedelta(minutes=2)
_WINDOW_HAIKU = 200_000
_TAIL_BLOCK = 256 * 1024
DRIVER_CHECKIN_INTERVAL = timedelta(minutes=30)
DRIVER_CHECKIN_GRACE = timedelta(minutes=5)
DRIVER_ERRORS = {"crashed": ("crashed", "error"), "driver_overdue": ("overdue", "warning")}
USER_KINDS = ("needs_user", "run_stalled")
_USER_WAITS = ("permission", "input", "question", "approval")
_REQUEST_CHARS = 300


@dataclass(frozen=True)
class Finding:
    agent_id: str
    name: str
    session_name: str | None
    kind: str
    detail: str
    next_step: str


def _log(message: str) -> None:
    sys.stderr.write(f"{stamp(utcnow())} watchdog: {message}\n")
    sys.stderr.flush()


def scan(
    conn: sqlite3.Connection,
    listing: list[dict],
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    run = conn.execute("SELECT * FROM runs WHERE state = 'active'").fetchone()
    if run is None:
        return []
    agents = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL "
            f"AND role IN ({', '.join('?' for _ in ROLES)}) "
            "ORDER BY started_at, agent_id",
            (run["run_id"], *ROLES),
        )
    ]
    by_session = {str(e["sessionId"]): e for e in listing if e.get("sessionId")}
    names = {a["agent_id"]: a["name"] for a in agents}

    findings: list[Finding] = []
    for agent in agents:
        entry = by_session.get(agent["agent_id"])
        if agent["role"] in _MEMBER_ROLES:
            findings += _session_findings(agent, entry, now, settings)
            spin = _spinning(conn, agent, settings)
            if spin is not None:
                parent = names.get(agent["parent_agent_id"]) or "its parent"
                findings.append(
                    _finding(
                        agent,
                        "spinning",
                        spin,
                        f"Ask {parent} to review the work: return_work with new direction, "
                        "or issue_escalate.",
                    )
                )
        elif agent["role"] == "driver" and not waiting_on_user(entry):
            findings += _driver_findings(conn, agent, entry, now)
        context = _context_high(agent, settings)
        if context is not None:
            findings.append(_finding(agent, "context_high", context, _context_step(agent, names)))

    findings += _needs_user_findings(agents, by_session)
    findings += _landed_wait_findings(conn, run["run_id"], agents, by_session)
    findings += _unsent_findings(conn, run["run_id"], agents, by_session, now, settings)
    findings += _queued_findings(conn, run["run_id"], agents, now, settings)

    stalled = _stalled(conn, run, agents, by_session, now)
    if stalled is not None:
        findings.append(stalled)
    run_stalled = _run_stalled(conn, run, agents, by_session, now, settings)
    if run_stalled is not None:
        findings.append(run_stalled)
    return findings


def waiting_on_user(entry: dict | None) -> str | None:
    if entry is None:
        return None
    waiting = " ".join(str(entry.get("waitingFor") or "").split())
    if any(word in waiting.lower() for word in _USER_WAITS):
        return waiting
    return None


def _needs_user_findings(agents: list[dict], by_session: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for agent in agents:
        waiting = waiting_on_user(by_session.get(agent["agent_id"]))
        if waiting is None:
            continue
        session = agent["session_name"] or agent["name"]
        request = pending_request(agent.get("transcript_path"))
        detail = f"its session {session} waits on {waiting}"
        if request:
            detail = f"{detail}: {request}"
        findings.append(
            _finding(
                agent,
                "needs_user",
                detail,
                f"The user has been notified. Tell the user to open the session {session} in "
                "agent view and answer it, and keep the rest of the run moving.",
            )
        )
    return findings


def pending_request(raw: object) -> str | None:
    """The tool call a session waits on, from the end of its transcript, as one line."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        with Path(raw).open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - _TAIL_BLOCK)
            handle.seek(start)
            lines = handle.read().split(b"\n")
    except OSError:
        return None
    for line in reversed(lines[1:] if start > 0 else lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if uses:
            return _describe_request(uses[-1])
    return None


def _describe_request(use: dict) -> str:
    name = str(use.get("name") or "a tool")
    raw = use.get("input")
    tool_input: dict = raw if isinstance(raw, dict) else {}
    if name == "AskUserQuestion":
        asked = []
        for question in tool_input.get("questions") or []:
            if not isinstance(question, dict):
                continue
            labels = [
                str(o.get("label")) for o in question.get("options") or [] if isinstance(o, dict)
            ]
            text = str(question.get("question") or "").strip()
            asked.append(f"{text} (options: {', '.join(labels)})" if labels else text)
        return _clip("AskUserQuestion: " + " / ".join(asked))
    for key in ("file_path", "command", "path", "url"):
        if tool_input.get(key):
            return _clip(f"{name} {tool_input[key]}")
    return _clip(f"{name} {json.dumps(tool_input, ensure_ascii=False)}")


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _REQUEST_CHARS else flat[: _REQUEST_CHARS - 3] + "..."


def _run_stalled(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    agents: list[dict],
    by_session: dict[str, dict],
    now: datetime,
    settings: WatchdogSettings,
) -> Finding | None:
    oracle = next((a for a in agents if a["role"] == "oracle"), None)
    after = timedelta(minutes=settings.stuck_minutes)
    if oracle is None or after <= timedelta(0):
        return None
    run_id = run["run_id"]
    if conn.execute(
        "SELECT 1 FROM spawn_queue WHERE run_id = ? AND state = 'queued'", (run_id,)
    ).fetchone():
        return None
    for agent in agents:
        entry = by_session.get(agent["agent_id"])
        running = entry is not None and sessions.is_running(entry)
        if agent["state"] == "working" and running and not waiting_on_user(entry):
            return None
    if conn.execute(
        "SELECT 1 FROM wakeups w JOIN agents t ON t.agent_id = w.to_agent_id "
        "WHERE w.run_id = ? AND w.sent_at IS NULL AND t.ended_at IS NULL",
        (run_id,),
    ).fetchone():
        return None
    stamps = [_last_activity(conn, run_id), parse_stamp(run["started_at"])]
    stamps += [_transcript_written(a) for a in agents]
    last = max((s for s in stamps if s is not None), default=None)
    if last is None or now - last < after:
        return None
    waiting = [
        a["session_name"] or a["name"]
        for a in agents
        if waiting_on_user(by_session.get(a["agent_id"]))
    ]
    detail = (
        f"no agent of the run has worked and no wake-up has been owed for {_minutes(now - last)} "
        "minutes"
    )
    if waiting:
        detail = f"{detail}; waiting on the user: {', '.join(waiting)}"
    return _finding(
        oracle,
        "run_stalled",
        detail,
        "The user has been notified. Read status_tree(), then wake the agent whose work is "
        "pending, or ask the user with user_ask when only the user can move the run.",
    )


def _landed_wait_findings(
    conn: sqlite3.Connection, run_id: int, agents: list[dict], by_session: dict[str, dict]
) -> list[Finding]:
    by_id = {a["agent_id"]: a for a in agents}
    waits: dict[str, list[str]] = {}
    for wait in landed_waits(conn, run_id):
        waits.setdefault(wait["agent_id"], []).append(
            f"{wait['path']} ({wait['file_state']}; waiting for: {wait['reason']})"
        )
    return [
        _finding(
            by_id[agent_id],
            "wait_landed",
            f"it still waits on {', '.join(paths)}",
            f"Wake it: {_wake_call(by_id[agent_id], by_session)}.",
        )
        for agent_id, paths in waits.items()
        if agent_id in by_id
    ]


def _unsent_findings(
    conn: sqlite3.Connection,
    run_id: int,
    agents: list[dict],
    by_session: dict[str, dict],
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    after = timedelta(minutes=settings.unsent_wakeup_minutes)
    if after <= timedelta(0):
        return []
    by_id = {a["agent_id"]: a for a in agents}
    findings: list[Finding] = []
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT w.*, f.name AS from_name, f.role AS from_role FROM wakeups w "
        "JOIN agents f ON f.agent_id = w.from_agent_id "
        "WHERE w.run_id = ? AND w.sent_at IS NULL AND f.ended_at IS NULL "
        "AND w.to_agent_id != w.from_agent_id ORDER BY w.wakeup_id",
        (run_id,),
    ):
        target = by_id.get(row["to_agent_id"])
        owed = parse_stamp(row["created_at"])
        if target is None or row["to_agent_id"] in seen or row["from_role"] == "driver":
            continue
        if owed is None or now - owed < after:
            continue
        seen.add(row["to_agent_id"])
        pointer = " ".join(str(row["pointer"]).split())
        call = _wake_call(target, by_session)
        step = f"Wake it: {call}."
        if target.get("parked_at"):
            step = (
                f"The ledger resumes it itself once memory allows ({target['park_reason']}). "
                f"To wake it now: {call}."
            )
        if "SendMessage(" in call:
            step = f"{step} {wake.AMBIGUOUS_NAME_RULE}"
        findings.append(
            _finding(
                target,
                "wake_unsent",
                f"{row['from_name']} has owed it a wake-up for {_minutes(now - owed)} minutes: "
                f"{pointer}",
                step,
            )
        )
    return findings


def _queued_findings(
    conn: sqlite3.Connection,
    run_id: int,
    agents: list[dict],
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    after = timedelta(minutes=settings.queued_spawn_minutes)
    if after <= timedelta(0):
        return []
    by_id = {a["agent_id"]: a for a in agents}
    findings: list[Finding] = []
    for row in conn.execute(
        "SELECT * FROM spawn_queue WHERE run_id = ? AND state = 'queued' ORDER BY queue_id",
        (run_id,),
    ):
        parent = by_id.get(row["parent_agent_id"])
        queued = parse_stamp(row["queued_at"])
        if parent is None or queued is None or now - queued < after:
            continue
        findings.append(
            _finding(
                parent,
                "spawn_queued",
                f"its spawn of {row['child_name']} ({row['role']}) has waited "
                f"{_minutes(now - queued)} minutes for free memory",
                "Free memory on the machine, or lower memory.headroom_mb, or call run_pause "
                "and tell the user the run waits on memory.",
            )
        )
    return findings


def _finding(agent: dict, kind: str, detail: str, next_step: str) -> Finding:
    return Finding(
        agent_id=agent["agent_id"],
        name=agent["name"],
        session_name=agent["session_name"],
        kind=kind,
        detail=detail,
        next_step=next_step,
    )


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _not_running(entry: dict | None) -> str:
    if entry is None:
        return "is not in claude agents --json"
    return f"is not running (status {entry.get('status')}, state {entry.get('state')})"


def _crashed(agent: dict, entry: dict | None, now: datetime) -> list[Finding]:
    state = agent["state"]
    started = parse_stamp(agent["started_at"])
    overdue = started is not None and now - started > REGISTER_GRACE
    if state != "working" and not (state == "registered" and overdue):
        return []
    return [
        _finding(
            agent,
            "crashed",
            f"the ledger says {state}, but its session {_not_running(entry)}",
            f"Resume it with agent_resume(target_name={json.dumps(agent['name'])}).",
        )
    ]


def _ask_step(agent: dict, alternative: str) -> str:
    label = json.dumps(agent["session_name"] or agent["name"])
    return (
        f"Message it with SendMessage(to={label}) and ask what blocks it, or {alternative} "
        "agent_resume refuses a running session."
    )


def _release_step(agent_id: str) -> str:
    return (
        "Its result is in the ledger: read directive_inbox() and status_tree(). "
        f"Then release it with agent_release(target_agent_id={json.dumps(agent_id)})."
    )


def _session_findings(
    agent: dict, entry: dict | None, now: datetime, settings: WatchdogSettings
) -> list[Finding]:
    if waiting_on_user(entry):
        return []

    running = entry is not None and sessions.is_running(entry)
    if not running:
        return _crashed(agent, entry, now)

    if agent["state"] != "working":
        return []
    last = parse_stamp(agent["last_heartbeat_at"]) or parse_stamp(agent["started_at"])
    if last is None or now - last < timedelta(minutes=settings.stuck_minutes):
        return []
    activity = agent["current_activity"] or "none recorded"
    return [
        _finding(
            agent,
            "stuck",
            f"its session runs, but it has shown no activity for {_minutes(now - last)} minutes "
            f"(last activity: {activity})",
            _ask_step(
                agent,
                "have its parent replace it with a fresh agent that continues from the ledger.",
            ),
        )
    ]


def _driver_findings(
    conn: sqlite3.Connection, agent: dict, entry: dict | None, now: datetime
) -> list[Finding]:
    running = entry is not None and sessions.is_running(entry)
    if not running:
        closed = _closed_exploration(conn, agent["agent_id"])
        if closed is not None:
            return [
                _finding(
                    agent,
                    "crashed",
                    f"its exploration {closed['request_id']} ended ({closed['state']}), but its "
                    f"session {_not_running(entry)} and the ledger has not released it",
                    _release_step(agent["agent_id"]),
                )
            ]
        return _crashed(agent, entry, now)

    if agent["state"] != "working":
        return _driver_unsent(conn, agent, now)
    request = conn.execute(
        "SELECT * FROM drive_requests WHERE agent_id = ? AND state = 'open' "
        "ORDER BY request_id DESC LIMIT 1",
        (agent["agent_id"],),
    ).fetchone()
    if request is None:
        return []
    last = parse_stamp(request["last_checkin_at"]) or parse_stamp(request["opened_at"])
    if last is None or now - last < DRIVER_CHECKIN_INTERVAL + DRIVER_CHECKIN_GRACE:
        return []
    return [
        _finding(
            agent,
            "driver_overdue",
            f"exploration {request['request_id']} has had no check-in for "
            f"{_minutes(now - last)} minutes",
            _ask_step(agent, "stop it and start a fix for whatever blocked it."),
        )
    ]


def _closed_exploration(conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT request_id, state FROM drive_requests r WHERE agent_id = ? AND state != 'open' "
        "AND NOT EXISTS (SELECT 1 FROM drive_requests o WHERE o.agent_id = r.agent_id "
        "AND o.state = 'open') ORDER BY request_id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()


def _driver_unsent(conn: sqlite3.Connection, agent: dict, now: datetime) -> list[Finding]:
    closed = _closed_exploration(conn, agent["agent_id"])
    if closed is None:
        return []
    wakeup = conn.execute(
        "SELECT w.* FROM wakeups w JOIN agents t ON t.agent_id = w.to_agent_id "
        "WHERE w.from_agent_id = ? AND w.sent_at IS NULL AND t.ended_at IS NULL "
        "ORDER BY w.wakeup_id LIMIT 1",
        (agent["agent_id"],),
    ).fetchone()
    if wakeup is None:
        return []
    owed = parse_stamp(wakeup["created_at"])
    if owed is None or now - owed < DRIVER_UNSENT_AFTER:
        return []
    pointer = " ".join(str(wakeup["pointer"]).split())
    return [
        _finding(
            agent,
            "driver_unsent",
            f"its exploration {closed['request_id']} ended ({closed['state']}), its session "
            f"runs but sits idle, and it has owed {wakeup['to_name']} an unsent wake-up for "
            f"{_minutes(now - owed)} minutes: {pointer}",
            _release_step(agent["agent_id"]),
        )
    ]


def _failed(row: sqlite3.Row) -> bool:
    exit_code = row["exit_code"]
    return exit_code != 0 or (row["failed"] or 0) > 0


def _spinning(conn: sqlite3.Connection, agent: dict, settings: WatchdogSettings) -> str | None:
    limit = settings.spin_failures
    if limit <= 0:
        return None
    groups: dict[tuple[str, str | None], list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT test_run_id, scope, target, exit_code, failed FROM test_runs "
        "WHERE agent_id = ? AND state != 'running' ORDER BY test_run_id DESC",
        (agent["agent_id"],),
    ):
        latest = groups.setdefault((row["scope"], row["target"]), [])
        if len(latest) < limit:
            latest.append(row)
    spinning = [
        f"the last {limit} test runs for scope {scope} and target {target or 'none'} failed "
        f"(latest test_run_id {rows[0]['test_run_id']})"
        for (scope, target), rows in groups.items()
        if len(rows) == limit and all(_failed(r) for r in rows)
    ]
    return "; ".join(spinning) or None


def _last_usage(path: Path) -> tuple[int, str] | None:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        carry = b""
        while end > 0:
            start = max(0, end - _TAIL_BLOCK)
            handle.seek(start)
            chunk = handle.read(end - start) + carry
            lines = chunk.split(b"\n")
            carry = lines[0] if start > 0 else b""
            for raw in reversed(lines[1:] if start > 0 else lines):
                found = _usage_of(raw)
                if found is not None:
                    return found
            end = start
    return None


def _usage_of(raw: bytes) -> tuple[int, str] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens = sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return tokens, str(message.get("model") or "")


def _context_window(models: str) -> int:
    return _WINDOW_HAIKU if "haiku" in models.lower() else _WINDOW_1M


def _context_high(agent: dict, settings: WatchdogSettings) -> str | None:
    raw = agent["transcript_path"]
    if not raw:
        return None
    try:
        found = _last_usage(Path(raw))
    except OSError:
        return None
    if found is None:
        return None
    tokens, model = found
    window = settings.context_window or _context_window(f"{agent['model'] or ''} {model}")
    pct = tokens * 100 / window
    if pct < settings.context_pct:
        return None
    return f"its latest request used {tokens} of {window} context tokens ({pct:.0f}%)"


def _context_step(agent: dict, names: dict[str, str]) -> str:
    if agent["role"] == "oracle":
        return (
            "Record where the run stands in the ledger, then call run_pause so the user can "
            "continue the run in a fresh Oracle session with /sentinel-swarm:resume."
        )
    parent = names.get(agent["parent_agent_id"]) or "its parent"
    return (
        f"Have {parent} release it and brief a fresh agent that continues from the ledger records."
    )


def _last_activity(conn: sqlite3.Connection, run_id: int) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(at) AS at FROM ("
        "SELECT MAX(e.at) AS at FROM agent_events e JOIN agents a ON a.agent_id = e.agent_id "
        "WHERE a.run_id = ? "
        "UNION ALL SELECT MAX(created_at) FROM messages WHERE run_id = ? "
        "UNION ALL SELECT MAX(created_at) FROM test_runs WHERE run_id = ?)",
        (run_id, run_id, run_id),
    ).fetchone()
    return parse_stamp(row["at"]) if row is not None else None


def _transcript_written(agent: dict) -> datetime | None:
    raw = agent.get("transcript_path")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(Path(raw).stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _stalled(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    agents: list[dict],
    by_session: dict[str, dict],
    now: datetime,
) -> Finding | None:
    oracle = next((a for a in agents if a["role"] == "oracle"), None)
    if oracle is None:
        return None
    run_id = run["run_id"]
    if conn.execute(
        "SELECT 1 FROM directives WHERE run_id = ? AND state = 'open' AND outcome = 'needs_user'",
        (run_id,),
    ).fetchone():
        return None
    if conn.execute(
        "SELECT 1 FROM spawn_queue WHERE run_id = ? AND state = 'queued'", (run_id,)
    ).fetchone():
        return None
    members = [a for a in agents if a["role"] != "oracle"]
    live = [
        by_session[a["agent_id"]]
        for a in agents
        if a["agent_id"] in by_session and sessions.is_running(by_session[a["agent_id"]])
    ]
    if live:
        # Not the Oracle's status: its armed watchdog Monitor keeps its session "busy" while
        # it is idle, so its transcript's last write says whether it is working.
        if any(
            str(by_session[a["agent_id"]].get("status") or "").lower() == "busy"
            for a in members
            if a["agent_id"] in by_session
        ):
            return None
        if any(a["state"] == "working" for a in members):
            return None
        stamps = [_last_activity(conn, run_id), _transcript_written(oracle)]
        last = max((s for s in stamps if s is not None), default=None)
        if last is not None and now - last < IDLE_STALL:
            return None
    handoffs = submitted_handoffs(conn, run_id, unpaused_only=True)
    unlocked = conn.execute(
        "SELECT COUNT(*) AS n FROM phases WHERE run_id = ? AND state = 'unlocked' "
        "AND paused_at IS NULL",
        (run_id,),
    ).fetchone()["n"]
    directives = conn.execute(
        "SELECT COUNT(*) AS n FROM directives WHERE run_id = ? AND state = 'open' "
        "AND source != 'watchdog'",
        (run_id,),
    ).fetchone()["n"]

    pending: list[str] = []
    if members:
        pending.append(f"{len(members)} live agent(s)")
    if handoffs:
        pending.append(f"{len(handoffs)} submitted handoff(s)")
    if unlocked:
        pending.append(f"{unlocked} unlocked phase(s)")
    if directives:
        pending.append(f"{directives} open directive(s)")

    by_id = {a["agent_id"]: a for a in members}
    targets: list[str] = []
    for handoff in handoffs:
        reviewer = by_id.get(handoff["reviewer_id"])
        if reviewer is not None and reviewer["name"] not in targets:
            targets.append(reviewer["name"])
    for agent in members:
        if unread_count(conn, run_id, agent["name"]) and agent["name"] not in targets:
            targets.append(agent["name"])
    for issue in conn.execute(
        "SELECT coder.parent_agent_id AS lead_id FROM issues i "
        "JOIN files f ON f.file_id = i.file_id "
        "LEFT JOIN agents coder ON coder.name = f.owner_agent_id AND coder.run_id = i.run_id "
        "WHERE i.run_id = ? AND i.state = 'open'",
        (run_id,),
    ).fetchall():
        lead = by_id.get(issue["lead_id"])
        if lead is not None and lead["name"] not in targets:
            targets.append(lead["name"])

    if live:
        where = f"no agent of the run has worked for {_minutes(IDLE_STALL)} minutes"
    else:
        where = "no session of the run is running"
    if pending:
        detail = f"{where}, and work is pending: {', '.join(pending)}"
    else:
        detail = f"{where}, and the run is not finished"
    if targets:
        by_name = {a["name"]: a for a in members}
        calls = ", ".join(_wake_call(by_name[t], by_session) for t in targets)
        step = f"Wake the agent whose work is pending: {calls}."
        if "SendMessage(" in calls:
            step = f"{step} {wake.AMBIGUOUS_NAME_RULE}"
    elif pending:
        step = "Continue the plan: resume or spawn the agent that owns the pending work."
    else:
        step = "Continue the plan, or call run_finish."
    return _finding(oracle, "stalled", detail, step)


def _wake_call(agent: dict, by_session: dict[str, dict]) -> str:
    entry = by_session.get(agent["agent_id"])
    return wake.route_wakeup(
        {
            "to_agent_id": agent["agent_id"],
            "to_name": agent["name"],
            "to_session_name": agent["session_name"],
        },
        live=entry is not None and sessions.is_running(entry),
    ).next


def _pause_reason(conn: sqlite3.Connection, run_id: int) -> str | None:
    row = conn.execute(
        "SELECT e.reason FROM agent_events e JOIN agents a ON a.agent_id = e.agent_id "
        "WHERE a.run_id = ? AND e.to_state = 'paused' ORDER BY e.event_id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return row["reason"] if row is not None else None


def directive_body(finding: Finding) -> str:
    session = finding.session_name or "none recorded"
    return (
        f"Watchdog finding {finding.kind}: {finding.name} (session {session}): "
        f"{finding.detail}. Next: {finding.next_step} Then resolve this directive with "
        "directive_resolve."
    )


def _needs_confirmation(kind: str) -> bool:
    # A run passes through "no session running" for a moment while one session exits and
    # another resumes, so a stall is reported only once two passes in a row see it.
    return kind == "stalled"


def record(
    conn: sqlite3.Connection, run_id: int, findings: list[Finding], now: datetime
) -> list[dict]:
    at = stamp(now)
    reported: list[dict] = []
    with write_tx(conn):
        live = {
            (row["agent_id"], row["kind"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM watchdog_findings WHERE run_id = ? AND cleared_at IS NULL",
                (run_id,),
            )
        }
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            key = (finding.agent_id, finding.kind)
            if key in seen:
                continue
            seen.add(key)
            row = live.get(key)
            if row is None:
                cur = conn.execute(
                    "INSERT INTO watchdog_findings (run_id, agent_id, kind, detail, "
                    "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, finding.agent_id, finding.kind, finding.detail, at, at),
                )
                finding_id = cur.lastrowid
                if _needs_confirmation(finding.kind):
                    continue
            else:
                finding_id = row["finding_id"]
                conn.execute(
                    "UPDATE watchdog_findings SET detail = ?, last_seen_at = ? "
                    "WHERE finding_id = ?",
                    (finding.detail, at, finding_id),
                )
                if row["reported_at"] is not None:
                    continue
            cur = conn.execute(
                "INSERT INTO directives (run_id, source, sender_name, body, created_at) "
                "VALUES (?, 'watchdog', 'watchdog', ?, ?)",
                (run_id, directive_body(finding), at),
            )
            conn.execute(
                "UPDATE watchdog_findings SET reported_at = ?, directive_id = ? "
                "WHERE finding_id = ?",
                (at, cur.lastrowid, finding_id),
            )
            reported.append(
                dict(
                    conn.execute(
                        "SELECT * FROM watchdog_findings WHERE finding_id = ?", (finding_id,)
                    ).fetchone()
                )
            )
        for key, row in live.items():
            if key not in seen:
                conn.execute(
                    "UPDATE watchdog_findings SET cleared_at = ? WHERE finding_id = ?",
                    (at, row["finding_id"]),
                )
    return reported


def driver_notices(
    conn: sqlite3.Connection, run_id: int, reported: list[dict], channels: Sequence[str]
) -> list[dict]:
    notices: list[dict] = []
    with write_tx(conn):
        for row in reported:
            entry = DRIVER_ERRORS.get(row["kind"])
            if entry is None:
                continue
            label, kind = entry
            agent = conn.execute(
                "SELECT name, role FROM agents WHERE agent_id = ?", (row["agent_id"],)
            ).fetchone()
            if agent is None or agent["role"] != "driver":
                continue
            notice = notify.record(
                conn,
                run_id,
                kind=kind,
                event_key=f"directive:{row['directive_id']}",
                message=f"Driver {label}: {agent['name']}, {row['detail']}",
                channels=channels,
            )
            if notice is not None:
                notices.append(notice)
    return notices


def user_notices(
    conn: sqlite3.Connection, run_id: int, reported: list[dict], channels: Sequence[str]
) -> list[dict]:
    notices: list[dict] = []
    with write_tx(conn):
        for row in reported:
            if row["kind"] not in USER_KINDS:
                continue
            agent = conn.execute(
                "SELECT name, role FROM agents WHERE agent_id = ?", (row["agent_id"],)
            ).fetchone()
            on_oracle = agent is not None and agent["role"] == "oracle"
            if row["kind"] == "needs_user":
                name = agent["name"] if agent is not None else row["agent_id"]
                message = f"The swarm needs you: {name}, {row['detail']}"
            else:
                message = f"The swarm run stalled: {row['detail']}"
            notice = notify.record(
                conn,
                run_id,
                kind="warning",
                event_key=f"directive:{row['directive_id']}",
                message=message,
                channels=_os_only(channels)
                if row["kind"] == "needs_user" and on_oracle
                else channels,
            )
            if notice is not None:
                notices.append(notice)
    return notices


def _os_only(channels: Sequence[str]) -> list[str]:
    # The Oracle cannot send a PushNotification while its own session is the one stuck.
    return [c for c in channels if c != "push"]


def waiting_questions(conn: sqlite3.Connection, run_id: int) -> list[str]:
    return [
        str(row["question"] or row["body"])
        for row in conn.execute(
            "SELECT question, body FROM directives WHERE run_id = ? AND state = 'open' "
            "AND outcome = 'needs_user' ORDER BY directive_id",
            (run_id,),
        )
    ]


def reminder(conn: sqlite3.Connection, run: sqlite3.Row, pause_reason: str | None) -> str | None:
    questions = waiting_questions(conn, run["run_id"])
    parts: list[str] = []
    if run["state"] == "paused":
        parts.append(pauses.pause_message(pause_reason or "no reason recorded", run["pause_check"]))
    if questions:
        parts.append(f"{len(questions)} question(s) wait on you: {questions[0]}")
    return " ".join(parts) or None


class Watchdog:
    def __init__(
        self,
        repo_root: Path,
        conn: sqlite3.Connection,
        settings: WatchdogSettings,
        *,
        exit_server: Callable[[], None],
        activity: Callable[[], object] = lambda: None,
        log: Callable[[str], None] = _log,
        notify_channels: Sequence[str] = NOTIFY_CHANNELS,
        ledger: Ledger | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.ledger = ledger
        self.conn = conn
        self.settings = settings
        self.notify_channels = tuple(notify_channels)
        self._exit_server = exit_server
        self._activity = activity
        self._log = log
        self._activity_seen = activity()
        self._idle_since: datetime | None = None

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self.settings.interval_seconds)

    def tick(self, now: datetime) -> None:
        for test_run_id in testrun.sweep(self.conn, now):
            self._log(f"abandoned test run {test_run_id}: {testrun.NO_RUNNER}")
        run = live_run(self.conn)
        try:
            listing: list[dict] | None = sessions.list_sessions()
        except LedgerError as exc:
            self._log(f"skipped a pass: {exc}")
            listing = None
        if run is not None and run["state"] == "active" and listing is not None:
            listing = self._memory_pass(listing, now)
            findings = scan(self.conn, listing, now, self.settings)
            reported = record(self.conn, run["run_id"], findings, now)
            for row in reported:
                self._log(
                    f"reported {row['kind']} for {row['agent_id']} "
                    f"as directive {row['directive_id']}"
                )
            for notice in driver_notices(self.conn, run["run_id"], reported, self.notify_channels):
                notify.deliver(notice, self.notify_channels, log=self._log)
            for notice in user_notices(self.conn, run["run_id"], reported, self.notify_channels):
                notify.deliver(notice, self.notify_channels, log=self._log)
            self._wake_oracle(run, listing, now)
        if run is not None and run["state"] == "paused":
            run = self._try_clear_check(run, now)
        if run is not None:
            self._remind(run, now)
        self._check_idle(run, listing, now)

    def _try_clear_check(self, run: sqlite3.Row, now: datetime) -> sqlite3.Row:
        check = pauses.CHECKS.get(run["pause_check"] or "")
        if check is None:
            return run
        try:
            passed = check.passes(self.repo_root)
        except Exception as exc:
            self._log(f"the clear-check {check.name} failed: {type(exc).__name__}: {exc}")
            return run
        if not passed:
            return run
        oracle = latest_live_oracle(self.conn, run["run_id"])
        with write_tx(self.conn) as conn:
            note = pauses.clear_resume(conn, run, oracle["agent_id"] if oracle else "watchdog", now)
            notice = notify.record(
                conn,
                run["run_id"],
                kind="done",
                event_key=f"resumed:{run['paused_at']}",
                message=f"The swarm run resumed by itself: {check.label}.",
                channels=self.notify_channels,
            )
        notify.deliver(notice, self.notify_channels, log=self._log)
        self._log(f"run {run['run_id']}: {note}")
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()

    def _remind(self, run: sqlite3.Row, now: datetime) -> None:
        every = timedelta(minutes=self.settings.remind_minutes)
        if every <= timedelta(0) or "os" not in self.notify_channels:
            return
        reason = _pause_reason(self.conn, run["run_id"]) if run["state"] == "paused" else None
        message = reminder(self.conn, run, reason)
        last = parse_stamp(run["reminded_at"])
        if message is None or (last is not None and now - last < every):
            if message is None and last is not None:
                self._set_reminded(run["run_id"], None)
            return
        self._set_reminded(run["run_id"], stamp(now))
        if last is not None:
            notify.show(notify.one_line(message), level=notify.LEVELS["warning"], log=self._log)

    def _set_reminded(self, run_id: int, at: str | None) -> None:
        with write_tx(self.conn) as conn:
            conn.execute("UPDATE runs SET reminded_at = ? WHERE run_id = ?", (at, run_id))

    def _memory_pass(self, listing: list[dict], now: datetime) -> list[dict]:
        ledger = self.ledger
        if ledger is None:
            return listing
        changed = False
        steps: list[tuple[str, Callable[[], object]]] = [
            ("sample memory", lambda: ledger.sample_memory(listing)),
            (
                "park agents",
                lambda: ledger.mark_parked(listing, now, REGISTER_GRACE.total_seconds()),
            ),
            ("resume parked agents", lambda: ledger.resume_parked(listing)),
            ("start queued spawns", lambda: ledger.start_queued_spawns(listing)),
            ("park idle parents", lambda: ledger.park_idle_parents(listing)),
        ]
        for label, step in steps:
            try:
                done = step()
            except Exception as exc:
                self._log(f"could not {label}: {type(exc).__name__}: {exc}")
                continue
            if done:
                changed = True
                self._log(f"{label}: {len(done) if isinstance(done, list) else done}")
        if not changed:
            return listing
        try:
            return sessions.list_sessions()
        except LedgerError:
            return listing

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.wait(self.settings.interval_seconds):
            try:
                self.tick(utcnow())
            except Exception as exc:
                self._log(f"a pass failed: {type(exc).__name__}: {exc}")

    def _wake_oracle(self, run: sqlite3.Row, listing: list[dict], now: datetime) -> None:
        oracle = latest_live_oracle(self.conn, run["run_id"])
        if oracle is None:
            return
        entry = sessions.find_session(listing, oracle["agent_id"])
        running = entry is not None and sessions.is_running(entry)
        if running and str((entry or {}).get("status") or "").lower() == "busy":
            return
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n, MIN(directive_id) AS first FROM directives WHERE run_id = ? "
            "AND source = 'watchdog' AND state = 'open' AND notified_at IS NULL "
            "AND created_at <= ?",
            (run["run_id"], stamp(now - self.interval)),
        ).fetchone()
        waiting = int(pending["n"])
        if not waiting:
            return
        if running:
            self._oracle_unreachable(run, oracle, entry, waiting, pending["first"])
            return
        attempts = [
            parse_stamp(row["at"])
            for row in self.conn.execute(
                "SELECT at FROM agent_events WHERE agent_id = ? AND to_state = ? AND at > ? "
                "ORDER BY event_id",
                (oracle["agent_id"], WAKE_STATE, oracle["last_heartbeat_at"] or ""),
            )
        ]
        last = attempts[-1] if attempts else None
        if last is not None and now - last < WAKE_EVERY:
            return
        if len(attempts) >= MAX_WAKES:
            self._pause(run, oracle, now)
            return

        message = wake.timed(
            f"The watchdog reported {waiting} finding(s). Read directive_inbox.",
            wake.time_signal(self.conn, run["run_id"], now),
        )
        agent_id = oracle["agent_id"]
        if not oracle["session_name"]:
            # A resume without a recorded session starts a fresh session named after the message.
            reason = (
                f"the watchdog cannot wake the Oracle: no session is recorded for "
                f"{oracle['agent_id']!r}"
            )
        else:
            try:
                bg_id = sessions.resume(
                    oracle["agent_id"],
                    message,
                    cwd=self.repo_root,
                    name=oracle["session_name"],
                    options=self._oracle_options(oracle["model"]),
                )
                reason = f"the watchdog woke the Oracle: {message}"
                agent_id = self._adopt_resumed(oracle, bg_id)
            except LedgerError as exc:
                reason = f"the watchdog could not resume the Oracle: {exc}"
        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, oracle["state"], WAKE_STATE, reason, stamp(now)),
            )
        self._log(reason)

    def _oracle_unreachable(
        self, run: sqlite3.Row, oracle: sqlite3.Row, entry: dict | None, count: int, first: int
    ) -> None:
        session = oracle["session_name"] or oracle["name"]
        asks = waiting_on_user(entry)
        state = f"waits on {asks}" if asks else "sits idle with no watchdog listener"
        with write_tx(self.conn) as conn:
            notice = notify.record(
                conn,
                run["run_id"],
                kind="warning",
                event_key=f"oracle_unreachable:{first}",
                message=(
                    f"The swarm's Oracle session {session} {state}, and {count} watchdog "
                    "finding(s) wait for it. Open it in agent view."
                ),
                channels=_os_only(self.notify_channels),
            )
        if notice is not None:
            self._log(f"the Oracle's session runs but {state}; notified the user")
        notify.deliver(notice, self.notify_channels, log=self._log)

    def _adopt_resumed(self, agent: sqlite3.Row, bg_id: str | None) -> str:
        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE agents SET bg_id = COALESCE(?, bg_id), resume_started_at = {NOW} "
                "WHERE agent_id = ?",
                (bg_id, agent["agent_id"]),
            )
        new_id = sessions.wait_for_resumed(bg_id, agent["session_name"])
        if new_id is None:
            return agent["agent_id"]
        with write_tx(self.conn) as conn:
            rekey.adopt(conn, agent["agent_id"], new_id)
        return new_id

    def _oracle_options(self, model: str | None) -> list[str]:
        from .agentfiles import session_options
        from .serve import server_url
        from .settings import load_settings

        settings = load_settings(self.repo_root)
        try:
            return session_options(
                self.repo_root,
                "oracle",
                model,
                server_url(self.repo_root),
                effort=settings.effort.get("oracle"),
                prompt_cache_ttl=settings.prompt_cache_ttl.get("oracle"),
                permission_mode=settings.permission_mode.get("oracle"),
            )
        except LedgerError:
            return []

    def _pause(self, run: sqlite3.Row, oracle: sqlite3.Row, now: datetime) -> None:
        with write_tx(self.conn) as conn:
            notice = pauses.record_pause(
                conn,
                run["run_id"],
                oracle["agent_id"],
                PAUSE_REASON,
                None,
                self.notify_channels,
                now,
            )
        notify.deliver(notice, self.notify_channels, log=self._log)
        self._log(f"paused run {run['run_id']}: {PAUSE_REASON}")

    def _check_idle(
        self, run: sqlite3.Row | None, listing: list[dict] | None, now: datetime
    ) -> None:
        activity = self._activity()
        if activity != self._activity_seen:
            self._activity_seen = activity
            self._idle_since = None
            return
        if run is not None and (run["state"] == "active" or run["pause_check"]):
            self._idle_since = None
            return
        if run is not None and listing is not None and self._any_running(run, listing):
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = now
            return
        if now - self._idle_since >= timedelta(minutes=self.settings.idle_exit_minutes):
            self._log("no run is active and no session of the run runs; the server exits")
            self._exit_server()

    def _any_running(self, run: sqlite3.Row, listing: list[dict]) -> bool:
        ids = {
            row["agent_id"]
            for row in self.conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND ended_at IS NULL",
                (run["run_id"],),
            )
        }
        return any(sessions.is_running(entry) for entry in listing if entry.get("sessionId") in ids)


def start(
    repo_root: Path,
    conn: sqlite3.Connection,
    settings: WatchdogSettings,
    *,
    exit_server: Callable[[], None],
    activity: Callable[[], object],
    notify_channels: Sequence[str] = NOTIFY_CHANNELS,
    ledger: Ledger | None = None,
) -> None:
    dog = Watchdog(
        repo_root,
        conn,
        settings,
        exit_server=exit_server,
        activity=activity,
        notify_channels=notify_channels,
        ledger=ledger,
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=dog.run_forever, args=(stop,), name="swarm-watchdog", daemon=True
    )
    thread.start()
