from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Literal

from .db import write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .review import _ROLE_RANK
from .settings import Settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_CR_BLOCKING_STATES = ("open", "accepted", "completed")
_PENDING_STATES = ("open", "lead_agreed", "manager_agreed")
_UNSETTLED_STATES = (*_PENDING_STATES, "pushed_back")
_FIRST_LEVEL = {"coder": "lead", "lead": "manager", "manager": "oracle"}
_AGREED_STATE = {"lead": "lead_agreed", "manager": "manager_agreed", "oracle": "signed_off"}
_NEXT_LEVEL = {"lead": "manager", "manager": "oracle", "oracle": None}
_ROLE_TITLE = {"lead": "Lead", "manager": "Manager", "oracle": "Oracle"}


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _caller_of(agent: sqlite3.Row) -> Caller:
    return Caller(
        agent_id=agent["agent_id"],
        name=agent["name"],
        role=agent["role"],
        run_id=agent["run_id"],
        phase_id=agent["phase_id"],
        module_id=agent["module_id"],
        file_id=agent["file_id"],
        parent_agent_id=agent["parent_agent_id"],
    )


def _test_run_passed(row: sqlite3.Row) -> bool:
    return (
        row["exit_code"] == 0
        and (row["failed"] or 0) == 0
        and (row["skipped"] or 0) == 0
        and (row["passed"] or 0) >= 1
    )


class AgreementsMixin:
    """Change requests, departures, and shortfalls: the agreements a non-owner and
    an owner reach about work neither can change alone.

    Split out of `ledger.py` and `review.py` to keep those files small; every
    method here still assumes it is mixed into `Ledger` and relies on `self.conn`,
    `self.settings`, `self._log_event`, `self._owe_wakeup`, and `self.next_step`.
    """

    conn: sqlite3.Connection
    settings: Settings

    def _log_event(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        from_state: str | None,
        to_state: str,
        reason: str | None = None,
    ) -> None: ...

    def _release_tx(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def _owe_wakeup(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        to_agent_id: str | None,
        reason: str,
        pointer: str,
    ) -> dict | None: ...

    def next_step(self, wakeup: dict | None) -> str | None: ...

    # -- Shared helpers -----------------------------------------------------

    def nearest_live_agent(self, conn: sqlite3.Connection, agent_id: str | None) -> dict | None:
        while agent_id is not None:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                return None
            if row["ended_at"] is None:
                return dict(row)
            agent_id = row["parent_agent_id"]
        return None

    def _cr_recipient(
        self, conn: sqlite3.Connection, c: Caller, file_row: sqlite3.Row
    ) -> sqlite3.Row:
        owner_name = file_row["owner_agent_id"]
        if owner_name:
            owner = conn.execute(
                "SELECT * FROM agents WHERE run_id = ? AND name = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (c.run_id, owner_name),
            ).fetchone()
            if owner is not None:
                return owner
        lead = conn.execute(
            "SELECT * FROM agents WHERE module_id = ? AND role = 'lead' AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (file_row["module_id"],),
        ).fetchone()
        if lead is not None:
            return lead
        module_row = conn.execute(
            "SELECT phase_id FROM modules WHERE module_id = ?", (file_row["module_id"],)
        ).fetchone()
        phase_id = module_row["phase_id"] if module_row is not None else None
        if phase_id is not None:
            manager = conn.execute(
                "SELECT * FROM agents WHERE phase_id = ? AND role = 'manager' AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (phase_id,),
            ).fetchone()
            if manager is not None:
                return manager
        oracle = conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (c.run_id,),
        ).fetchone()
        if oracle is None:
            raise LedgerError("no live Oracle for this run")
        return oracle

    # -- Gates called from review.py and ledger.py ---------------------------

    def _block_handoff_for_open_cr(self, conn: sqlite3.Connection, file_id: int) -> None:
        rows = conn.execute(
            "SELECT cr.cr_id FROM change_requests cr JOIN agents a ON a.agent_id = cr.to_agent_id "
            "WHERE cr.file_id = ? AND cr.state = 'accepted' AND a.role = 'coder'",
            (file_id,),
        ).fetchall()
        if rows:
            ids = [r["cr_id"] for r in rows]
            raise LedgerError(
                f"change request(s) {ids} are accepted and not completed on this file; "
                "call cr_complete first"
            )

    def _block_approve_for_cr(self, conn: sqlite3.Connection, file_id: int) -> None:
        placeholders = ",".join("?" for _ in _CR_BLOCKING_STATES)
        rows = conn.execute(
            f"SELECT cr_id FROM change_requests WHERE file_id = ? AND state IN ({placeholders})",
            (file_id, *_CR_BLOCKING_STATES),
        ).fetchall()
        if rows:
            ids = [r["cr_id"] for r in rows]
            raise LedgerError(f"change request(s) {ids} on this file are not verified")

    def _block_approve_for_departures(self, conn: sqlite3.Connection, handoff_id: int) -> None:
        self._block_return_for_open_departures(conn, handoff_id)
        pushed_rows = conn.execute(
            "SELECT departure_id FROM departures WHERE handoff_id = ? AND state = 'pushed_back'",
            (handoff_id,),
        ).fetchall()
        if pushed_rows:
            ids = [r["departure_id"] for r in pushed_rows]
            raise LedgerError(
                f"departure(s) {ids} are pushed back; a pushed-back departure is a return: "
                "call return_work"
            )

    def _block_return_for_open_departures(self, conn: sqlite3.Connection, handoff_id: int) -> None:
        open_rows = conn.execute(
            "SELECT departure_id FROM departures WHERE handoff_id = ? AND state = 'open' "
            "AND level = 'lead'",
            (handoff_id,),
        ).fetchall()
        if open_rows:
            ids = [r["departure_id"] for r in open_rows]
            raise LedgerError(f"departure(s) {ids} are open; decide each with departure_decide")

    def _mark_departures_reworked(
        self, conn: sqlite3.Connection, file_id: int, handoff_id: int
    ) -> None:
        conn.execute(
            f"UPDATE departures SET state = 'reworked', reworked_at = {_NOW}, "
            "reworked_by_handoff_id = ? WHERE file_id = ? AND kind = 'departure' "
            "AND state = 'pushed_back'",
            (handoff_id, file_id),
        )

    def _block_module_review_for_departures(self, conn: sqlite3.Connection, module_id: int) -> None:
        rows = [
            row
            for row in self._blocking_departures(conn, _UNSETTLED_STATES, module_id=module_id)
            if row["state"] == "pushed_back" or row["level"] in ("lead", "manager")
        ]
        if rows:
            raise LedgerError(
                f"departure(s) {[r['departure_id'] for r in rows]} in the module are not decided "
                f"by the Manager yet: {self._describe_waits(conn, rows)}"
            )

    def _block_phase_review_for_departures(self, conn: sqlite3.Connection, phase_id: int) -> None:
        rows = self._blocking_departures(conn, _UNSETTLED_STATES, phase_id=phase_id)
        if rows:
            raise LedgerError(
                f"departure(s) {[r['departure_id'] for r in rows]} in the phase are not signed "
                f"off by the Oracle or reworked: {self._describe_waits(conn, rows)}"
            )

    def _block_run_finish_for_departures(self, conn: sqlite3.Connection, run_id: int) -> None:
        rows = self._blocking_departures(conn, _UNSETTLED_STATES, run_id=run_id)
        if rows:
            raise LedgerError(
                f"departure(s) {[r['departure_id'] for r in rows]} are not signed off or "
                f"reworked: {self._describe_waits(conn, rows)}"
            )

    def _block_phase_approval_for_cr(self, conn: sqlite3.Connection, phase_id: int) -> None:
        placeholders = ",".join("?" for _ in _CR_BLOCKING_STATES)
        rows = conn.execute(
            "SELECT cr.cr_id FROM change_requests cr JOIN files f ON f.file_id = cr.file_id "
            "JOIN modules m ON m.module_id = f.module_id "
            f"WHERE m.phase_id = ? AND cr.state IN ({placeholders})",
            (phase_id, *_CR_BLOCKING_STATES),
        ).fetchall()
        if rows:
            ids = [r["cr_id"] for r in rows]
            raise LedgerError(f"change request(s) {ids} in the phase are not verified")

    def _block_run_finish_for_cr(self, conn: sqlite3.Connection, run_id: int) -> None:
        placeholders = ",".join("?" for _ in _CR_BLOCKING_STATES)
        rows = conn.execute(
            f"SELECT cr_id FROM change_requests WHERE run_id = ? AND state IN ({placeholders})",
            (run_id, *_CR_BLOCKING_STATES),
        ).fetchall()
        if rows:
            ids = [r["cr_id"] for r in rows]
            raise LedgerError(f"change request(s) {ids} are not verified")

    # -- Change requests ------------------------------------------------------

    def cr_open(self, caller: str, agent_id: str, path: str, body: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            file_row = conn.execute(
                "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ? AND f.path = ? "
                "ORDER BY f.file_id DESC LIMIT 1",
                (c.run_id, path),
            ).fetchone()
            if file_row is None:
                raise LedgerError(f"no module plans {path!r}; ask your parent to plan it")
            recipient = self._cr_recipient(conn, c, file_row)
            if recipient["agent_id"] == c.agent_id:
                raise LedgerError(f"{caller!r} already owns {path!r}; edit it directly")

            cur = conn.execute(
                "INSERT INTO change_requests (run_id, from_agent_id, to_agent_id, file_id, "
                "path, state, body) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (c.run_id, c.agent_id, recipient["agent_id"], file_row["file_id"], path, body),
            )
            cr_id = cur.lastrowid
            wakeup = self._owe_wakeup(
                conn,
                c,
                recipient["agent_id"],
                "cr_open",
                f"Change request {cr_id} for {path} is waiting in the ledger; "
                "read it with cr_list.",
            )

        row = dict(
            self.conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
        )
        row["next"] = self.next_step(wakeup)
        return row

    def cr_accept(self, caller: str, agent_id: str, cr_id: int, accept: bool, reason: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown cr_id {cr_id!r}")
            if row["to_agent_id"] != c.agent_id:
                raise LedgerError(f"{caller!r} is not the recipient of change request {cr_id}")
            if row["state"] != "open":
                raise LedgerError(f"change request {cr_id} is {row['state']}, not open")
            if not accept and not reason.strip():
                raise LedgerError("a decline needs a non-empty reason")

            new_state = "accepted" if accept else "declined"
            if accept:
                conn.execute(
                    f"UPDATE change_requests SET state = ?, decision_reason = ?, "
                    f"accepted_at = {_NOW}, decided_at = {_NOW} WHERE cr_id = ?",
                    (new_state, reason, cr_id),
                )
            else:
                conn.execute(
                    f"UPDATE change_requests SET state = ?, decision_reason = ?, "
                    f"decided_at = {_NOW} WHERE cr_id = ?",
                    (new_state, reason, cr_id),
                )
            wakeup = self._owe_wakeup(
                conn,
                c,
                row["from_agent_id"],
                "cr_accept",
                f"Change request {cr_id} for {row['path']} was {new_state}; read it with cr_list.",
            )

        result = dict(
            self.conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        return result

    def _cr_targets(self, conn: sqlite3.Connection, cr_row: sqlite3.Row) -> tuple[str, ...]:
        # A Coder's evidence is normally the run of its own test file, whose target is the
        # test_path, not the CR's source path; accept either.
        targets = {cr_row["path"]}
        file_row = conn.execute(
            "SELECT test_path FROM files WHERE file_id = ?", (cr_row["file_id"],)
        ).fetchone()
        if file_row is not None and file_row["test_path"]:
            targets.add(file_row["test_path"])
        return tuple(targets)

    def _cr_evidence(
        self, conn: sqlite3.Connection, cr_row: sqlite3.Row, recipient_role: str, c: Caller
    ) -> int:
        targets = self._cr_targets(conn, cr_row)
        placeholders = ",".join("?" for _ in targets)

        if recipient_role == "coder":
            candidates = conn.execute(
                f"SELECT * FROM test_runs WHERE scope = 'file' AND target IN ({placeholders}) "
                "AND agent_id = ? AND created_at > ? ORDER BY test_run_id DESC",
                (*targets, c.agent_id, cr_row["accepted_at"]),
            ).fetchall()
            for candidate in candidates:
                if _test_run_passed(candidate):
                    return candidate["test_run_id"]
            raise LedgerError(
                f"no passing test run of {cr_row['path']} by {c.name} since acceptance; "
                "call tests_run(scope='file') first"
            )

        candidates = conn.execute(
            f"SELECT * FROM test_runs WHERE scope = 'file' AND target IN ({placeholders}) "
            "AND created_at > ? ORDER BY test_run_id DESC",
            (*targets, cr_row["accepted_at"]),
        ).fetchall()
        for candidate in candidates:
            if _test_run_passed(candidate):
                return candidate["test_run_id"]
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE file_id = ? AND state = 'approved' AND decided_at > ? "
            "ORDER BY handoff_id DESC LIMIT 1",
            (cr_row["file_id"], cr_row["accepted_at"]),
        ).fetchone()
        if handoff is not None and handoff["test_run_id"] is not None:
            return handoff["test_run_id"]
        raise LedgerError(
            f"no passing test run or approved handoff for {cr_row['path']} since acceptance"
        )

    def cr_complete(self, caller: str, agent_id: str, cr_id: int, notes: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown cr_id {cr_id!r}")
            if row["to_agent_id"] != c.agent_id:
                raise LedgerError(f"{caller!r} is not the recipient of change request {cr_id}")
            if row["state"] != "accepted":
                raise LedgerError(f"change request {cr_id} is {row['state']}, not accepted")

            evidence_id = self._cr_evidence(conn, row, c.role, c)
            conn.execute(
                f"UPDATE change_requests SET state = 'completed', completed_at = {_NOW}, "
                "completion_notes = ?, evidence_test_run_id = ? WHERE cr_id = ?",
                (notes, evidence_id, cr_id),
            )
            wakeup = self._owe_wakeup(
                conn,
                c,
                row["from_agent_id"],
                "cr_complete",
                f"Change request {cr_id} for {row['path']} is completed; verify it with cr_verify.",
            )

        result = dict(
            self.conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        return result

    def cr_verify(self, caller: str, agent_id: str, cr_id: int, ok: bool, notes: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown cr_id {cr_id!r}")
            if row["state"] != "completed":
                raise LedgerError(f"change request {cr_id} is {row['state']}, not completed")
            verifier = self.nearest_live_agent(conn, row["from_agent_id"])
            if verifier is None or verifier["agent_id"] != c.agent_id:
                expected = verifier["name"] if verifier is not None else "nobody live"
                raise LedgerError(
                    f"only the requester or its nearest live ancestor may verify change "
                    f"request {cr_id}; expected {expected!r}"
                )

            wakeup = None
            if ok:
                conn.execute(
                    f"UPDATE change_requests SET state = 'verified', verified_at = {_NOW}, "
                    "verify_notes = ? WHERE cr_id = ?",
                    (notes, cr_id),
                )
            else:
                conn.execute(
                    "UPDATE change_requests SET state = 'accepted', verify_notes = ? "
                    "WHERE cr_id = ?",
                    (notes, cr_id),
                )
                wakeup = self._owe_wakeup(
                    conn,
                    c,
                    row["to_agent_id"],
                    "cr_verify",
                    f"Change request {cr_id} for {row['path']} failed verification; "
                    "read the notes with cr_list and fix it.",
                )

        result = dict(
            self.conn.execute("SELECT * FROM change_requests WHERE cr_id = ?", (cr_id,)).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        return result

    def cr_list(self, caller: str, agent_id: str, state: str | None = None) -> list[dict]:
        c = resolve(self.conn, caller, agent_id)
        if c.role == "oracle":
            query = "SELECT * FROM change_requests WHERE run_id = ?"
            params: list[object] = [c.run_id]
        else:
            query = (
                "SELECT * FROM change_requests WHERE run_id = ? "
                "AND (from_agent_id = ? OR to_agent_id = ?)"
            )
            params = [c.run_id, c.agent_id, c.agent_id]
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY cr_id"
        return _rows(self.conn.execute(query, params))

    # -- Departures and shortfalls --------------------------------------------

    def _departure_scope(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[int | None, int | None]:
        if row["file_id"] is not None:
            scope = conn.execute(
                "SELECT m.module_id, m.phase_id FROM files f "
                "JOIN modules m ON m.module_id = f.module_id WHERE f.file_id = ?",
                (row["file_id"],),
            ).fetchone()
            if scope is not None:
                return scope["module_id"], scope["phase_id"]
        agent = conn.execute(
            "SELECT module_id, phase_id FROM agents WHERE agent_id = ?", (row["agent_id"],)
        ).fetchone()
        if agent is None:
            return None, None
        return agent["module_id"], agent["phase_id"]

    def _blocking_departures(
        self,
        conn: sqlite3.Connection,
        states: tuple[str, ...],
        *,
        run_id: int | None = None,
        module_id: int | None = None,
        phase_id: int | None = None,
    ) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in states)
        rows = conn.execute(
            f"SELECT * FROM departures WHERE kind = 'departure' AND state IN ({placeholders}) "
            "AND (? IS NULL OR run_id = ?) ORDER BY departure_id",
            (*states, run_id, run_id),
        ).fetchall()
        blocking = []
        for row in rows:
            # No approval can mark a departure with no file reworked, so its pushback is final.
            if row["state"] == "pushed_back" and row["file_id"] is None:
                continue
            row_module, row_phase = self._departure_scope(conn, row)
            if module_id is not None and row_module != module_id:
                continue
            if phase_id is not None and row_phase != phase_id:
                continue
            blocking.append(row)
        return blocking

    def _level_agent(
        self, conn: sqlite3.Connection, row: sqlite3.Row, level: str
    ) -> sqlite3.Row | None:
        module_id, phase_id = self._departure_scope(conn, row)
        if level == "lead":
            scope_column, scope_id = "module_id", module_id
        elif level == "manager":
            scope_column, scope_id = "phase_id", phase_id
        else:
            scope_column, scope_id = "run_id", row["run_id"]
        return conn.execute(
            f"SELECT * FROM agents WHERE role = ? AND {scope_column} = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (level, scope_id),
        ).fetchone()

    def _level_label(self, conn: sqlite3.Connection, row: sqlite3.Row, level: str) -> str:
        agent = self._level_agent(conn, row, level)
        title = _ROLE_TITLE[level]
        if agent is None:
            return f"the {title}, which is not live"
        return f"the {title} ({agent['name']})"

    def _describe_waits(self, conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
        waits = []
        for row in rows:
            if row["state"] == "pushed_back":
                waits.append(
                    f"{row['departure_id']} is pushed back and waits on the reworked file's "
                    "approval"
                )
            else:
                label = self._level_label(conn, row, row["level"] or "oracle")
                waits.append(
                    f"{row['departure_id']} is {row['state']} and waits on {label} to call "
                    "departure_decide"
                )
        return "; ".join(waits)

    def departure_record(
        self,
        caller: str,
        agent_id: str,
        body: str,
        file_id: int | None = None,
        guideline_id: int | None = None,
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "coder", "lead", "manager", "oracle")

            if c.role == "coder":
                if file_id is None:
                    file_id = c.file_id
                if file_id != c.file_id:
                    raise LedgerError(f"{caller!r} may record a departure only for its own file")
            elif file_id is not None:
                file_row = conn.execute(
                    "SELECT m.module_id, m.phase_id FROM files f "
                    "JOIN modules m ON m.module_id = f.module_id WHERE f.file_id = ?",
                    (file_id,),
                ).fetchone()
                if file_row is None:
                    raise LedgerError(f"unknown file_id {file_id!r}")
                if c.role == "lead" and file_row["module_id"] != c.module_id:
                    raise LedgerError(f"{caller!r} may record a departure only for its own module")
                if c.role == "manager" and file_row["phase_id"] != c.phase_id:
                    raise LedgerError(f"{caller!r} may record a departure only for its own phase")

            handoff_id = None
            if file_id is not None and c.role == "coder":
                handoff_row = conn.execute(
                    "SELECT handoff_id FROM handoffs WHERE file_id = ? AND agent_id = ? "
                    "AND state = 'submitted' ORDER BY handoff_id DESC LIMIT 1",
                    (file_id, c.agent_id),
                ).fetchone()
                if handoff_row is not None:
                    handoff_id = handoff_row["handoff_id"]

            level = _FIRST_LEVEL.get(c.role)
            if level is None:
                cur = conn.execute(
                    "INSERT INTO departures (run_id, agent_id, guideline_id, file_id, handoff_id, "
                    "kind, state, body, signed_off_at) "
                    f"VALUES (?, ?, ?, ?, ?, 'departure', 'signed_off', ?, {_NOW})",
                    (c.run_id, c.agent_id, guideline_id, file_id, handoff_id, body),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO departures (run_id, agent_id, guideline_id, file_id, handoff_id, "
                    "kind, state, body, level) VALUES (?, ?, ?, ?, ?, 'departure', 'open', ?, ?)",
                    (c.run_id, c.agent_id, guideline_id, file_id, handoff_id, body, level),
                )
            departure_id = cur.lastrowid

        return self._departure_dict(departure_id)

    def _departure_dict(self, departure_id: int | None) -> dict:
        result = dict(
            self.conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (departure_id,)
            ).fetchone()
        )
        result["decisions"] = _rows(
            self.conn.execute(
                "SELECT d.*, a.name AS agent_name FROM departure_decisions d "
                "LEFT JOIN agents a ON a.agent_id = d.agent_id "
                "WHERE d.departure_id = ? ORDER BY d.decision_id",
                (departure_id,),
            )
        )
        return result

    def departure_decide(
        self,
        caller: str,
        agent_id: str,
        departure_id: int,
        decision: Literal["agree", "push_back"],
        reason: str,
        solution: str | None = None,
    ) -> dict:
        if decision not in ("agree", "push_back"):
            raise LedgerError(f"unknown decision {decision!r}; use 'agree' or 'push_back'")
        if not reason.strip():
            raise LedgerError("a decision needs a non-empty reason")
        if decision == "push_back" and not (solution or "").strip():
            raise LedgerError("a pushback needs a suggested solution")

        wakeup = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (departure_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"unknown departure_id {departure_id!r}")
            if row["kind"] != "departure":
                raise LedgerError(f"departure {departure_id} is a {row['kind']!r}, not a departure")
            if row["state"] not in _PENDING_STATES or row["level"] is None:
                raise LedgerError(
                    f"departure {departure_id} is {row['state']}; no decision is pending"
                )

            level = row["level"]
            module_id, phase_id = self._departure_scope(conn, row)
            in_scope = (
                (level == "lead" and c.module_id == module_id)
                or (level == "manager" and c.phase_id == phase_id)
                or (level == "oracle" and c.run_id == row["run_id"])
            )
            if c.role != level or not in_scope:
                raise LedgerError(
                    f"departure {departure_id} is {row['state']} and waits on "
                    f"{self._level_label(conn, row, level)}; {caller!r} may not decide it"
                )

            conn.execute(
                "INSERT INTO departure_decisions (departure_id, role, agent_id, decision, reason, "
                "solution) VALUES (?, ?, ?, ?, ?, ?)",
                (departure_id, c.role, c.agent_id, decision, reason, solution),
            )
            if decision == "agree":
                new_state = _AGREED_STATE[level]
                conn.execute(
                    "UPDATE departures SET state = ?, level = ?, decided_by = ?, "
                    f"decision_reason = ?, decided_at = {_NOW}, "
                    f"signed_off_at = CASE WHEN ? = 'signed_off' THEN {_NOW} END "
                    "WHERE departure_id = ?",
                    (new_state, _NEXT_LEVEL[level], c.agent_id, reason, new_state, departure_id),
                )
            else:
                conn.execute(
                    "UPDATE departures SET state = 'pushed_back', level = NULL, decided_by = ?, "
                    f"decision_reason = ?, solution = ?, decided_at = {_NOW} "
                    "WHERE departure_id = ?",
                    (c.agent_id, reason, solution, departure_id),
                )
                file_row = (
                    conn.execute(
                        "SELECT * FROM files WHERE file_id = ?", (row["file_id"],)
                    ).fetchone()
                    if row["file_id"] is not None
                    else None
                )
                lead_returns_it = (
                    level == "lead" and file_row is not None and file_row["released_at"] is None
                )
                if not lead_returns_it:
                    wakeup = self._resume_chain(
                        conn, c, row, file_row, phase_id, reason, solution or ""
                    )

        result = self._departure_dict(departure_id)
        result["next"] = self.next_step(wakeup)
        return result

    def _departure_chain(
        self, conn: sqlite3.Connection, c: Caller, row: sqlite3.Row, file_row: sqlite3.Row | None
    ) -> list[sqlite3.Row]:
        start_id = row["agent_id"]
        if file_row is not None:
            coder = None
            if row["handoff_id"] is not None:
                coder = conn.execute(
                    "SELECT a.agent_id FROM handoffs h JOIN agents a ON a.agent_id = h.agent_id "
                    "WHERE h.handoff_id = ?",
                    (row["handoff_id"],),
                ).fetchone()
            if coder is None:
                coder = conn.execute(
                    "SELECT agent_id FROM agents WHERE file_id = ? AND role = 'coder' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (file_row["file_id"],),
                ).fetchone()
            if coder is not None:
                start_id = coder["agent_id"]

        chain: list[sqlite3.Row] = []
        agent_id: str | None = start_id
        while agent_id is not None:
            agent = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if agent is None or agent["role"] not in _ROLE_RANK:
                break
            if _ROLE_RANK[agent["role"]] >= _ROLE_RANK[c.role]:
                break
            chain.append(agent)
            agent_id = agent["parent_agent_id"]
        chain.reverse()
        return chain

    def _unrelease_agent(self, conn: sqlite3.Connection, agent: sqlite3.Row, reason: str) -> None:
        if agent["ended_at"] is None:
            if agent["state"] == "handed_up":
                conn.execute(
                    "UPDATE agents SET state = 'idle' WHERE agent_id = ?", (agent["agent_id"],)
                )
                self._log_event(conn, agent["agent_id"], agent["state"], "idle", reason)
            return
        clash = conn.execute(
            "SELECT 1 FROM agents WHERE name = ? AND ended_at IS NULL AND agent_id != ?",
            (agent["name"], agent["agent_id"]),
        ).fetchone()
        if clash is not None:
            raise LedgerError(
                f"cannot resume {agent['name']!r}: a live agent already holds that name"
            )
        conn.execute(
            "UPDATE agents SET state = 'idle', ended_at = NULL, end_reason = NULL, "
            "phase_at_end = NULL WHERE agent_id = ?",
            (agent["agent_id"],),
        )
        self._log_event(conn, agent["agent_id"], agent["state"], "idle", reason)

    def _reopen_file(
        self, conn: sqlite3.Connection, c: Caller, file_row: sqlite3.Row
    ) -> int | None:
        path = file_row["path"]
        if file_row["state"] == "superseded":
            raise LedgerError(f"file {file_row['file_id']} ({path}) is superseded by a later claim")
        if file_row["released_at"] is not None:
            clash = conn.execute(
                "SELECT owner_agent_id FROM files WHERE path = ? AND released_at IS NULL "
                "AND file_id != ?",
                (path, file_row["file_id"]),
            ).fetchone()
            if clash is not None:
                raise LedgerError(
                    f"{path!r} has a live claim for {clash['owner_agent_id']!r}; the file "
                    "cannot reopen for rework until that claim is released"
                )
            conn.execute(
                "UPDATE files SET state = 'returned', released_at = NULL WHERE file_id = ?",
                (file_row["file_id"],),
            )
            return None
        submitted = conn.execute(
            "SELECT handoff_id FROM handoffs WHERE file_id = ? AND state = 'submitted' "
            "ORDER BY handoff_id DESC LIMIT 1",
            (file_row["file_id"],),
        ).fetchone()
        if submitted is None:
            return None
        conn.execute(
            f"UPDATE handoffs SET state = 'returned', decided_at = {_NOW}, decided_by = ? "
            "WHERE file_id = ? AND state = 'submitted'",
            (c.agent_id, file_row["file_id"]),
        )
        conn.execute(
            "UPDATE files SET state = 'returned' WHERE file_id = ?", (file_row["file_id"],)
        )
        return submitted["handoff_id"]

    def _resume_chain(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        row: sqlite3.Row,
        file_row: sqlite3.Row | None,
        phase_id: int | None,
        reason: str,
        solution: str,
    ) -> dict | None:
        departure_id = row["departure_id"]
        chain = self._departure_chain(conn, c, row, file_row)
        where = f" on {file_row['path']}" if file_row is not None else ""
        event = f"departure_decide: departure {departure_id} pushed back by {c.name}"

        if file_row is not None:
            returned_handoff = self._reopen_file(conn, c, file_row)
            next_round = conn.execute(
                "SELECT COALESCE(MAX(round), 0) + 1 AS n FROM attempts WHERE file_id = ?",
                (file_row["file_id"],),
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO attempts (file_id, handoff_id, issue_ids_json, targeted_json, "
                "round) VALUES (?, ?, ?, '[]', ?)",
                (
                    file_row["file_id"],
                    returned_handoff or row["handoff_id"],
                    json.dumps([f"Departure {departure_id} pushed back: {solution}"]),
                    next_round,
                ),
            )
            conn.execute(
                "UPDATE modules SET state = 'returned' WHERE module_id = ?",
                (file_row["module_id"],),
            )
        if phase_id is not None:
            conn.execute(
                "UPDATE phases SET state = 'working' WHERE phase_id = ? AND state = 'handed_up'",
                (phase_id,),
            )

        for agent in chain:
            self._unrelease_agent(conn, agent, event)

        header = [
            f"Departure {departure_id}{where} was pushed back by {c.name} ({c.role}).",
            f"Departure: {row['body']}",
            f"Reason: {reason}",
            f"Solution: {solution}",
        ]
        wakeup = None
        upper = c
        for index, agent in enumerate(chain):
            child = chain[index + 1] if index + 1 < len(chain) else None
            if child is not None:
                action = (
                    f"Wake {child['name']} as the Stop hook asks; the reworked file comes back "
                    "up through the normal reviews."
                )
                pointer_action = "pass the wake-up down"
            elif agent["role"] == "coder":
                action = (
                    "Try the solution, then follow your normal order of work and hand off again."
                )
                pointer_action = "try the solution"
            else:
                action = "Apply the solution in your own scope."
                pointer_action = "apply the solution"
            conn.execute(
                "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                (c.run_id, c.name, agent["name"], "\n".join([*header, action])),
            )
            owed = self._owe_wakeup(
                conn,
                upper,
                agent["agent_id"],
                "departure_decide",
                f"Departure {departure_id}{where} was pushed back by {c.name}; read it with "
                f"message_inbox and {pointer_action}.",
            )
            if index == 0:
                wakeup = owed
            upper = _caller_of(agent)
        return wakeup

    def shortfall_record(
        self, caller: str, agent_id: str, body: str, file_id: int | None = None
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            cur = conn.execute(
                "INSERT INTO departures (run_id, agent_id, file_id, kind, state, body) "
                "VALUES (?, ?, ?, 'shortfall', 'recorded', ?)",
                (c.run_id, c.agent_id, file_id, body),
            )
            shortfall_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (shortfall_id,)
            ).fetchone()
        )
