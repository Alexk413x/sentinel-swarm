from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import memory, notify, pauses, permission, procs, rekey, sessions, testrun, transcript, wake
from .clock import parse_stamp, stamp, utcnow
from .db import write_tx
from .identity import ROLES, LedgerError
from .queries import (
    USER_DIRECTIVE_SOURCES,
    latest_live_oracle,
    live_run,
    logical_state,
    submitted_handoffs,
    unread_count,
)
from .settings import NOTIFY_CHANNELS, WatchdogSettings, channels_for
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
RESUME_FAILURE_LIMIT = 3
# Findings identified by the agent's name, so one agent that dies again under a new session id
# after a resume keeps one finding; one that cleared less than this long ago is reopened.
NAMED_KINDS = frozenset({"crashed"})
# One idle episode of an agent: its session shows idle (stuck) or blocked by turns, and each
# change would otherwise file a second directive.
IDLE_KINDS = frozenset({"stuck", "blocked"})
CRASH_REOPEN = timedelta(minutes=30)
# A released session can take a moment to exit after claude stop returns.
STRAY_GRACE = timedelta(minutes=1)
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
HELD_WAIT = "a permission request held for the run panel"


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
    held = {r["agent_id"]: r for r in permission.open_requests(conn, run["run_id"], now)}
    for agent_id in held.keys() & by_session.keys():
        by_session[agent_id] = {
            **by_session[agent_id],
            "status": "waiting",
            "waitingFor": HELD_WAIT,
        }
    names = {a["agent_id"]: a["name"] for a in agents}
    by_id = {a["agent_id"]: a for a in agents}
    superseded = _superseded_coders(conn, run["run_id"])
    user_findings, user_waits, blocked = _classify_waits(agents, by_session, held, superseded)

    findings: list[Finding] = []
    for agent in agents:
        agent_id = agent["agent_id"]
        entry = by_session.get(agent_id)
        if agent_id in superseded:
            findings.append(_superseded_finding(agent, superseded[agent_id], by_id))
            continue
        if agent["role"] in _MEMBER_ROLES:
            waiting = _waits_on_work(conn, run["run_id"], agent_id)
            if agent_id in blocked:
                if not waiting:
                    findings += _blocked_findings(agent, now, settings, by_id)
            elif agent_id not in user_waits:
                found = _session_findings(conn, agent, entry, now, settings)
                findings += [f for f in found if not (waiting and f.kind == "stuck")]
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
        elif agent["role"] == "driver" and agent_id not in user_waits:
            findings += _driver_findings(conn, agent, entry, now)
        context = _context_high(agent, settings)
        if context is not None:
            findings.append(_finding(agent, "context_high", context, _context_step(agent, names)))

    waits = user_waits | blocked
    findings += user_findings
    findings += _landed_wait_findings(conn, run["run_id"], agents, by_session)
    findings += _unsent_findings(conn, run["run_id"], agents, by_session, now, settings)
    findings += _queued_findings(conn, run["run_id"], agents, now, settings)
    findings += _unstarted_brief_findings(conn, run["run_id"], agents, by_session, now, settings)
    findings += _unmanaged_phase_findings(conn, run["run_id"], agents)
    findings += _unread_directive_findings(conn, run["run_id"], agents, now, settings)
    findings += _stray_process_findings(conn, run["run_id"], now)

    stalled = _stalled(conn, run, agents, by_session, now)
    if stalled is not None:
        findings.append(stalled)
    run_stalled = _run_stalled(conn, run, agents, by_session, now, settings, waits, user_waits)
    if run_stalled is not None:
        findings.append(run_stalled)
    return findings


def session_wait(entry: dict | None) -> str | None:
    if entry is None:
        return None
    waiting = " ".join(str(entry.get("waitingFor") or "").split())
    states = {str(entry.get(key) or "").lower() for key in ("status", "state")}
    if "waiting" in states:
        return waiting or "input"
    if "blocked" in states:
        return waiting or "the user (blocked)"
    if any(word in waiting.lower() for word in _USER_WAITS):
        return waiting
    return None


def _classify_waits(
    agents: list[dict],
    by_session: dict[str, dict],
    held: dict[str, dict],
    skip: Mapping[str, object],
) -> tuple[list[Finding], set[str], set[str]]:
    # Claude Code also reports "waiting" or "blocked" for a session that ended its turn on a
    # work blocker, and for an idle Oracle; only a held or pending request needs a person.
    findings: list[Finding] = []
    user_waits: set[str] = set()
    blocked: set[str] = set()
    for agent in agents:
        agent_id = agent["agent_id"]
        if agent_id in skip:
            continue
        waiting = session_wait(by_session.get(agent_id))
        if waiting is None:
            continue
        session = agent["session_name"] or agent["name"]
        asked = held.get(agent_id)
        if asked is not None:
            user_waits.add(agent_id)
            findings.append(_held_finding(agent, session, asked))
            continue
        request = pending_request(agent.get("transcript_path"))
        if request is None:
            blocked.add(agent_id)
            continue
        user_waits.add(agent_id)
        findings.append(
            _finding(
                agent,
                "needs_user",
                f"its session {session} waits on {waiting}: {request}",
                f"The user has been notified. Tell the user to open the session {session} in "
                "agent view and answer it, and keep the rest of the run moving.",
            )
        )
    return findings, user_waits, blocked


def _waits_on_work(conn: sqlite3.Connection, run_id: int, agent_id: str) -> bool:
    # A parent that ended its turn while its children work is waiting, not stuck. A child in
    # handed_up does not count: it waits on the parent's review. A parked child counts by the
    # state it had before the park.
    return (
        conn.execute(
            "SELECT 1 FROM agents WHERE parent_agent_id = ? AND ended_at IS NULL "
            "AND (state IN ('registered', 'working') "
            "OR (state = 'parked' AND state_before_park IN ('registered', 'working'))) "
            "UNION ALL SELECT 1 FROM spawn_queue WHERE run_id = ? AND parent_agent_id = ? "
            "AND state = 'queued' "
            "UNION ALL SELECT 1 FROM file_waits WHERE agent_id = ? AND closed_at IS NULL "
            "LIMIT 1",
            (agent_id, run_id, agent_id, agent_id),
        ).fetchone()
        is not None
    )


def _blocked_findings(
    agent: dict, now: datetime, settings: WatchdogSettings, by_id: dict[str, dict]
) -> list[Finding]:
    if agent["state"] != "working":
        return []
    last = parse_stamp(agent["last_heartbeat_at"]) or parse_stamp(agent["started_at"])
    if last is None or now - last < timedelta(minutes=settings.stuck_minutes):
        return []
    session = agent["session_name"] or agent["name"]
    label = json.dumps(session)
    parent = by_id.get(agent["parent_agent_id"] or "")
    if parent is None or parent["role"] == "oracle":
        step = (
            f"Ask it what blocks it with SendMessage(to={label}), then unblock it, or have it "
            "call issue_escalate, or release it."
        )
    else:
        parent_label = json.dumps(parent["session_name"] or parent["name"])
        step = (
            f"Wake {parent['name']} (SendMessage(to={parent_label})) to unblock {agent['name']}: "
            "answer its question, return_work with new direction, or release it."
        )
    return [
        _finding(
            agent,
            "blocked",
            f"its session {session} ended its turn blocked on its work, with no permission "
            f"prompt or tool request pending, and has shown no activity for "
            f"{_minutes(now - last)} minutes",
            step,
        )
    ]


def _superseded_coders(conn: sqlite3.Connection, run_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT a.agent_id, f.file_id, f.path FROM agents a JOIN files f ON f.file_id = a.file_id "
        "WHERE a.run_id = ? AND a.role = 'coder' AND a.ended_at IS NULL "
        "AND f.state = 'superseded'",
        (run_id,),
    ).fetchall()
    return {row["agent_id"]: row for row in rows}


def _superseded_finding(agent: dict, file_row: sqlite3.Row, by_id: dict[str, dict]) -> Finding:
    parent = by_id.get(agent["parent_agent_id"] or "")
    who = parent["name"] if parent is not None else "its parent"
    return _finding(
        agent,
        "superseded",
        f"its file {file_row['file_id']} ({file_row['path']}) was superseded by a later claim",
        f"Ask {who} to release it with "
        f"agent_release(target_agent_id={json.dumps(agent['agent_id'])}); the later claim owns "
        "the file, and the session holds memory.",
    )


def _held_finding(agent: dict, session: str, asked: dict) -> Finding:
    request = _clip(f"{asked['tool']} {asked['summary']}")
    return _finding(
        agent,
        "needs_user",
        f"its session {session} waits on {HELD_WAIT}: {request}; answer permission request "
        f"{asked['request_id']} in the run panel",
        "The user has been notified. Tell the user to allow or deny permission request "
        f"{asked['request_id']} in the run panel; unanswered by {asked['expires_at']}, the "
        f"session {session} shows its own prompt in agent view. Keep the rest of the run moving.",
    )


def pending_request(raw: object) -> str | None:
    use = transcript.pending_use(raw)
    return None if use is None else _describe_request(use)


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
    return _clip(f"{name} {permission.input_text(tool_input)}")


def _clip(text: str) -> str:
    return permission.one_line(text, _REQUEST_CHARS)


def _run_stalled(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    agents: list[dict],
    by_session: dict[str, dict],
    now: datetime,
    settings: WatchdogSettings,
    waits: set[str],
    user_waits: set[str],
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
        if agent["state"] == "working" and running and agent["agent_id"] not in waits:
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
    waiting = [a["session_name"] or a["name"] for a in agents if a["agent_id"] in user_waits]
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
                f"{_minutes(now - queued)} minutes in the queue: {row['reason']}",
                "Release a live agent that is done to free a slot, free memory on the machine, "
                "lower memory.headroom_mb, or raise parallelism_cap, or call run_pause and tell "
                "the user the run waits on capacity.",
            )
        )
    return findings


_UNACKED_BRIEF = "b.acked_at IS NULL"


def _unstarted_brief_findings(
    conn: sqlite3.Connection,
    run_id: int,
    agents: list[dict],
    by_session: dict[str, dict],
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    by_id = {a["agent_id"]: a for a in agents}
    after = timedelta(minutes=settings.stuck_minutes)
    findings: list[Finding] = []
    for brief in conn.execute(
        "SELECT b.* FROM briefs b "
        f"WHERE b.run_id = ? AND {_UNACKED_BRIEF} "
        "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.name = b.child_name "
        "AND a.ended_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM spawn_queue q WHERE q.child_name = b.child_name "
        "AND q.state = 'queued') "
        "AND (b.child_role != 'coder' OR EXISTS (SELECT 1 FROM files f "
        "WHERE f.file_id = b.file_id AND f.owner_agent_id = b.child_name "
        "AND f.released_at IS NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM phases p WHERE p.phase_id = b.phase_id "
        "AND p.paused_at IS NOT NULL) "
        "ORDER BY b.brief_id",
        (run_id,),
    ):
        parent = by_id.get(brief["parent_agent_id"])
        created = parse_stamp(brief["created_at"])
        if parent is None or created is None or now - created < after:
            continue
        child = json.dumps(brief["child_name"])
        step = (
            f"Call agent_spawn(child_name={child}) to start it."
            if parent["role"] == "oracle"
            else f"Wake {parent['name']} ({_wake_call(parent, by_session)}) to call "
            f"agent_spawn(child_name={child})."
        )
        if "SendMessage(" in step:
            step = f"{step} {wake.AMBIGUOUS_NAME_RULE}"
        findings.append(
            _finding(
                parent,
                "brief_unstarted",
                f"its brief for {brief['child_name']} ({brief['child_role']}) has stood "
                f"unacknowledged for {_minutes(now - created)} minutes: no agent runs under "
                "that name and no spawn is queued for it",
                step,
            )
        )
    return findings


def _unread_directive_findings(
    conn: sqlite3.Connection,
    run_id: int,
    agents: list[dict],
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    oracle = next((a for a in agents if a["role"] == "oracle"), None)
    after = timedelta(minutes=settings.unsent_wakeup_minutes)
    if oracle is None or after <= timedelta(0):
        return []
    marks = ", ".join("?" for _ in USER_DIRECTIVE_SOURCES)
    waiting: list[tuple[int, timedelta]] = []
    for row in conn.execute(
        "SELECT directive_id, created_at FROM directives WHERE run_id = ? "
        f"AND source IN ({marks}) AND state = 'open' AND seen_at IS NULL ORDER BY directive_id",
        (run_id, *USER_DIRECTIVE_SOURCES),
    ):
        made = parse_stamp(row["created_at"])
        if made is not None and now - made >= after:
            waiting.append((row["directive_id"], now - made))
    if not waiting:
        return []
    ids = ", ".join(str(directive_id) for directive_id, _ in waiting)
    age = _minutes(max(waited for _, waited in waiting))
    return [
        _finding(
            oracle,
            "directive_unread",
            f"directive(s) {ids} from the user have waited unread for up to {age} minutes",
            "Call directive_inbox() now, then apply each directive or acknowledge one you "
            "cannot apply at once with directive_ack(directive_id, note).",
        )
    ]


def _unmanaged_phase_findings(
    conn: sqlite3.Connection, run_id: int, agents: list[dict]
) -> list[Finding]:
    oracle = next((a for a in agents if a["role"] == "oracle"), None)
    if oracle is None:
        return []
    phases = [
        f"{row['phase_id']} ({row['name']}, {row['state']}; last Manager {row['manager']})"
        for row in conn.execute(
            "SELECT p.phase_id, p.name, p.state, "
            "(SELECT a.name FROM agents a WHERE a.phase_id = p.phase_id AND a.role = 'manager' "
            "ORDER BY a.started_at DESC LIMIT 1) AS manager FROM phases p "
            "WHERE p.run_id = ? AND p.state IN ('unlocked', 'working') AND p.paused_at IS NULL "
            "AND EXISTS (SELECT 1 FROM agents a WHERE a.phase_id = p.phase_id "
            "AND a.role = 'manager') "
            "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.phase_id = p.phase_id "
            "AND a.role = 'manager' AND a.ended_at IS NULL) ORDER BY p.ordinal",
            (run_id,),
        )
    ]
    if not phases:
        return []
    return [
        _finding(
            oracle,
            "phase_unmanaged",
            f"phase(s) {', '.join(phases)} have work left and no live Manager",
            "Brief and spawn a new Manager for each phase, or call file_accept_incomplete for "
            "a file that cannot finish, or run_close if the run cannot.",
        )
    ]


def _stray_process_findings(conn: sqlite3.Connection, run_id: int, now: datetime) -> list[Finding]:
    rows = conn.execute(
        "SELECT p.*, a.name, a.session_name FROM released_processes p "
        "JOIN agents a ON a.agent_id = p.agent_id WHERE a.run_id = ? AND p.recorded_at <= ? "
        "ORDER BY p.agent_id, p.pid",
        (run_id, stamp(now - STRAY_GRACE)),
    ).fetchall()
    if not rows:
        return []
    table = procs.snapshot()
    protected = procs.ancestors(table, os.getpid())
    by_agent: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_agent.setdefault(row["agent_id"], []).append(row)
    candidates: dict[str, list[procs.Proc]] = {}
    for agent_id, recorded in by_agent.items():
        found = procs.verified_strays(
            table, [(r["pid"], r["created"], bool(r["is_session"])) for r in recorded], protected
        )
        if found:
            candidates[agent_id] = found
    if not candidates:
        return []
    fresh = procs.snapshot()
    findings: list[Finding] = []
    for agent_id, found in candidates.items():
        left = [p for p in found if procs.identical(fresh.get(p.pid), p.created)]
        if not left:
            continue
        agent = {"agent_id": agent_id, "name": by_agent[agent_id][0]["name"]}
        agent["session_name"] = by_agent[agent_id][0]["session_name"]
        listed = ", ".join(f"{p.name} (pid {p.pid})" for p in left)
        command = stop_command([p.pid for p in left], sys.platform)
        findings.append(
            _finding(
                agent,
                "stray_process",
                f"it was released, but {len(left)} process(es) its session started still run: "
                f"{listed}",
                f"Tell the user to stop them with `{command}`; they hold files and memory.",
            )
        )
    return findings


def stop_command(pids: list[int], platform: str) -> str:
    if platform == "win32":
        return "taskkill /F " + " ".join(f"/PID {pid}" for pid in pids)
    return "kill " + " ".join(str(pid) for pid in pids)


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


@dataclass(frozen=True)
class ResumeHistory:
    failed: int = 0
    last: str | None = None
    hopeless: str | None = None


def _resume_history(
    conn: sqlite3.Connection, agent: dict, now: datetime, settings: WatchdogSettings
) -> ResumeHistory:
    """The failed resumes since the agent's last resume that stayed up, and whether any is left."""
    rows = conn.execute(
        "SELECT reason FROM agent_events WHERE agent_id = ? "
        "AND to_state IN ('resume_failed', 'resume_vanished') AND event_id > "
        "(SELECT COALESCE(MAX(event_id), 0) FROM agent_events "
        "WHERE agent_id = ? AND to_state = 'resume_stable') ORDER BY event_id",
        (agent["agent_id"], agent["agent_id"]),
    ).fetchall()
    failed = len(rows)
    last = rows[-1]["reason"] if rows else None
    if sessions.resume_source(agent) is None:
        return ResumeHistory(failed, last, sessions.NO_CONVERSATION)
    if last is not None and sessions.CLI_NO_CONVERSATION in last:
        return ResumeHistory(
            failed, last, "claude --resume found no conversation for its session id"
        )
    beat = parse_stamp(agent["last_heartbeat_at"]) or parse_stamp(agent["started_at"])
    quiet = beat is None or now - beat >= timedelta(minutes=settings.stuck_minutes)
    if failed >= RESUME_FAILURE_LIMIT and quiet:
        return ResumeHistory(
            failed, last, f"{failed} resumes failed and it has shown no activity since"
        )
    return ResumeHistory(failed, last)


def _crashed(
    agent: dict, entry: dict | None, now: datetime, history: ResumeHistory | None = None
) -> list[Finding]:
    state = logical_state(agent)
    started = parse_stamp(agent["started_at"])
    overdue = started is not None and now - started > REGISTER_GRACE
    if state != "working" and not (state == "registered" and overdue):
        return []
    history = history or ResumeHistory()
    detail = f"the ledger says {state}, but its session {_not_running(entry)}"
    if history.hopeless:
        detail += f"; {history.hopeless}"
    elif history.failed:
        detail += f"; {history.failed} resume{'' if history.failed == 1 else 's'} failed"
    if history.last:
        detail += f" (last: {history.last})"
    if history.hopeless:
        return [
            _finding(
                agent,
                "crashed",
                detail,
                "Its conversation cannot be resumed. Read what it left in the ledger with "
                "directive_inbox() and status_tree(), then have its parent release it with "
                f"agent_release(target_agent_id={json.dumps(agent['agent_id'])}) and brief a "
                "replacement under the same name, from its brief.",
            )
        ]
    return [
        _finding(
            agent,
            "crashed",
            detail,
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
    conn: sqlite3.Connection,
    agent: dict,
    entry: dict | None,
    now: datetime,
    settings: WatchdogSettings,
) -> list[Finding]:
    if not sessions.agent_running(entry, agent):
        return _crashed(agent, entry, now, _resume_history(conn, agent, now, settings))

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


def _context_window(models: str, overrides: dict[str, int] | None = None) -> int:
    given = overrides or {}
    name = models.lower()
    for family in sorted(given, key=len, reverse=True):
        if family != "default" and family in name:
            return given[family]
    if "haiku" in name:
        return _WINDOW_HAIKU
    return given.get("default", _WINDOW_1M)


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
    window = settings.context_window or _context_window(
        f"{agent['model'] or ''} {model}", settings.context_windows
    )
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


def _identity(kind: str, agent_id: str, name: str | None) -> tuple[str, str]:
    episode = "idle" if kind in IDLE_KINDS else kind
    return (name if kind in NAMED_KINDS and name else agent_id, episode)


def _reopen(conn: sqlite3.Connection, run_id: int, finding: Finding, now: datetime) -> dict | None:
    """The crashed finding of the same agent name that cleared a moment ago, made live again.

    A session that dies, is resumed, and dies again clears the finding in between and comes
    back under a new session id. The agent is one, so its finding is too.
    """
    row = conn.execute(
        "SELECT f.* FROM watchdog_findings f JOIN agents a ON a.agent_id = f.agent_id "
        "WHERE f.run_id = ? AND f.kind = ? AND a.name = ? AND f.cleared_at >= ? "
        "ORDER BY f.finding_id DESC LIMIT 1",
        (run_id, finding.kind, finding.name, stamp(now - CRASH_REOPEN)),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE watchdog_findings SET cleared_at = NULL, agent_id = ?, detail = ?, "
        "last_seen_at = ? WHERE finding_id = ?",
        (finding.agent_id, finding.detail, stamp(now), row["finding_id"]),
    )
    return dict(row)


def _directive_open(conn: sqlite3.Connection, directive_id: int | None) -> bool:
    row = conn.execute(
        "SELECT state FROM directives WHERE directive_id = ?", (directive_id,)
    ).fetchone()
    return row is not None and row["state"] == "open"


def record(
    conn: sqlite3.Connection, run_id: int, findings: list[Finding], now: datetime
) -> list[dict]:
    at = stamp(now)
    reported: list[dict] = []
    with write_tx(conn):
        live = {
            _identity(row["kind"], row["agent_id"], row["agent_name"]): dict(row)
            for row in conn.execute(
                "SELECT f.*, a.name AS agent_name FROM watchdog_findings f "
                "LEFT JOIN agents a ON a.agent_id = f.agent_id "
                "WHERE f.run_id = ? AND f.cleared_at IS NULL",
                (run_id,),
            )
        }
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            key = _identity(finding.kind, finding.agent_id, finding.name)
            if key in seen:
                continue
            seen.add(key)
            row = live.get(key)
            reopened = False
            if row is None and finding.kind in NAMED_KINDS:
                row = _reopen(conn, run_id, finding, now)
                reopened = row is not None
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
                if not reopened:
                    conn.execute(
                        "UPDATE watchdog_findings SET kind = ?, detail = ?, last_seen_at = ?, "
                        "agent_id = ? WHERE finding_id = ?",
                        (finding.kind, finding.detail, at, finding.agent_id, finding_id),
                    )
                if row["reported_at"] is not None and (
                    not reopened or _directive_open(conn, row["directive_id"])
                ):
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
    conn: sqlite3.Connection,
    run_id: int,
    reported: list[dict],
    channels: Sequence[str],
    events: Mapping[str, bool | list[str]] | None = None,
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
                channels=channels_for("driver", channels, events),
            )
            if notice is not None:
                notices.append(notice)
    return notices


def user_notices(
    conn: sqlite3.Connection,
    run_id: int,
    reported: list[dict],
    channels: Sequence[str],
    events: Mapping[str, bool | list[str]] | None = None,
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
            event_channels = channels_for(row["kind"], channels, events)
            notice = notify.record(
                conn,
                run_id,
                kind="warning",
                event_key=f"directive:{row['directive_id']}",
                message=message,
                channels=_os_only(event_channels)
                if row["kind"] == "needs_user" and on_oracle
                else event_channels,
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


def blocked_sessions(conn: sqlite3.Connection, run_id: int) -> list[str]:
    return [
        f"{row['name'] or row['agent_id']}, {row['detail']}"
        for row in conn.execute(
            "SELECT f.agent_id, f.detail, a.name FROM watchdog_findings f "
            "LEFT JOIN agents a ON a.agent_id = f.agent_id "
            "WHERE f.run_id = ? AND f.kind = 'needs_user' AND f.cleared_at IS NULL "
            "ORDER BY f.finding_id",
            (run_id,),
        )
    ]


def reminder(conn: sqlite3.Connection, run: sqlite3.Row, pause_reason: str | None) -> str | None:
    questions = waiting_questions(conn, run["run_id"])
    blocked = blocked_sessions(conn, run["run_id"])
    parts: list[str] = []
    if run["state"] == "paused":
        parts.append(pauses.pause_message(pause_reason or "no reason recorded", run["pause_check"]))
    if questions:
        parts.append(f"{len(questions)} question(s) wait on you: {questions[0]}")
    if blocked:
        parts.append(f"{len(blocked)} session(s) wait on you: {blocked[0]}")
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
        notify_events: Mapping[str, bool | list[str]] | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.ledger = ledger
        self.conn = conn
        self.settings = settings
        self.notify_channels = tuple(notify_channels)
        self.notify_events = dict(notify_events or {})
        self._exit_server = exit_server
        self._activity = activity
        self._log = log
        self._activity_seen = activity()
        self._idle_since: datetime | None = None
        self._orphans_seen: set[str] = set()

    def _channels(self, event: str) -> list[str]:
        return channels_for(event, self.notify_channels, self.notify_events)

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
        if run is not None and run["state"] == "active":
            run = self._pause_on_usage_limit(run, now)
        if run is not None and run["state"] == "active" and listing is not None:
            listing = self._memory_pass(listing, now)
            findings = scan(self.conn, listing, now, self.settings)
            reported = record(self.conn, run["run_id"], findings, now)
            for row in reported:
                self._log(
                    f"reported {row['kind']} for {row['agent_id']} "
                    f"as directive {row['directive_id']}"
                )
            for notice in driver_notices(
                self.conn, run["run_id"], reported, self.notify_channels, self.notify_events
            ):
                notify.deliver(notice, log=self._log)
            for notice in user_notices(
                self.conn, run["run_id"], reported, self.notify_channels, self.notify_events
            ):
                notify.deliver(notice, log=self._log)
            self._wake_oracle(run, listing, now)
        if run is not None and run["state"] == "paused":
            run = self._try_clear_check(run, now)
        if run is not None:
            self._remind(run, now)
        self._check_idle(run, listing, now)

    def _limited_agents(self, run_id: int) -> list[tuple[sqlite3.Row, dict]]:
        found: list[tuple[sqlite3.Row, dict]] = []
        for agent in self.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL "
            "AND transcript_path IS NOT NULL ORDER BY started_at",
            (run_id,),
        ):
            stop = transcript.usage_limit_stop(agent["transcript_path"])
            if stop is not None:
                found.append((agent, stop))
        return found

    def _pause_on_usage_limit(self, run: sqlite3.Row, now: datetime) -> sqlite3.Row:
        resumed = self.conn.execute(
            "SELECT MAX(e.at) FROM agent_events e JOIN agents a ON a.agent_id = e.agent_id "
            "WHERE a.run_id = ? AND e.from_state = 'paused' AND e.to_state = 'active'",
            (run["run_id"],),
        ).fetchone()[0]
        after = parse_stamp(resumed)
        limited = [
            (agent, stop)
            for agent, stop in self._limited_agents(run["run_id"])
            if after is None or (parse_stamp(stop["at"]) or now) > after
        ]
        if not limited:
            return run
        text = limited[0][1]["text"]
        reset = pauses.usage_reset(text, now)
        check = f"{pauses.USAGE_CHECK}@{stamp(reset)}" if reset is not None else None
        reason = (
            f"the account hit its usage limit ({text}); {len(limited)} session(s) stopped on "
            "it, and they are woken when the run resumes"
        )
        oracle = latest_live_oracle(self.conn, run["run_id"])
        with write_tx(self.conn) as conn:
            notice = pauses.record_pause(
                conn,
                run["run_id"],
                oracle["agent_id"] if oracle else "watchdog",
                reason,
                check,
                self._channels("run_paused"),
                now,
            )
        notify.deliver(notice, log=self._log)
        self._log(f"run {run['run_id']} paused: {reason}")
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()

    def _wake_limited(self, run_id: int) -> None:
        oracle = latest_live_oracle(self.conn, run_id)
        if oracle is None:
            return
        with write_tx(self.conn) as conn:
            for agent, _ in self._limited_agents(run_id):
                if agent["agent_id"] == oracle["agent_id"] or not agent["session_name"]:
                    continue
                conn.execute(
                    "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, "
                    "to_session_name, reason, pointer) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        oracle["agent_id"],
                        agent["agent_id"],
                        agent["name"],
                        agent["session_name"],
                        pauses.USAGE_WAKE_REASON,
                        "The account's usage limit reset and the run resumed. Continue the step "
                        "the limit stopped.",
                    ),
                )

    def _try_clear_check(self, run: sqlite3.Row, now: datetime) -> sqlite3.Row:
        check, arg = pauses.split_check(run["pause_check"])
        if check is None:
            return run
        try:
            passed = check.passes(self.repo_root, arg, now)
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
                channels=self._channels("run_resumed"),
            )
        notify.deliver(notice, log=self._log)
        self._log(f"run {run['run_id']}: {note}")
        if check.name == pauses.USAGE_CHECK:
            self._wake_limited(run["run_id"])
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()

    def _remind(self, run: sqlite3.Row, now: datetime) -> None:
        every = timedelta(minutes=self.settings.remind_minutes)
        if every <= timedelta(0) or "os" not in self._channels("pause_reminder"):
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
        freed_by_coders = [0]

        def park_blocked_coders() -> list[dict]:
            parked = ledger.park_blocked_coders(listing, now)
            freed_by_coders[0] = len(parked) * memory.estimate_mb(ledger.conn, "coder")
            return parked

        steps: list[tuple[str, Callable[[], object]]] = [
            ("sample memory", lambda: ledger.sample_memory(listing)),
            (
                "park agents",
                lambda: ledger.mark_parked(listing, now, REGISTER_GRACE.total_seconds()),
            ),
            ("resume parked agents", ledger.resume_parked),
            ("start queued spawns", lambda: ledger.start_queued_spawns(listing)),
            ("park blocked coders", park_blocked_coders),
            (
                "park idle parents",
                lambda: ledger.park_idle_parents(listing, freed_by_coders[0]),
            ),
            ("release finished parked parents", lambda: ledger.release_finished_parked(now)),
            ("stop orphaned sessions", lambda: self._stop_orphaned_sessions(ledger, listing)),
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

    def _stop_orphaned_sessions(self, ledger: Ledger, listing: list[dict]) -> list[str]:
        found = ledger.orphaned_sessions(listing)
        ripe = {bg_id: found[bg_id] for bg_id in found.keys() & self._orphans_seen}
        stopped, failed = ledger.stop_orphaned_sessions(ripe)
        self._orphans_seen = set(found) - set(stopped)
        for line in failed:
            self._log(f"could not stop {line}")
        return stopped

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
        elif (source := sessions.resume_source(dict(oracle))) is None:
            reason = f"the watchdog cannot wake the Oracle: {sessions.NO_CONVERSATION}"
        else:
            if not rekey.claim_resume(self.conn, oracle["agent_id"]):
                reason = "the watchdog did not wake the Oracle: a resume of it is already running"
            else:
                try:
                    bg_id = sessions.resume(
                        source,
                        message,
                        cwd=self.repo_root,
                        name=oracle["session_name"],
                        options=self._oracle_options(oracle["model"]),
                    )
                    reason = f"the watchdog woke the Oracle: {message}"
                    agent_id = self._adopt_resumed(oracle, bg_id)
                except sessions.ConfigParseError as exc:
                    rekey.release_resume(self.conn, oracle["agent_id"])
                    failure = self.ledger.pause_for_config(exc) if self.ledger is not None else exc
                    reason = f"the watchdog could not resume the Oracle: {failure}"
                except LedgerError as exc:
                    rekey.release_resume(self.conn, oracle["agent_id"])
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
        asks = session_wait(entry) if pending_request(oracle["transcript_path"]) else None
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
                channels=_os_only(self._channels("oracle_unreachable")),
            )
        if notice is not None:
            self._log(f"the Oracle's session runs but {state}; notified the user")
        notify.deliver(notice, log=self._log)

    def _adopt_resumed(self, agent: sqlite3.Row, bg_id: str | None) -> str:
        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET bg_id = COALESCE(?, bg_id) WHERE agent_id = ?",
                (bg_id, agent["agent_id"]),
            )
        new_id = sessions.wait_for_resumed(bg_id, agent["session_name"])
        if new_id is None:
            return agent["agent_id"]
        with write_tx(self.conn) as conn:
            rekeyed = rekey.adopt(conn, agent["agent_id"], new_id)
        if rekeyed:
            rekey.stop_earlier_sessions(self.conn, new_id)
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
                self._channels("run_paused"),
                now,
            )
        notify.deliver(notice, log=self._log)
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
        # A failed listing is unknown liveness, not "no session runs".
        if run is not None and (listing is None or self._any_running(run, listing)):
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
    notify_events: Mapping[str, bool | list[str]] | None = None,
    ledger: Ledger | None = None,
) -> None:
    dog = Watchdog(
        repo_root,
        conn,
        settings,
        exit_server=exit_server,
        activity=activity,
        notify_channels=notify_channels,
        notify_events=notify_events,
        ledger=ledger,
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=dog.run_forever, args=(stop,), name="swarm-watchdog", daemon=True
    )
    thread.start()
