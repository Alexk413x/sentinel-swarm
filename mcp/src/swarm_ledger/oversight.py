from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from . import rubric
from .db import write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .rubric import Thresholds
from .settings import Settings


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


class OversightMixin:
    """The Manager-level module review and the Oracle-level phase review.

    Split out of `ledger.py` and `review.py` like `ReviewMixin`; every method
    here still assumes it is mixed into `Ledger` and relies on `self.conn`,
    `self.settings`, `self._owe_wakeup`, `self.next_step`, `self._scores_for`,
    and `self._thresholds`.
    """

    conn: sqlite3.Connection
    settings: Settings
    repo_root: Path

    def _owe_wakeup(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        to_agent_id: str | None,
        reason: str,
        pointer: str,
    ) -> dict | None: ...

    def next_step(self, wakeup: dict | None) -> str | None: ...

    def _scores_for(self, review_id: int) -> dict[str, float]: ...

    def _thresholds(self) -> Thresholds: ...

    # -- Shared helpers -----------------------------------------------------

    def _latest_role_agent(
        self,
        conn: sqlite3.Connection,
        *,
        module_id: int | None = None,
        phase_id: int | None = None,
        role: str,
    ) -> sqlite3.Row | None:
        if module_id is not None:
            return conn.execute(
                "SELECT * FROM agents WHERE module_id = ? AND role = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (module_id, role),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM agents WHERE phase_id = ? AND role = ? ORDER BY started_at DESC LIMIT 1",
            (phase_id, role),
        ).fetchone()

    def _module_last_decision(self, conn: sqlite3.Connection, module_id: int) -> str | None:
        row = conn.execute(
            "SELECT MAX(h.decided_at) AS t FROM handoffs h JOIN files f ON f.file_id = h.file_id "
            "WHERE f.module_id = ?",
            (module_id,),
        ).fetchone()
        return row["t"] if row is not None else None

    def _passing_scoped_test_after(
        self, conn: sqlite3.Connection, module_id: int, phase_id: int | None, after: str | None
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM test_runs t JOIN agents a ON a.agent_id = t.agent_id "
            "WHERE t.exit_code = 0 AND t.failed = 0 AND t.skipped = 0 AND "
            "((t.scope = 'module' AND a.module_id = ?) OR (t.scope = 'phase' AND a.phase_id = ?)) "
            "AND (? IS NULL OR t.created_at >= ?) LIMIT 1",
            (module_id, phase_id, after, after),
        ).fetchone()
        return row is not None

    def _passing_full_test_after(
        self, conn: sqlite3.Connection, run_id: int, after: str | None
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM test_runs t JOIN agents a ON a.agent_id = t.agent_id "
            "WHERE t.run_id = ? AND t.scope = 'full' AND a.role = 'oracle' AND t.exit_code = 0 "
            "AND t.failed = 0 AND t.skipped = 0 AND (? IS NULL OR t.created_at >= ?) LIMIT 1",
            (run_id, after, after),
        ).fetchone()
        return row is not None

    def _file_disagreements(self, conn: sqlite3.Connection, file_id: int) -> list[str]:
        handoff = conn.execute(
            "SELECT * FROM handoffs WHERE file_id = ? AND state = 'approved' "
            "ORDER BY handoff_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        if handoff is None or handoff["self_review_id"] is None:
            return []
        lead_review = conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' AND created_at >= ? "
            "ORDER BY review_id DESC LIMIT 1",
            (file_id, handoff["created_at"]),
        ).fetchone()
        if lead_review is None:
            return []
        self_scores = self._scores_for(handoff["self_review_id"])
        lead_scores = self._scores_for(lead_review["review_id"])
        return rubric.disagreements(self_scores, lead_scores, self._thresholds())

    def _require_module_ready_for_review(
        self,
        conn: sqlite3.Connection,
        module_id: int,
        phase_id: int,
        disagreement_notes: dict[str, str],
    ) -> None:
        files = _rows(conn.execute("SELECT * FROM files WHERE module_id = ?", (module_id,)))
        not_ready = [
            f["path"] for f in files if f["state"] not in ("approved", "incomplete", "superseded")
        ]
        if not_ready:
            raise LedgerError(f"file(s) are not approved or incomplete yet: {not_ready}")

        last_change = self._module_last_decision(conn, module_id)
        if not self._passing_scoped_test_after(conn, module_id, phase_id, last_change):
            raise LedgerError(
                "no passing test run of scope 'module' for this module (or 'phase' for this "
                "phase) exists after the module's last file approval; call tests_run"
            )

        missing = [
            f["file_id"]
            for f in files
            if f["state"] == "approved"
            and self._file_disagreements(conn, f["file_id"])
            and not str(disagreement_notes.get(str(f["file_id"])) or "").strip()
        ]
        if missing:
            raise LedgerError(
                f"disagreement_notes needs a non-empty note keyed by file_id for {missing}, "
                "where the self and lead scores did not agree"
            )

    def _require_phase_modules_reviewed(self, conn: sqlite3.Connection, phase_id: int) -> None:
        modules = _rows(
            conn.execute("SELECT module_id FROM modules WHERE phase_id = ?", (phase_id,))
        )
        missing = []
        for module in modules:
            last_change = self._module_last_decision(conn, module["module_id"])
            review = conn.execute(
                "SELECT created_at FROM reviews WHERE module_id = ? AND kind = 'manager' "
                "AND outcome = 'accepted' ORDER BY review_id DESC LIMIT 1",
                (module["module_id"],),
            ).fetchone()
            if review is None or (last_change is not None and review["created_at"] < last_change):
                missing.append(module["module_id"])
        if missing:
            raise LedgerError(
                f"module(s) {missing} have no accepted manager review newer than their last "
                "change; call module_review first"
            )

    def _require_phase_ready_for_review(
        self,
        conn: sqlite3.Connection,
        phase_row: sqlite3.Row,
        low_score_notes: dict[str, str],
        departure_notes: dict[str, str],
    ) -> None:
        phase_id = phase_row["phase_id"]
        if not self._passing_full_test_after(conn, phase_row["run_id"], phase_row["handed_up_at"]):
            raise LedgerError(
                "no passing tests_run of scope 'full' by the Oracle exists after the phase was "
                "handed up; call tests_run(scope='full')"
            )

        target = self._thresholds().target
        missing_low = []
        for file_row in conn.execute(
            "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
            "WHERE m.phase_id = ?",
            (phase_id,),
        ).fetchall():
            lead_review = conn.execute(
                "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' "
                "ORDER BY review_id DESC LIMIT 1",
                (file_row["file_id"],),
            ).fetchone()
            if lead_review is None:
                continue
            scores = self._scores_for(lead_review["review_id"])
            if any(score < target for score in scores.values()):
                if not str(low_score_notes.get(str(file_row["file_id"])) or "").strip():
                    missing_low.append(file_row["file_id"])
        if missing_low:
            raise LedgerError(
                f"low_score_notes needs a non-empty note keyed by file_id for {missing_low}, "
                "whose latest lead review has a dimension below the rubric target"
            )

        missing_dep = []
        for dep in conn.execute(
            "SELECT * FROM departures WHERE run_id = ? AND state = 'accepted'",
            (phase_row["run_id"],),
        ).fetchall():
            if dep["file_id"] is not None:
                owning_phase = conn.execute(
                    "SELECT m.phase_id AS phase_id FROM files f "
                    "JOIN modules m ON m.module_id = f.module_id WHERE f.file_id = ?",
                    (dep["file_id"],),
                ).fetchone()
                if owning_phase is None or owning_phase["phase_id"] != phase_id:
                    continue
            if not str(departure_notes.get(str(dep["departure_id"])) or "").strip():
                missing_dep.append(dep["departure_id"])
        if missing_dep:
            raise LedgerError(
                f"departure_notes needs a non-empty note keyed by departure_id for {missing_dep}"
            )

    # -- Manager review of a module ------------------------------------------

    def module_review(
        self,
        caller: str,
        agent_id: str,
        module_id: int,
        outcome: Literal["accepted", "returned"],
        notes: str,
        disagreement_notes: dict[str, str] | None = None,
    ) -> dict:
        if outcome not in ("accepted", "returned"):
            raise LedgerError(f"unknown module_review outcome {outcome!r}")
        disagreement_notes = disagreement_notes or {}

        wakeup = None
        lead_ended = False
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "manager")
            module_row = conn.execute(
                "SELECT * FROM modules WHERE module_id = ?", (module_id,)
            ).fetchone()
            if module_row is None:
                raise LedgerError(f"unknown module_id {module_id!r}")
            if c.phase_id != module_row["phase_id"]:
                raise LedgerError(f"{caller!r} is not the Manager of this module's phase")

            lead_row = self._latest_role_agent(conn, module_id=module_id, role="lead")

            if outcome == "accepted":
                self._require_module_ready_for_review(
                    conn, module_id, module_row["phase_id"], disagreement_notes
                )
            else:
                conn.execute(
                    "UPDATE modules SET state = 'returned' WHERE module_id = ?", (module_id,)
                )
                if lead_row is not None and lead_row["ended_at"] is None:
                    wakeup = self._owe_wakeup(
                        conn,
                        c,
                        lead_row["agent_id"],
                        "module_review",
                        f"Module {module_id} was returned; read the review in the ledger.",
                    )
                else:
                    lead_ended = True

            cur = conn.execute(
                "INSERT INTO reviews (module_id, phase_id, reviewer_agent_id, subject_agent_id, "
                "kind, outcome, notes, details_json) VALUES (?, ?, ?, ?, 'manager', ?, ?, ?)",
                (
                    module_id,
                    module_row["phase_id"],
                    c.agent_id,
                    lead_row["agent_id"] if lead_row is not None else None,
                    outcome,
                    notes,
                    json.dumps({"disagreement_notes": disagreement_notes}),
                ),
            )
            review_id = cur.lastrowid

        result = dict(
            self.conn.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        )
        if outcome == "returned":
            result["lead_ended"] = lead_ended
            result["next"] = self.next_step(wakeup)
        return result

    # -- Oracle review of a phase --------------------------------------------

    def phase_review(
        self,
        caller: str,
        agent_id: str,
        phase_id: int,
        outcome: Literal["accepted", "returned"],
        notes: str,
        low_score_notes: dict[str, str] | None = None,
        departure_notes: dict[str, str] | None = None,
    ) -> dict:
        if outcome not in ("accepted", "returned"):
            raise LedgerError(f"unknown phase_review outcome {outcome!r}")
        low_score_notes = low_score_notes or {}
        departure_notes = departure_notes or {}

        wakeup = None
        manager_ended = False
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            phase_row = conn.execute(
                "SELECT * FROM phases WHERE phase_id = ?", (phase_id,)
            ).fetchone()
            if phase_row is None:
                raise LedgerError(f"unknown phase_id {phase_id!r}")
            if phase_row["state"] != "handed_up":
                raise LedgerError(f"phase {phase_id} is {phase_row['state']!r}, not handed_up")

            manager_row = self._latest_role_agent(conn, phase_id=phase_id, role="manager")

            if outcome == "accepted":
                self._require_phase_ready_for_review(
                    conn, phase_row, low_score_notes, departure_notes
                )
            else:
                conn.execute("UPDATE phases SET state = 'working' WHERE phase_id = ?", (phase_id,))
                if manager_row is not None and manager_row["ended_at"] is None:
                    wakeup = self._owe_wakeup(
                        conn,
                        c,
                        manager_row["agent_id"],
                        "phase_review",
                        f"Phase {phase_id} was returned; read the review in the ledger.",
                    )
                else:
                    manager_ended = True

            cur = conn.execute(
                "INSERT INTO reviews (phase_id, reviewer_agent_id, subject_agent_id, kind, "
                "outcome, notes, details_json) VALUES (?, ?, ?, 'oracle', ?, ?, ?)",
                (
                    phase_id,
                    c.agent_id,
                    manager_row["agent_id"] if manager_row is not None else None,
                    outcome,
                    notes,
                    json.dumps(
                        {"low_score_notes": low_score_notes, "departure_notes": departure_notes}
                    ),
                ),
            )
            review_id = cur.lastrowid

        result = dict(
            self.conn.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        )
        if outcome == "returned":
            result["manager_ended"] = manager_ended
            result["next"] = self.next_step(wakeup)
        return result
