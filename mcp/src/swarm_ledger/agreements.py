from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Literal

from .db import write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .review import _PARENT_ROLE, _ROLE_RANK
from .settings import Settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_CR_BLOCKING_STATES = ("open", "accepted", "completed")


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


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
        open_rows = conn.execute(
            "SELECT departure_id FROM departures WHERE handoff_id = ? AND state = 'open'",
            (handoff_id,),
        ).fetchall()
        if open_rows:
            ids = [r["departure_id"] for r in open_rows]
            raise LedgerError(f"departure(s) {ids} are open; decide it with departure_decide")
        denied_rows = conn.execute(
            "SELECT departure_id FROM departures WHERE handoff_id = ? AND state = 'denied'",
            (handoff_id,),
        ).fetchall()
        if denied_rows:
            ids = [r["departure_id"] for r in denied_rows]
            raise LedgerError(
                f"departure(s) {ids} are denied; a denied departure is a return: call return_work"
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

            cur = conn.execute(
                "INSERT INTO departures (run_id, agent_id, guideline_id, file_id, handoff_id, "
                "kind, state, body) VALUES (?, ?, ?, ?, ?, 'departure', 'open', ?)",
                (c.run_id, c.agent_id, guideline_id, file_id, handoff_id, body),
            )
            departure_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (departure_id,)
            ).fetchone()
        )

    def departure_decide(
        self,
        caller: str,
        agent_id: str,
        departure_id: int,
        decision: Literal["accepted", "denied"],
        reason: str,
        solution: str | None = None,
    ) -> dict:
        if decision not in ("accepted", "denied"):
            raise LedgerError(f"unknown decision {decision!r}")
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (departure_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"unknown departure_id {departure_id!r}")
            if row["kind"] != "departure":
                raise LedgerError(f"departure {departure_id} is a {row['kind']!r}, not a departure")

            proposer = conn.execute(
                "SELECT role FROM agents WHERE agent_id = ?", (row["agent_id"],)
            ).fetchone()
            proposer_role = proposer["role"] if proposer is not None else "oracle"
            required_role = _PARENT_ROLE.get(proposer_role, "oracle")
            required_rank = _ROLE_RANK[required_role]
            caller_rank = _ROLE_RANK[c.role]

            if row["state"] == "open":
                if caller_rank < required_rank:
                    raise LedgerError(
                        f"role {c.role!r} may not decide a departure recorded by a "
                        f"{proposer_role!r}"
                    )
            elif row["state"] == "accepted":
                if decision != "denied" or caller_rank <= required_rank:
                    raise LedgerError(
                        "an accepted departure can only be overridden by a denial from a "
                        f"role higher than {required_role!r}"
                    )
            else:
                raise LedgerError(f"departure {departure_id} is already {row['state']}")

            if decision == "denied" and not (solution or "").strip():
                raise LedgerError("a denial needs a solution")

            was_accepted = row["state"] == "accepted"
            conn.execute(
                f"UPDATE departures SET state = ?, decided_by = ?, decision_reason = ?, "
                f"solution = ?, decided_at = {_NOW} WHERE departure_id = ?",
                (decision, c.agent_id, reason, solution, departure_id),
            )

            if decision == "denied" and was_accepted and row["file_id"] is not None:
                file_row = conn.execute(
                    "SELECT state FROM files WHERE file_id = ?", (row["file_id"],)
                ).fetchone()
                if file_row is not None and file_row["state"] == "approved":
                    conn.execute(
                        "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, state) "
                        "VALUES (?, ?, ?, ?, 'open')",
                        (c.run_id, row["file_id"], c.agent_id, solution),
                    )

        return dict(
            self.conn.execute(
                "SELECT * FROM departures WHERE departure_id = ?", (departure_id,)
            ).fetchone()
        )

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
