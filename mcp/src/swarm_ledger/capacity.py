from __future__ import annotations

import sqlite3
from datetime import datetime

from . import memory, sessions
from .clock import parse_stamp, stamp
from .db import NOW, dict_rows, in_clause, write_tx
from .host import LedgerHost
from .identity import LedgerError
from .queries import unread_count

CHILD_ROLES = ("manager", "lead", "coder", "driver")
_PARENT_ROLES = ("manager", "lead")
_MEMBER_ROLES = ("manager", "lead", "coder")
_LIVE_CHILD_STATES = ("registered", "working")
_RESUME_WINDOW = "'-120 seconds'"
QUEUED_POINTER = (
    "the ledger queued it: free memory minus its estimate is below the headroom. The ledger "
    "starts it when memory frees, oldest first; do not call agent_spawn for it again"
)
PARK_PRESSURE = "the ledger stopped it under memory pressure while its children work"


class CapacityMixin(LedgerHost):
    def _headroom(self) -> int | None:
        return self.settings.memory.headroom_mb

    def _fits(self, free: int | None, need: int) -> bool:
        headroom = self._headroom()
        return free is None or headroom is None or free - need >= headroom

    def _queued(self, conn: sqlite3.Connection, run_id: int | None) -> list[dict]:
        return dict_rows(
            conn.execute(
                "SELECT * FROM spawn_queue WHERE run_id = ? AND state = 'queued' ORDER BY queue_id",
                (run_id,),
            )
        )

    def _queued_entry(self, child_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM spawn_queue WHERE child_name = ? AND state = 'queued'", (child_name,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _queue_reason(self, brief: sqlite3.Row) -> str | None:
        role = brief["child_role"]
        need = memory.estimate_mb(self.conn, role)
        ahead = len(self._queued(self.conn, brief["run_id"]))
        free = memory.free_mb()
        if ahead:
            return f"{ahead} earlier spawn(s) wait for memory"
        if self._fits(free, need):
            return None
        return (
            f"{free} MB free minus the {role} estimate of {need} MB is below the "
            f"{self._headroom()} MB headroom"
        )

    def _queue_spawn(self, brief: sqlite3.Row, parent_id: str, reason: str) -> dict:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO spawn_queue (run_id, brief_id, child_name, role, parent_agent_id, "
                "reason) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    brief["run_id"],
                    brief["brief_id"],
                    brief["child_name"],
                    brief["child_role"],
                    parent_id,
                    reason,
                ),
            )
            queue_id = cur.lastrowid
        return self._queued_result(
            dict(
                self.conn.execute(
                    "SELECT * FROM spawn_queue WHERE queue_id = ?", (queue_id,)
                ).fetchone()
            )
        )

    def _queued_result(self, entry: dict) -> dict:
        return {
            "queued": True,
            "queue_id": entry["queue_id"],
            "child_name": entry["child_name"],
            "role": entry["role"],
            "queued_at": entry["queued_at"],
            "reason": entry["reason"],
            "next": f"{entry['child_name']} is not started yet: {QUEUED_POINTER}.",
        }

    def start_queued_spawns(self, listing: list[dict]) -> list[dict]:
        started: list[dict] = []
        free = memory.free_mb()
        run = self.conn.execute("SELECT run_id FROM runs WHERE state = 'active'").fetchone()
        if run is None:
            return started
        for entry in self._queued(self.conn, run["run_id"]):
            brief = self.conn.execute(
                "SELECT * FROM briefs WHERE brief_id = ?", (entry["brief_id"],)
            ).fetchone()
            parent = self.conn.execute(
                "SELECT ended_at FROM agents WHERE agent_id = ?", (entry["parent_agent_id"],)
            ).fetchone()
            if brief is None or brief["acked_at"] is not None or parent is None:
                self._drop_queued(entry, "its brief or its parent is gone")
                continue
            if parent["ended_at"] is not None:
                self._drop_queued(entry, "its parent has ended")
                continue
            need = memory.estimate_mb(self.conn, entry["role"])
            if not self._fits(free, need):
                break
            try:
                self._require_spawnable(brief, entry["child_name"])
                self._require_spawn_slot(brief, listing)
            except LedgerError as exc:
                if "paused" in str(exc) or "cap" in str(exc):
                    continue
                self._drop_queued(entry, str(exc))
                continue
            try:
                spawned = self._start_child(
                    brief,
                    entry["parent_agent_id"],
                    self.session_name(entry["run_id"], entry["child_name"]),
                )
            except LedgerError as exc:
                self._drop_queued(entry, f"the start failed: {exc}")
                continue
            with write_tx(self.conn) as conn:
                conn.execute(
                    f"UPDATE spawn_queue SET state = 'started', started_at = {NOW}, "
                    "agent_id = ? WHERE queue_id = ?",
                    (spawned["agent_id"], entry["queue_id"]),
                )
            started.append(spawned)
            if free is not None:
                free -= need
        return started

    def _drop_queued(self, entry: dict, reason: str) -> None:
        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE spawn_queue SET state = 'dropped', reason = ? WHERE queue_id = ?",
                (reason, entry["queue_id"]),
            )
            parent = conn.execute(
                "SELECT name FROM agents WHERE agent_id = ? AND ended_at IS NULL",
                (entry["parent_agent_id"],),
            ).fetchone()
            if parent is not None:
                conn.execute(
                    "INSERT INTO messages (run_id, from_name, to_name, body) "
                    "VALUES (?, 'ledger', ?, ?)",
                    (
                        entry["run_id"],
                        parent["name"],
                        f"The queued spawn of {entry['child_name']} was dropped: {reason}.",
                    ),
                )

    def capacity(self, run_id: int) -> dict:
        free = memory.free_mb()
        estimates = memory.estimates(self.conn, CHILD_ROLES)
        headroom = self._headroom()
        slots: dict[str, int | None] = {}
        queued = self._queued(self.conn, run_id)
        cap = self.settings.parallelism_cap
        live = self.conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND ended_at IS NULL", (run_id,)
        ).fetchone()["n"]
        for role, need in estimates.items():
            fit: int | None = None
            if free is not None and headroom is not None:
                fit = max(0, (free - headroom) // need) if need > 0 else None
            free_cap = None if cap is None else max(0, cap - live)
            limits = [n for n in (fit, free_cap) if n is not None]
            role_cap = self.settings.role_parallelism_cap.get(role)
            if role_cap is not None:
                live_role = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND role = ? "
                    "AND ended_at IS NULL",
                    (run_id, role),
                ).fetchone()["n"]
                limits.append(max(0, role_cap - live_role))
            slots[role] = max(0, min(limits) - len(queued)) if limits else None
        parked = self.conn.execute(
            "SELECT name, park_reason FROM agents WHERE run_id = ? AND ended_at IS NULL "
            "AND parked_at IS NOT NULL ORDER BY parked_at",
            (run_id,),
        ).fetchall()
        return {
            "free_mb": free,
            "headroom_mb": headroom,
            "estimate_mb": estimates,
            "spawn_slots": slots,
            "queued_spawns": [
                {"child_name": q["child_name"], "role": q["role"], "queued_at": q["queued_at"]}
                for q in queued
            ],
            "parked": [{"name": p["name"], "reason": p["park_reason"]} for p in parked],
        }

    def sample_memory(self, listing: list[dict]) -> None:
        pids = memory.session_pids()
        rows = self.conn.execute(
            "SELECT agent_id, role FROM agents WHERE ended_at IS NULL AND parked_at IS NULL"
        ).fetchall()
        by_session = {str(e.get("sessionId")): e for e in listing if e.get("sessionId")}
        samples: list[tuple[str, int]] = []
        for row in rows:
            entry = by_session.get(row["agent_id"])
            if entry is None or not sessions.is_running(entry):
                continue
            pid = entry.get("pid") or pids.get(row["agent_id"])
            if not isinstance(pid, int) or pid <= 0:
                continue
            rss = memory.process_rss_mb(pid)
            if rss is not None and rss > 0:
                samples.append((row["role"], rss))
        if samples:
            with write_tx(self.conn) as conn:
                for role, rss in samples:
                    memory.record_sample(conn, role, rss)

    def mark_parked(self, listing: list[dict], now: datetime, grace_seconds: float) -> list[dict]:
        run = self.conn.execute("SELECT run_id FROM runs WHERE state = 'active'").fetchone()
        if run is None:
            return []
        by_session = {str(e.get("sessionId")): e for e in listing if e.get("sessionId")}
        agents = dict_rows(
            self.conn.execute(
                "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL "
                f"AND role IN ({in_clause(_MEMBER_ROLES)})",
                (run["run_id"], *_MEMBER_ROLES),
            )
        )
        parked: list[dict] = []
        for agent in agents:
            entry = by_session.get(agent["agent_id"])
            running = entry is not None and sessions.is_running(entry)
            if running:
                if agent["parked_at"] is not None:
                    with write_tx(self.conn) as conn:
                        conn.execute(
                            "UPDATE agents SET parked_at = NULL, park_reason = NULL "
                            "WHERE agent_id = ?",
                            (agent["agent_id"],),
                        )
                        self._log_event(conn, agent["agent_id"], None, "unparked", "running")
                continue
            if agent["parked_at"] is not None or self._resuming(agent):
                continue
            started = parse_stamp(agent["started_at"])
            if started is not None and (now - started).total_seconds() < grace_seconds:
                continue
            reason = memory.retire_reason(agent["bg_id"])
            if reason is not None:
                reason = f"the daemon retired its session: {reason}"
            elif entry is None:
                reason = "its session is not in claude agents --json"
            else:
                reason = f"its session is not running (state {entry.get('state')})"
            self._park(agent["agent_id"], reason, now)
            parked.append(agent | {"park_reason": reason})
        return parked

    def _resuming(self, agent: dict) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ? AND resume_started_at >= "
            f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', {_RESUME_WINDOW})",
            (agent["agent_id"],),
        ).fetchone()
        return row is not None

    def _park(self, agent_id: str, reason: str, now: datetime | None = None) -> None:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "UPDATE agents SET parked_at = COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "park_reason = ? WHERE agent_id = ? "
                "AND (parked_at IS NULL OR park_reason IS NOT ?)",
                (stamp(now) if now is not None else None, reason, agent_id, reason),
            )
            if cur.rowcount:
                self._log_event(conn, agent_id, None, "parked", reason)

    def wake_stopped_targets(self, target_ids: list[str]) -> set[str]:
        if not target_ids:
            return set()
        rows = dict_rows(
            self.conn.execute(
                f"SELECT * FROM agents WHERE agent_id IN ({in_clause(target_ids)}) "
                "AND ended_at IS NULL",
                target_ids,
            )
        )
        try:
            listing = sessions.list_sessions()
        except LedgerError:
            return {r["agent_id"] for r in rows if r["parked_at"] is not None}
        handled: set[str] = set()
        for row in rows:
            entry = sessions.find_session(listing, row["agent_id"])
            if entry is not None and sessions.is_running(entry):
                continue
            moved = sessions.resumed_entry(listing, None, row["session_name"])
            if moved is not None:
                self.adopt_resumed(row["agent_id"], str(moved["sessionId"]))
                continue
            handled.add(row["agent_id"])
            if row["role"] == "oracle" or not row["session_name"] or self._resuming(row):
                continue
            if not self._may_resume(row, listing):
                self._park(row["agent_id"], "waiting for memory to resume it for a wake-up")
                continue
            try:
                self.resume_agent(self._row(row["agent_id"]), "the ledger resumed it for a wake-up")
            except LedgerError as exc:
                with write_tx(self.conn) as conn:
                    self._log_event(conn, row["agent_id"], None, "resume_failed", str(exc))
        return handled

    def _row(self, agent_id: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()

    def _may_resume(self, agent: dict, listing: list[dict], free: int | None = None) -> bool:
        free = memory.free_mb() if free is None else free
        if self._fits(free, memory.estimate_mb(self.conn, agent["role"])):
            return True
        run_ids = {
            row["agent_id"]
            for row in self.conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND ended_at IS NULL "
                f"AND role IN ({in_clause(_MEMBER_ROLES)})",
                (agent["run_id"], *_MEMBER_ROLES),
            )
        }
        return not any(e.get("sessionId") in run_ids and sessions.is_running(e) for e in listing)

    def resume_parked(self, listing: list[dict]) -> list[dict]:
        run = self.conn.execute("SELECT run_id FROM runs WHERE state = 'active'").fetchone()
        if run is None:
            return []
        candidates = dict_rows(
            self.conn.execute(
                "SELECT a.*, MIN(w.created_at) AS owed_since, "
                "(SELECT MIN(p.ordinal) FROM phases p WHERE p.phase_id = a.phase_id) AS ordinal, "
                "(SELECT COUNT(*) FROM agents c WHERE c.parent_agent_id = a.agent_id "
                "AND c.ended_at IS NULL AND c.state IN ('registered', 'working')) AS busy "
                "FROM agents a JOIN wakeups w ON w.to_agent_id = a.agent_id "
                "WHERE a.run_id = ? AND a.ended_at IS NULL AND a.parked_at IS NOT NULL "
                "AND a.role != 'oracle' AND a.session_name IS NOT NULL AND w.sent_at IS NULL "
                "GROUP BY a.agent_id",
                (run["run_id"],),
            )
        )
        candidates.sort(key=lambda a: (a["busy"] > 0, a["ordinal"] or 0, a["owed_since"] or ""))
        resumed: list[dict] = []
        free = memory.free_mb()
        for agent in candidates:
            if self._resuming(agent):
                continue
            if not self._may_resume(agent, listing, free):
                break
            try:
                result = self.resume_agent(
                    self._row(agent["agent_id"]), "the ledger resumed it when memory freed"
                )
            except LedgerError as exc:
                with write_tx(self.conn) as conn:
                    self._log_event(conn, agent["agent_id"], None, "resume_failed", str(exc))
                continue
            resumed.append(result)
            if free is not None:
                free -= memory.estimate_mb(self.conn, agent["role"])
        return resumed

    def park_idle_parents(self, listing: list[dict]) -> list[dict]:
        if not self.settings.memory.park_idle_parents:
            return []
        run = self.conn.execute("SELECT run_id FROM runs WHERE state = 'active'").fetchone()
        headroom = self._headroom()
        free = memory.free_mb()
        if run is None or headroom is None or free is None:
            return []
        queued = self._queued(self.conn, run["run_id"])
        need = headroom + sum(memory.estimate_mb(self.conn, q["role"]) for q in queued) - free
        if need <= 0:
            return []
        by_session = {str(e.get("sessionId")): e for e in listing if e.get("sessionId")}
        parents = dict_rows(
            self.conn.execute(
                "SELECT a.* FROM agents a WHERE a.run_id = ? AND a.ended_at IS NULL "
                "AND a.parked_at IS NULL AND a.state = 'idle' AND a.bg_id IS NOT NULL "
                f"AND a.role IN ({in_clause(_PARENT_ROLES)}) "
                "AND EXISTS (SELECT 1 FROM agents c WHERE c.parent_agent_id = a.agent_id "
                f"AND c.ended_at IS NULL AND c.state IN ({in_clause(_LIVE_CHILD_STATES)})) "
                "ORDER BY CASE a.role WHEN 'lead' THEN 0 ELSE 1 END, a.last_heartbeat_at",
                (run["run_id"], *_PARENT_ROLES, *_LIVE_CHILD_STATES),
            )
        )
        parked: list[dict] = []
        for parent in parents:
            if need <= 0:
                break
            entry = by_session.get(parent["agent_id"])
            if entry is None or not sessions.is_running(entry):
                continue
            if not self._claim_park(parent):
                continue
            try:
                sessions.stop(parent["bg_id"])
            except LedgerError as exc:
                with write_tx(self.conn) as conn:
                    conn.execute(
                        "UPDATE agents SET parked_at = NULL, park_reason = NULL WHERE agent_id = ?",
                        (parent["agent_id"],),
                    )
                    self._log_event(conn, parent["agent_id"], None, "park_failed", str(exc))
                continue
            parked.append(parent)
            need -= memory.estimate_mb(self.conn, parent["role"])
        return parked

    def _claim_park(self, parent: dict) -> bool:
        with write_tx(self.conn) as conn:
            owed = conn.execute(
                "SELECT 1 FROM wakeups WHERE to_agent_id = ? AND sent_at IS NULL",
                (parent["agent_id"],),
            ).fetchone()
            if owed is not None or unread_count(conn, parent["run_id"], parent["name"]):
                return False
            cur = conn.execute(
                f"UPDATE agents SET parked_at = {NOW}, park_reason = ? WHERE agent_id = ? "
                "AND parked_at IS NULL AND state = 'idle'",
                (PARK_PRESSURE, parent["agent_id"]),
            )
            if cur.rowcount:
                self._log_event(conn, parent["agent_id"], "idle", "parked", PARK_PRESSURE)
            return bool(cur.rowcount)
