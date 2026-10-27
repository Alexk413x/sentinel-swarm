from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Literal

from . import graph, rubric
from .db import dict_rows, in_clause, row_dict, write_tx
from .host import LedgerHost
from .identity import Caller, LedgerError, ledger_errors, require_role, resolve
from .review import latest_lead_review

_CLEAN_TEST_RUN = "t.exit_code = 0 AND t.failed = 0 AND t.skipped = 0"


def _review_scores_json(scores: list[dict] | None) -> list[dict]:
    parsed = [rubric.parse_review_score(s) for s in (scores or [])]
    with ledger_errors():
        rubric.validate_review_scores(parsed)
    return [{"dimension": s.dimension, "value": s.value, "reason": s.reason} for s in parsed]


def _returned_message(
    conn: sqlite3.Connection, label: str, reviewer: str, notes: str, file_notes: dict[str, str]
) -> str:
    lines = [f"{label} was returned by {reviewer}.", f"Notes: {notes}"]
    if file_notes:
        lines.append("Notes by file:")
        for file_id, note in file_notes.items():
            row = conn.execute(
                "SELECT path FROM files WHERE file_id = ?",
                (int(file_id) if str(file_id).isdigit() else None,),
            ).fetchone()
            where = f"file {file_id} ({row['path']})" if row is not None else f"file {file_id}"
            lines.append(f"- {where}: {note}")
    return "\n".join(lines)


class OversightMixin(LedgerHost):
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
            f"WHERE {_CLEAN_TEST_RUN} AND "
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
            f"WHERE t.run_id = ? AND t.scope = 'full' AND a.role = 'oracle' AND {_CLEAN_TEST_RUN} "
            "AND (? IS NULL OR t.created_at >= ?) LIMIT 1",
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
        lead_review = latest_lead_review(conn, file_id, handoff["created_at"])
        if lead_review is None:
            return []
        self_scores = self._scores_for(handoff["self_review_id"])
        lead_scores = self._scores_for(lead_review["review_id"])
        return rubric.disagreements(self_scores, lead_scores, self.settings.rubric)

    def _require_module_ready_for_review(
        self,
        conn: sqlite3.Connection,
        module_id: int,
        phase_id: int,
        disagreement_notes: dict[str, str],
    ) -> None:
        files = dict_rows(conn.execute("SELECT * FROM files WHERE module_id = ?", (module_id,)))
        not_ready = [
            f["path"] for f in files if f["state"] not in ("approved", "incomplete", "superseded")
        ]
        if not_ready:
            raise LedgerError(f"file(s) are not approved or incomplete yet: {not_ready}")
        self._block_module_review_for_departures(conn, module_id)

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
        modules = dict_rows(
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
    ) -> None:
        phase_id = phase_row["phase_id"]
        self._block_phase_review_for_departures(conn, phase_id)
        if not self._passing_full_test_after(conn, phase_row["run_id"], phase_row["handed_up_at"]):
            raise LedgerError(
                "no passing tests_run of scope 'full' by the Oracle exists after the phase was "
                "handed up; call tests_run(scope='full')"
            )

        untested = [
            f"file {f['file_id']} ({f['path']})"
            for f in dict_rows(
                conn.execute(
                    "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
                    "WHERE m.phase_id = ? AND f.state IN ('approved', 'incomplete') "
                    "AND (f.test_path IS NULL OR f.test_path = '')",
                    (phase_id,),
                )
            )
            if graph.code_symbols_for(self.repo_root, f["path"])
        ]
        if untested:
            raise LedgerError(
                f"{', '.join(untested)} contain functions or classes in the code graph but were "
                "claimed with no test_path, so the ledger never ran tests for them. Test anchors "
                "in the graph do not count. Return the phase so the module's Lead re-claims each "
                "path with claim_file(path, test_path=<its test file>) for a new Coder; the new "
                "claim supersedes the old one"
            )

        target = self.settings.rubric.target
        missing_low = []
        for file_row in conn.execute(
            "SELECT f.* FROM files f JOIN modules m ON m.module_id = f.module_id "
            "WHERE m.phase_id = ?",
            (phase_id,),
        ).fetchall():
            lead_review = latest_lead_review(conn, file_row["file_id"])
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

    def _record_review(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        *,
        kind: str,
        scope: dict[str, int],
        subject: sqlite3.Row | None,
        outcome: str,
        notes: str,
        details_notes: dict[str, dict[str, str]],
        scores: list[dict] | None,
        ready: Callable[[], None],
        reopen: tuple[str, int],
        returned_message: str,
        pointer: str,
    ) -> tuple[int | None, dict | None, bool]:
        review_scores: list[dict] = []
        wakeup = None
        subject_ended = False
        if outcome == "accepted":
            ready()
            review_scores = _review_scores_json(scores)
        else:
            conn.execute(reopen[0], (reopen[1],))
            if subject is not None:
                conn.execute(
                    "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                    (c.run_id, c.name, subject["name"], returned_message),
                )
            if subject is not None and subject["ended_at"] is None:
                wakeup = self._owe_wakeup(conn, c, subject["agent_id"], f"{kind}_review", pointer)
            else:
                subject_ended = True
        columns = {
            **scope,
            "reviewer_agent_id": c.agent_id,
            "subject_agent_id": subject["agent_id"] if subject is not None else None,
            "kind": "manager" if kind == "module" else "oracle",
            "outcome": outcome,
            "notes": notes,
            "details_json": json.dumps({**details_notes, "review_scores": review_scores}),
        }
        cur = conn.execute(
            f"INSERT INTO reviews ({', '.join(columns)}) VALUES ({in_clause(columns)})",
            tuple(columns.values()),
        )
        return cur.lastrowid, wakeup, subject_ended

    def returned_review(self, brief: dict) -> dict | None:
        if brief["child_role"] == "manager" and brief["phase_id"] is not None:
            where, key, kind = "phase_id", brief["phase_id"], "oracle"
        elif brief["child_role"] == "lead" and brief["module_id"] is not None:
            where, key, kind = "module_id", brief["module_id"], "manager"
        else:
            return None
        row = self.conn.execute(
            f"SELECT review_id, outcome, notes, details_json, created_at FROM reviews "
            f"WHERE {where} = ? AND kind = ? ORDER BY review_id DESC LIMIT 1",
            (key, kind),
        ).fetchone()
        if row is None or row["outcome"] != "returned":
            return None
        details = json.loads(row["details_json"] or "{}")
        file_notes = details.get("low_score_notes") or details.get("disagreement_notes") or {}
        return {
            "review_id": row["review_id"],
            "outcome": row["outcome"],
            "notes": row["notes"],
            "file_notes": file_notes,
            "created_at": row["created_at"],
        }

    def _review_result(
        self, review_id: int | None, outcome: str, ended_key: str, ended: bool, wakeup: dict | None
    ) -> dict:
        result = row_dict(self.conn, "reviews", "review_id", review_id)
        if outcome == "returned":
            result[ended_key] = ended
            result["next"] = self.next_step(wakeup)
        return result

    # -- Manager review of a module ------------------------------------------

    def module_review(
        self,
        caller: str,
        agent_id: str,
        module_id: int,
        outcome: Literal["accepted", "returned"],
        notes: str,
        disagreement_notes: dict[str, str] | None = None,
        scores: list[dict] | None = None,
    ) -> dict:
        if outcome not in ("accepted", "returned"):
            raise LedgerError(f"unknown module_review outcome {outcome!r}")
        file_notes = disagreement_notes or {}

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
            review_id, wakeup, ended = self._record_review(
                conn,
                c,
                kind="module",
                scope={"module_id": module_id, "phase_id": module_row["phase_id"]},
                subject=self._latest_role_agent(conn, module_id=module_id, role="lead"),
                outcome=outcome,
                notes=notes,
                details_notes={"disagreement_notes": file_notes},
                scores=scores,
                ready=lambda: self._require_module_ready_for_review(
                    conn, module_id, module_row["phase_id"], file_notes
                ),
                reopen=("UPDATE modules SET state = 'returned' WHERE module_id = ?", module_id),
                returned_message=_returned_message(
                    conn, f"Module {module_id}", c.name, notes, file_notes
                ),
                pointer=f"Module {module_id} was returned; read why with message_inbox.",
            )

        return self._review_result(review_id, outcome, "lead_ended", ended, wakeup)

    # -- Oracle review of a phase --------------------------------------------

    def phase_review(
        self,
        caller: str,
        agent_id: str,
        phase_id: int,
        outcome: Literal["accepted", "returned"],
        notes: str,
        low_score_notes: dict[str, str] | None = None,
        scores: list[dict] | None = None,
    ) -> dict:
        if outcome not in ("accepted", "returned"):
            raise LedgerError(f"unknown phase_review outcome {outcome!r}")
        file_notes = low_score_notes or {}

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
            review_id, wakeup, ended = self._record_review(
                conn,
                c,
                kind="phase",
                scope={"phase_id": phase_id},
                subject=self._latest_role_agent(conn, phase_id=phase_id, role="manager"),
                outcome=outcome,
                notes=notes,
                details_notes={"low_score_notes": file_notes},
                scores=scores,
                ready=lambda: self._require_phase_ready_for_review(conn, phase_row, file_notes),
                reopen=("UPDATE phases SET state = 'working' WHERE phase_id = ?", phase_id),
                returned_message=_returned_message(
                    conn, f"Phase {phase_id}", c.name, notes, file_notes
                ),
                pointer=f"Phase {phase_id} was returned; read why with message_inbox.",
            )

        return self._review_result(review_id, outcome, "manager_ended", ended, wakeup)
