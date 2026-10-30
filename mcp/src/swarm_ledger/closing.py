from __future__ import annotations

import json
import sqlite3

from . import lock, sessions, settle
from .agreements import test_run_passed
from .db import NOW, in_clause, row_dict, write_tx
from .host import LedgerHost
from .identity import Caller, LedgerError, require_role, resolve
from .queries import live_run

_FILE_INCOMPLETE_KEYS = ("file_id", "reason")


class ClosingMixin(LedgerHost):
    def _close_incomplete(
        self,
        conn: sqlite3.Connection,
        file_row: sqlite3.Row,
        reason: str,
        decided_by: str | None,
        evidence: list[int] | None = None,
    ) -> int | None:
        file_id = file_row["file_id"]
        conn.execute(
            f"UPDATE files SET state = 'incomplete', released_at = COALESCE(released_at, {NOW}) "
            "WHERE file_id = ?",
            (file_id,),
        )
        settle.sync_products(conn, file_id, "incomplete", released="set")
        conn.execute(
            f"UPDATE handoffs SET state = 'incomplete', decided_at = {NOW}, decided_by = ?, "
            "decided_notes = ? WHERE file_id = ? AND state = 'submitted'",
            (decided_by, reason, file_id),
        )
        for agent in conn.execute(
            "SELECT agent_id FROM agents WHERE file_id = ? AND ended_at IS NULL", (file_id,)
        ).fetchall():
            self._release_agent(conn, agent["agent_id"], "accepted incomplete")
        run_id = conn.execute(
            "SELECT p.run_id FROM modules m JOIN phases p ON p.phase_id = m.phase_id "
            "WHERE m.module_id = ?",
            (file_row["module_id"],),
        ).fetchone()["run_id"]
        cur = conn.execute(
            "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, kind, evidence_json, "
            f"state, decided_by, decision_reason, decided_at) VALUES (?, ?, ?, ?, 'file', ?, "
            f"'agreed', ?, ?, {NOW})",
            (
                run_id,
                file_id,
                decided_by,
                reason,
                json.dumps(evidence) if evidence is not None else None,
                decided_by,
                reason,
            ),
        )
        if not settle.unsettled_files(conn, self.repo_root, file_row["module_id"]):
            conn.execute(
                "UPDATE modules SET state = 'approved' WHERE module_id = ?",
                (file_row["module_id"],),
            )
        return cur.lastrowid

    def file_accept_incomplete(
        self, caller: str, agent_id: str, file_id: int, reason: str, evidence: list[int]
    ) -> dict:
        if not reason.strip():
            raise LedgerError("file_accept_incomplete needs a reason: why the file stops short")
        if not evidence:
            raise LedgerError(
                "file_accept_incomplete needs evidence: the ids of passing test runs that "
                "cover the file"
            )
        woken: list[dict] = []
        with self._release_tx() as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            file_row = self._run_file(conn, c, file_id)
            if settle.file_settled(conn, self.repo_root, file_row):
                raise LedgerError(f"file {file_id} ({file_row['path']}) is already settled")
            runs = {
                row["test_run_id"]: row
                for row in conn.execute(
                    f"SELECT * FROM test_runs WHERE run_id = ? AND test_run_id IN "
                    f"({in_clause(evidence)})",
                    (c.run_id, *evidence),
                )
            }
            missing = [run_id for run_id in evidence if run_id not in runs]
            if missing:
                raise LedgerError(f"test run(s) {missing} are not test runs of this run")
            failing = [run_id for run_id in evidence if not test_run_passed(runs[run_id])]
            if failing:
                raise LedgerError(f"test run(s) {failing} did not pass, so they cover nothing")
            deferral_id = self._close_incomplete(conn, file_row, reason, c.agent_id, evidence)
            woken = self._wake_file_waiters(conn, c, file_row["path"], "incomplete")

        result = row_dict(self.conn, "files", "file_id", file_id)
        result["deferral"] = row_dict(self.conn, "deferrals", "deferral_id", deferral_id)
        if woken:
            result["woken_waiters"] = [
                {"name": w["to_name"], "next": self.next_step(w)} for w in woken
            ]
        return result

    @staticmethod
    def _run_file(conn: sqlite3.Connection, c: Caller, file_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
            "JOIN phases p ON p.phase_id = m.phase_id WHERE f.file_id = ? AND p.run_id = ?",
            (file_id, c.run_id),
        ).fetchone()
        if row is None:
            raise LedgerError(f"no file {file_id!r} in this run")
        return row

    def run_close(
        self,
        caller: str,
        agent_id: str,
        outcome: str,
        files_incomplete: list[dict] | None = None,
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        return self.close_run(outcome, files_incomplete or [], closed_by="oracle", oracle=c)

    def close_run(
        self,
        outcome: str,
        files_incomplete: list[dict],
        *,
        closed_by: str,
        oracle: Caller | None = None,
    ) -> dict:
        if not outcome.strip():
            raise LedgerError("run_close needs an outcome: what the run delivered")
        reasons = _incomplete_reasons(files_incomplete)
        decided_by = oracle.agent_id if oracle is not None else None
        with self._release_tx() as conn:
            run = live_run(conn)
            if run is None:
                raise LedgerError("no active or paused run to close")
            run_id = run["run_id"]
            modules = [
                row["module_id"]
                for row in conn.execute(
                    "SELECT m.module_id FROM modules m JOIN phases p ON p.phase_id = m.phase_id "
                    "WHERE p.run_id = ? ORDER BY m.module_id",
                    (run_id,),
                )
            ]
            open_files = {
                f["file_id"]: f
                for module_id in modules
                for f in settle.unsettled_files(conn, self.repo_root, module_id)
                if f["source_file_id"] is None
            }
            unnamed = [f"{i} ({f['path']})" for i, f in open_files.items() if i not in reasons]
            if unnamed:
                raise LedgerError(
                    f"file(s) {', '.join(unnamed)} are not settled; name each in "
                    "files_incomplete with the reason it stops short"
                )
            settled = [i for i in reasons if i not in open_files]
            if settled:
                raise LedgerError(
                    f"file(s) {settled} are settled or not files of this run; leave them out"
                )
            for module_id in modules:
                settle.close_settled(conn, self.repo_root, module_id)
            for file_id, reason in reasons.items():
                row = conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
                self._close_incomplete(conn, row, reason, decided_by)
            conn.execute(
                "UPDATE spawn_queue SET state = 'dropped', reason = 'run_close' "
                "WHERE run_id = ? AND state = 'queued'",
                (run_id,),
            )
            conn.execute(
                f"UPDATE runs SET state = 'closed', outcome = ?, ended_at = {NOW}, "
                "closed_by = ? WHERE run_id = ?",
                (outcome, closed_by, run_id),
            )
            for row in conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND role != 'oracle' "
                "AND ended_at IS NULL",
                (run_id,),
            ).fetchall():
                self._release_agent(conn, row["agent_id"], "run_close")
            oracles = conn.execute(
                "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL",
                (run_id,),
            ).fetchall()

        report = self.write_report(run_id)
        with write_tx(self.conn) as conn:
            for row in oracles:
                conn.execute(
                    f"UPDATE agents SET state = 'released', ended_at = {NOW}, "
                    "end_reason = 'run_close' WHERE agent_id = ?",
                    (row["agent_id"],),
                )
                self._log_event(conn, row["agent_id"], row["state"], "released", "run_close")
        stopped = [] if oracle is not None else _stop_sessions(oracles)
        lock.release(self.repo_root, run_id)

        result = row_dict(self.conn, "runs", "run_id", run_id)
        result["report_path"] = report["path"]
        result["files_incomplete"] = sorted(reasons)
        if stopped:
            result["oracle_sessions"] = stopped
        return result


def _incomplete_reasons(files_incomplete: list[dict]) -> dict[int, str]:
    reasons: dict[int, str] = {}
    for item in files_incomplete:
        if not isinstance(item, dict) or set(item) != set(_FILE_INCOMPLETE_KEYS):
            raise LedgerError(
                "each files_incomplete entry is {file_id, reason}, such as "
                '{"file_id": 20, "reason": "binary; covered by its export tests"}'
            )
        reason = str(item["reason"]).strip()
        if not reason:
            raise LedgerError(f"file {item['file_id']} needs a non-empty reason")
        reasons[int(item["file_id"])] = reason
    return reasons


def _stop_sessions(oracles: list[sqlite3.Row]) -> list[str]:
    try:
        listing = sessions.list_sessions()
    except LedgerError as exc:
        return [f"Oracle sessions not checked: {exc}"]
    stopped: list[str] = []
    for row in oracles:
        entry = sessions.find_session(listing, row["agent_id"])
        if entry is None or not sessions.is_running(entry):
            continue
        if entry.get("kind") != "background" or not entry.get("id"):
            stopped.append(f"{row['name']}: interactive session, left running")
            continue
        try:
            sessions.stop(str(entry["id"]))
            stopped.append(f"{row['name']}: session {entry['id']} stopped")
        except LedgerError as exc:
            stopped.append(f"{row['name']}: session {entry['id']} not stopped: {exc}")
    return stopped
