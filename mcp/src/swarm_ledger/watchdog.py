from __future__ import annotations

import json
import sqlite3
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify, sessions, wake
from .clock import parse_stamp, stamp, utcnow
from .db import write_tx
from .identity import LedgerError
from .settings import NOTIFY_CHANNELS, WatchdogSettings

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
# The Driver checks in every 30 minutes by design (knowledge/prd/02-run-lifecycle.md); the watchdog
# adds a grace period before it reports one as overdue.
DRIVER_CHECKIN_INTERVAL = timedelta(minutes=30)
DRIVER_CHECKIN_GRACE = timedelta(minutes=5)
DRIVER_ERRORS = {"crashed": ("crashed", "error"), "driver_overdue": ("overdue", "warning")}


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
            "AND role IN ('oracle', 'manager', 'lead', 'coder', 'driver') "
            "ORDER BY started_at, agent_id",
            (run["run_id"],),
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
        elif agent["role"] == "driver":
            findings += _driver_findings(conn, agent, entry, now)
        context = _context_high(agent, settings)
        if context is not None:
            findings.append(_finding(agent, "context_high", context, _context_step(agent, names)))

    stalled = _stalled(conn, run, agents, by_session, now)
    if stalled is not None:
        findings.append(stalled)
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


def _session_findings(
    agent: dict, entry: dict | None, now: datetime, settings: WatchdogSettings
) -> list[Finding]:
    name = json.dumps(agent["name"])
    if entry is not None and "permission" in str(entry.get("waitingFor") or "").lower():
        return [
            _finding(
                agent,
                "waiting_permission",
                "its session waits on a permission prompt",
                f"Tell the user to open the session {agent['session_name'] or agent['name']} "
                "in agent view and answer its permission prompt.",
            )
        ]

    state = agent["state"]
    running = entry is not None and sessions.is_running(entry)
    if not running:
        started = parse_stamp(agent["started_at"])
        overdue = started is not None and now - started > REGISTER_GRACE
        if state == "working" or (state == "registered" and overdue):
            where = (
                "is not in claude agents --json"
                if entry is None
                else f"is not running (status {entry.get('status')}, state {entry.get('state')})"
            )
            return [
                _finding(
                    agent,
                    "crashed",
                    f"the ledger says {state}, but its session {where}",
                    f"Resume it with agent_resume(target_name={name}).",
                )
            ]
        return []

    if state != "working":
        return []
    last = parse_stamp(agent["last_heartbeat_at"]) or parse_stamp(agent["started_at"])
    if last is None or now - last < timedelta(minutes=settings.stuck_minutes):
        return []
    minutes = int((now - last).total_seconds() // 60)
    activity = agent["current_activity"] or "none recorded"
    label = json.dumps(agent["session_name"] or agent["name"])
    return [
        _finding(
            agent,
            "stuck",
            f"its session runs, but it has shown no activity for {minutes} minutes "
            f"(last activity: {activity})",
            f"Message it with SendMessage(to={label}) and ask what blocks it, or have its "
            "parent replace it with a fresh agent that continues from the ledger. "
            "agent_resume refuses a running session.",
        )
    ]


def _driver_findings(
    conn: sqlite3.Connection, agent: dict, entry: dict | None, now: datetime
) -> list[Finding]:
    name = json.dumps(agent["name"])
    running = entry is not None and sessions.is_running(entry)
    if not running:
        where = (
            "is not in claude agents --json"
            if entry is None
            else f"is not running (status {entry.get('status')}, state {entry.get('state')})"
        )
        closed = _closed_exploration(conn, agent["agent_id"])
        if closed is not None:
            return [
                _finding(
                    agent,
                    "crashed",
                    f"its exploration {closed['request_id']} ended ({closed['state']}), but its "
                    f"session {where} and the ledger has not released it",
                    "Its result is in the ledger: read directive_inbox() and status_tree(). "
                    f"Then release it with agent_release(target_agent_id="
                    f"{json.dumps(agent['agent_id'])}).",
                )
            ]
        started = parse_stamp(agent["started_at"])
        overdue = started is not None and now - started > REGISTER_GRACE
        if agent["state"] == "working" or (agent["state"] == "registered" and overdue):
            return [
                _finding(
                    agent,
                    "crashed",
                    f"the ledger says {agent['state']}, but its session {where}",
                    f"Resume it with agent_resume(target_name={name}).",
                )
            ]
        return []

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
    minutes = int((now - last).total_seconds() // 60)
    label = json.dumps(agent["session_name"] or agent["name"])
    return [
        _finding(
            agent,
            "driver_overdue",
            f"exploration {request['request_id']} has had no check-in for {minutes} minutes",
            f"Message it with SendMessage(to={label}) and ask what blocks it, or stop it and "
            "start a fix for whatever blocked it. agent_resume refuses a running session.",
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
    minutes = int((now - owed).total_seconds() // 60)
    pointer = " ".join(str(wakeup["pointer"]).split())
    return [
        _finding(
            agent,
            "driver_unsent",
            f"its exploration {closed['request_id']} ended ({closed['state']}), its session "
            f"runs but sits idle, and it has owed {wakeup['to_name']} an unsent wake-up for "
            f"{minutes} minutes: {pointer}",
            "Its result is in the ledger: read directive_inbox() and status_tree(). "
            f"Then release it with agent_release(target_agent_id="
            f"{json.dumps(agent['agent_id'])}).",
        )
    ]


def _failed(row: sqlite3.Row) -> bool:
    exit_code = row["exit_code"]
    return (exit_code is None or exit_code != 0) or (row["failed"] or 0) > 0


def _spinning(conn: sqlite3.Connection, agent: dict, settings: WatchdogSettings) -> str | None:
    limit = settings.spin_failures
    if limit <= 0:
        return None
    groups: dict[tuple[str, str | None], list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT test_run_id, scope, target, exit_code, failed FROM test_runs "
        "WHERE agent_id = ? ORDER BY test_run_id DESC",
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
    handoffs = conn.execute(
        "SELECT h.handoff_id, f.path, coder.parent_agent_id AS reviewer_id FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "LEFT JOIN agents coder ON coder.agent_id = h.agent_id "
        "WHERE p.run_id = ? AND h.state = 'submitted' AND p.paused_at IS NULL "
        "ORDER BY h.handoff_id",
        (run_id,),
    ).fetchall()
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
        unread = conn.execute(
            "SELECT 1 FROM messages WHERE run_id = ? AND to_name = ? AND read_at IS NULL",
            (run_id, agent["name"]),
        ).fetchone()
        if unread is not None and agent["name"] not in targets:
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
        where = (
            f"no agent of the run has worked for {int(IDLE_STALL.total_seconds() // 60)} minutes"
        )
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
    ) -> None:
        self.repo_root = repo_root
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
        run = self._live_run()
        try:
            listing: list[dict] | None = sessions.list_sessions()
        except LedgerError as exc:
            self._log(f"skipped a pass: {exc}")
            listing = None
        if run is not None and run["state"] == "active" and listing is not None:
            findings = scan(self.conn, listing, now, self.settings)
            reported = record(self.conn, run["run_id"], findings, now)
            for row in reported:
                self._log(
                    f"reported {row['kind']} for {row['agent_id']} "
                    f"as directive {row['directive_id']}"
                )
            for notice in driver_notices(self.conn, run["run_id"], reported, self.notify_channels):
                notify.deliver(notice, self.notify_channels, log=self._log)
            self._wake_oracle(run, listing, now)
        self._check_idle(run, listing, now)

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.wait(self.settings.interval_seconds):
            try:
                self.tick(utcnow())
            except Exception as exc:
                self._log(f"a pass failed: {type(exc).__name__}: {exc}")

    def _live_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE state IN ('active', 'paused') ORDER BY run_id DESC LIMIT 1"
        ).fetchone()

    def _oracle(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()

    def _wake_oracle(self, run: sqlite3.Row, listing: list[dict], now: datetime) -> None:
        oracle = self._oracle(run["run_id"])
        if oracle is None:
            return
        entry = sessions.find_session(listing, oracle["agent_id"])
        if entry is not None and sessions.is_running(entry):
            return
        waiting = self.conn.execute(
            "SELECT COUNT(*) AS n FROM directives WHERE run_id = ? AND source = 'watchdog' "
            "AND state = 'open' AND notified_at IS NULL AND created_at <= ?",
            (run["run_id"], stamp(now - self.interval)),
        ).fetchone()["n"]
        if not waiting:
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
        if not oracle["session_name"]:
            # A resume without a recorded session starts a fresh session named after the message.
            reason = (
                f"the watchdog cannot wake the Oracle: no session is recorded for "
                f"{oracle['agent_id']!r}"
            )
        else:
            try:
                sessions.resume(
                    oracle["agent_id"],
                    message,
                    cwd=self.repo_root,
                    name=oracle["session_name"],
                    options=self._oracle_options(oracle["model"]),
                )
                reason = f"the watchdog woke the Oracle: {message}"
            except LedgerError as exc:
                reason = f"the watchdog could not resume the Oracle: {exc}"
        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (oracle["agent_id"], oracle["state"], WAKE_STATE, reason, stamp(now)),
            )
        self._log(reason)

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
            )
        except LedgerError:
            return []

    def _pause(self, run: sqlite3.Row, oracle: sqlite3.Row, now: datetime) -> None:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "UPDATE runs SET state = 'paused' WHERE run_id = ? AND state = 'active'",
                (run["run_id"],),
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO agent_events (agent_id, from_state, to_state, reason, at) "
                    "VALUES (?, 'active', 'paused', ?, ?)",
                    (oracle["agent_id"], PAUSE_REASON, stamp(now)),
                )
        self._log(f"paused run {run['run_id']}: {PAUSE_REASON}")

    def _check_idle(
        self, run: sqlite3.Row | None, listing: list[dict] | None, now: datetime
    ) -> None:
        activity = self._activity()
        if activity != self._activity_seen:
            self._activity_seen = activity
            self._idle_since = None
            return
        if run is not None and run["state"] == "active":
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
) -> threading.Event:
    dog = Watchdog(
        repo_root,
        conn,
        settings,
        exit_server=exit_server,
        activity=activity,
        notify_channels=notify_channels,
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=dog.run_forever, args=(stop,), name="swarm-watchdog", daemon=True
    )
    thread.start()
    return stop
