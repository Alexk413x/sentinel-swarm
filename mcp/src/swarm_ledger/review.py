from __future__ import annotations

import json
import re
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal

from . import graph, rubric, versions
from .db import ledger_path, write_tx
from .identity import Caller, LedgerError, require_role, resolve
from .rubric import Rating
from .settings import Settings
from .testing import run_tests

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _parse_rating(raw: object) -> Rating:
    if not isinstance(raw, dict):
        raise LedgerError(
            f"a rating must be an object, got {type(raw).__name__}. {rubric.schema_help()}"
        )
    missing = [key for key in ("dimension", "criterion", "value") if key not in raw]
    if missing:
        raise LedgerError(
            f"rating {sorted(raw.keys())} is missing {missing}. {rubric.schema_help()}"
        )
    value = raw["value"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(f"rating value must be an integer 1..10, got {value!r}")
    return Rating(
        str(raw["dimension"]), str(raw["criterion"]), value, raw.get("reason"), raw.get("ref")
    )


_SCOPE_ROLE = {"file": "coder", "module": "lead", "phase": "manager", "full": "oracle"}
_PARENT_ROLE = {"manager": "oracle", "lead": "manager", "coder": "lead"}
_ROLE_RANK = {"coder": 0, "lead": 1, "manager": 2, "oracle": 3}
_SELECT_RE = re.compile(r"(?is)^\s*select\b")

_CRITERION_TEXT: dict[tuple[str, str], str] = {
    (dimension_key, criterion_key): criterion_text
    for dimension_key, _, criteria in rubric.DIMENSIONS
    for criterion_key, criterion_text in criteria
}


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _agent_name(conn: sqlite3.Connection, agent_id: str | None) -> str:
    if agent_id is None:
        return "unknown"
    row = conn.execute("SELECT name FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return row["name"] if row is not None else agent_id


def _criterion_text(dimension: str, criterion: str) -> str:
    return _CRITERION_TEXT.get((dimension, criterion), f"{dimension}.{criterion}")


def _phase_name_at(conn: sqlite3.Connection, phase_id: int | None) -> str | None:
    if phase_id is None:
        return None
    phase_row = conn.execute("SELECT name FROM phases WHERE phase_id = ?", (phase_id,)).fetchone()
    return phase_row["name"] if phase_row is not None else None


class ReviewMixin:
    """The handoff and review layer: tests, the code graph, scoring, and the
    approve/return/defer decisions a Lead makes about a Coder's work.

    Split out of `ledger.py` to keep that file's run/plan/brief/lifecycle core
    readable on its own; every method here still assumes it is mixed into
    `Ledger` and relies on `self.conn`, `self.settings`, `self.repo_root`,
    `self._log_event`, and `self._active_run`.
    """

    # Declared, not assigned: Ledger.__init__ sets these. The declarations let
    # pyright check this file as if it were part of Ledger, which it always is.
    conn: sqlite3.Connection
    settings: Settings
    repo_root: Path
    _pending_stops: list[tuple[str, str]]

    def _log_event(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        from_state: str | None,
        to_state: str,
        reason: str | None = None,
    ) -> None: ...

    def _active_run(self, conn: sqlite3.Connection) -> sqlite3.Row: ...

    def pause_reason(self, run_id: int) -> str | None: ...

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

    def _block_handoff_for_open_cr(self, conn: sqlite3.Connection, file_id: int) -> None: ...

    def _block_approve_for_cr(self, conn: sqlite3.Connection, file_id: int) -> None: ...

    def _block_approve_for_departures(self, conn: sqlite3.Connection, handoff_id: int) -> None: ...

    def _block_return_for_open_departures(
        self, conn: sqlite3.Connection, handoff_id: int
    ) -> None: ...

    def _mark_departures_reworked(
        self, conn: sqlite3.Connection, file_id: int, handoff_id: int
    ) -> None: ...

    # -- Shared helpers ---------------------------------------------------

    def _thresholds(self) -> rubric.Thresholds:
        r = self.settings.rubric
        return rubric.Thresholds(
            target=r.target,
            floor=r.floor,
            criterion_floor=r.criterion_floor,
            disagreement_gap=r.disagreement_gap,
            plateau=r.plateau,
            regression_tolerance=r.regression_tolerance,
        )

    def _ratings_for(self, review_id: int) -> list[Rating]:
        rows = self.conn.execute(
            "SELECT dimension, criterion, value FROM scores WHERE review_id = ?", (review_id,)
        ).fetchall()
        return [Rating(r["dimension"], r["criterion"], r["value"], None, None) for r in rows]

    def _scores_for(self, review_id: int) -> dict[str, float]:
        return rubric.dimension_scores(self._ratings_for(review_id))

    def _release_agent(self, conn: sqlite3.Connection, target_agent_id: str, reason: str) -> None:
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (target_agent_id,)).fetchone()
        if row is None or row["state"] == "released":
            return
        phase_name = _phase_name_at(conn, row["phase_id"])
        conn.execute(
            f"UPDATE agents SET state = 'released', ended_at = {_NOW}, phase_at_end = ? "
            "WHERE agent_id = ?",
            (phase_name, target_agent_id),
        )
        self._log_event(conn, target_agent_id, row["state"], "released", reason)
        if row["bg_id"] and row["role"] != "oracle":
            self._pending_stops.append((target_agent_id, row["bg_id"]))

    # -- Tests --------------------------------------------------------------

    def tests_run(
        self,
        caller: str,
        agent_id: str,
        scope: Literal["file", "module", "phase", "full"],
        target: str | None = None,
    ) -> dict:
        if scope not in _SCOPE_ROLE:
            raise LedgerError(f"unknown test scope {scope!r}")
        c = resolve(self.conn, caller, agent_id)
        require_role(c, _SCOPE_ROLE[scope])

        if scope == "file":
            if c.file_id is None:
                raise LedgerError(f"{caller!r} has no claimed file")
            file_row = self.conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (c.file_id,)
            ).fetchone()
            if file_row is None or target not in (file_row["path"], file_row["test_path"]):
                raise LedgerError(f"{caller!r} may only run tests for its own file or test file")

        if not self.settings.test_command:
            raise LedgerError("no test command in the profile")

        result = run_tests(self.settings.test_command, target, self.repo_root)

        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
                "passed, failed, skipped, output) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.run_id,
                    c.agent_id,
                    scope,
                    target,
                    result.command,
                    result.exit_code,
                    result.passed,
                    result.failed,
                    result.skipped,
                    result.output,
                ),
            )
            test_run_id = cur.lastrowid

        return {
            "test_run_id": test_run_id,
            "command": result.command,
            "exit_code": result.exit_code,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "errors": result.errors,
            "output": result.output,
            "duration_ms": result.duration_ms,
            "ok": result.ok,
            "reason": result.reason,
        }

    # -- Code graph -----------------------------------------------------------

    def graph_upsert(self, caller: str, agent_id: str, nodes: list[dict]) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder")
        if c.file_id is None:
            raise LedgerError(f"{caller!r} has no claimed file")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (c.file_id,)
        ).fetchone()
        if file_row is None:
            raise LedgerError(f"{caller!r} has no claimed file")
        allowed_paths = {file_row["path"], file_row["test_path"]}

        for node in nodes:
            for anchor in node.get("anchors") or []:
                if isinstance(anchor, str):
                    anchor_path = anchor.split("#", 1)[0].strip()
                elif isinstance(anchor, dict):
                    anchor_path = str(anchor.get("path", "")).strip()
                else:
                    raise LedgerError(f"anchor {anchor!r} must be a string or an object")
                if anchor_path not in allowed_paths:
                    raise LedgerError("a Coder updates only the nodes that anchor on its own file")

        result = graph.graph_upsert(self.repo_root, nodes)

        with write_tx(self.conn) as conn:
            row = conn.execute(
                "SELECT state FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            state = row["state"] if row is not None else None
            self._log_event(conn, agent_id, state, state or "working", "graph_upsert")

        return result

    # -- Scoring --------------------------------------------------------------

    def score_record(
        self,
        caller: str,
        agent_id: str,
        file_id: int,
        ratings: list[dict],
        applicable: dict[str, str | None],
        kind: Literal["self", "lead"],
        targeted: list[str] | None = None,
    ) -> dict:
        if kind not in ("self", "lead"):
            raise LedgerError(f"unknown review kind {kind!r}")

        c = resolve(self.conn, caller, agent_id)
        file_row = self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        if file_row is None:
            raise LedgerError(f"unknown file_id {file_id!r}")

        rating_objs = [_parse_rating(r) for r in ratings]
        if not isinstance(applicable, dict):
            raise LedgerError(f"applicable must be an object. {rubric.schema_help()}")
        try:
            rubric.validate_ratings(rating_objs, applicable)
        except ValueError as exc:
            raise LedgerError(str(exc)) from exc

        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()

        if kind == "self":
            require_role(c, "coder")
            if file_row["owner_agent_id"] != c.name:
                raise LedgerError(f"{caller!r} does not own file {file_id!r}")
            subject_agent_id = c.agent_id
        else:
            require_role(c, "lead")
            if c.module_id != file_row["module_id"]:
                raise LedgerError(f"{caller!r} is not the Lead of this file's module")
            if handoff_row is None:
                raise LedgerError("no handoff exists for this file yet")
            if handoff_row["compared_at"] is not None:
                raise LedgerError(
                    "review_compare already ran for this handoff; blind scoring is closed"
                )
            owner_row = self.conn.execute(
                "SELECT agent_id FROM agents WHERE name = ? AND run_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (file_row["owner_agent_id"], c.run_id),
            ).fetchone()
            subject_agent_id = owner_row["agent_id"] if owner_row is not None else None

        scores = rubric.dimension_scores(rating_objs)
        ok, reasons = rubric.passes(scores, rating_objs, self._thresholds())
        notes = json.dumps({"targeted": targeted}) if targeted else None

        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO reviews (file_id, module_id, phase_id, reviewer_agent_id, "
                "subject_agent_id, kind, outcome, notes, applicable_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    file_row["module_id"],
                    c.phase_id,
                    c.agent_id,
                    subject_agent_id,
                    kind,
                    "pass" if ok else "fail",
                    notes,
                    json.dumps(applicable),
                ),
            )
            review_id = cur.lastrowid
            for rating in rating_objs:
                conn.execute(
                    "INSERT INTO scores (review_id, dimension, criterion, value) "
                    "VALUES (?, ?, ?, ?)",
                    (review_id, rating.dimension, rating.criterion, rating.value),
                )
            closed_ids: list[int] = []
            if kind == "lead":
                closed_ids = self._close_resolved_issues(conn, c.agent_id, file_id, rating_objs)
            issue_ids = []
            for rating in rubric.issues_from(rating_objs):
                existing = conn.execute(
                    "SELECT issue_id FROM issues WHERE file_id = ? AND state = 'open' "
                    "AND dimension = ? AND criterion = ?",
                    (file_id, rating.dimension, rating.criterion),
                ).fetchone()
                if existing is not None:
                    issue_ids.append(existing["issue_id"])
                    continue
                cur = conn.execute(
                    "INSERT INTO issues (run_id, file_id, opened_by_agent_id, title, body, "
                    "state, dimension, criterion) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                    (
                        c.run_id,
                        file_id,
                        c.agent_id,
                        _criterion_text(rating.dimension, rating.criterion),
                        rating.reason or "",
                        rating.dimension,
                        rating.criterion,
                    ),
                )
                issue_ids.append(cur.lastrowid)

        return {
            "review_id": review_id,
            "kind": kind,
            "scores": scores,
            "ok": ok,
            "reasons": reasons,
            "issues": issue_ids,
            "closed_issues": closed_ids,
        }

    def _close_resolved_issues(
        self, conn: sqlite3.Connection, closer: str, file_id: int, ratings: list[Rating]
    ) -> list[int]:
        resolved = {(r.dimension, r.criterion) for r in ratings if r.value >= 5}
        open_rows = conn.execute(
            "SELECT issue_id, dimension, criterion FROM issues "
            "WHERE file_id = ? AND state = 'open' AND dimension IS NOT NULL",
            (file_id,),
        ).fetchall()
        closed: list[int] = []
        for row in open_rows:
            if (row["dimension"], row["criterion"]) in resolved:
                conn.execute(
                    f"UPDATE issues SET state = 'closed', closed_by_agent_id = ?, "
                    f"resolution = 'rated 5 or higher in the Lead review', closed_at = {_NOW} "
                    "WHERE issue_id = ?",
                    (closer, row["issue_id"]),
                )
                closed.append(row["issue_id"])
        return closed

    def issue_close(self, caller: str, agent_id: str, issue_id: int, resolution: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown issue_id {issue_id!r}")
            if row["state"] != "open":
                raise LedgerError(f"issue {issue_id} is already {row['state']}")
            if c.role == "lead":
                file_row = conn.execute(
                    "SELECT module_id FROM files WHERE file_id = ?", (row["file_id"],)
                ).fetchone()
                if file_row is None or file_row["module_id"] != c.module_id:
                    raise LedgerError(f"{caller!r} is not the Lead of this file's module")
            else:
                require_role(c, "oracle", "manager")
            conn.execute(
                f"UPDATE issues SET state = 'closed', closed_by_agent_id = ?, resolution = ?, "
                f"closed_at = {_NOW} WHERE issue_id = ?",
                (c.agent_id, resolution, issue_id),
            )
        return dict(
            self.conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        )

    # -- Handoff ----------------------------------------------------------------

    def handoff_submit(
        self,
        caller: str,
        agent_id: str,
        file_id: int,
        open_issues: list[str],
        departures: list[str],
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder")
        file_row = self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        if file_row is None:
            raise LedgerError(f"unknown file_id {file_id!r}")
        if file_row["owner_agent_id"] != c.name:
            raise LedgerError(f"{caller!r} does not own file {file_id!r}")
        self._block_handoff_for_open_cr(self.conn, file_id)

        test_run_id = None
        if file_row["test_path"]:
            test_result = self.tests_run(caller, agent_id, "file", file_row["test_path"])
            if not test_result["ok"]:
                raise LedgerError(f"tests are not passing: {test_result['reason']}")
            test_run_id = test_result["test_run_id"]

        graph_ok, graph_reasons = graph.graph_current_for(self.repo_root, file_row["path"])
        if not graph_ok:
            raise LedgerError(
                f"code graph is not current for {file_row['path']}: {'; '.join(graph_reasons)}"
            )

        self_review = self.conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'self' "
            "ORDER BY review_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        stale_since = file_row["stale_since"]
        if self_review is None or (
            stale_since is not None and self_review["created_at"] <= stale_since
        ):
            raise LedgerError("self review is missing or older than the last edit")

        records_dir = ledger_path(self.repo_root).parent
        path_version = versions.save_version(
            records_dir, file_id, agent_id, self.repo_root / file_row["path"]
        )
        test_version = (
            versions.save_version(
                records_dir, file_id, agent_id, self.repo_root / file_row["test_path"]
            )
            if file_row["test_path"]
            else None
        )

        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO versions (file_id, agent_id, content, sha256, stored_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    file_id,
                    agent_id,
                    path_version["content"],
                    path_version["sha256"],
                    path_version["stored_path"],
                ),
            )
            version_id = cur.lastrowid
            test_version_id = None
            if test_version is not None:
                cur = conn.execute(
                    "INSERT INTO versions (file_id, agent_id, content, sha256, stored_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        file_id,
                        agent_id,
                        test_version["content"],
                        test_version["sha256"],
                        test_version["stored_path"],
                    ),
                )
                test_version_id = cur.lastrowid

            cur = conn.execute(
                "INSERT INTO handoffs (file_id, agent_id, test_run_id, version_id, "
                "test_version_id, self_review_id, open_issues_json, departures_json, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'submitted')",
                (
                    file_id,
                    agent_id,
                    test_run_id,
                    version_id,
                    test_version_id,
                    self_review["review_id"],
                    json.dumps(open_issues),
                    json.dumps(departures),
                ),
            )
            handoff_id = cur.lastrowid
            conn.execute(
                "UPDATE departures SET handoff_id = ? WHERE file_id = ? AND handoff_id IS NULL "
                "AND kind = 'departure' AND state = 'open' AND level = 'lead'",
                (handoff_id, file_id),
            )
            for departure_body in departures:
                conn.execute(
                    "INSERT INTO departures (run_id, agent_id, file_id, handoff_id, kind, "
                    "state, body, level) VALUES (?, ?, ?, ?, 'departure', 'open', ?, 'lead')",
                    (c.run_id, c.agent_id, file_id, handoff_id, departure_body),
                )

            conn.execute("UPDATE files SET state = 'handed_up' WHERE file_id = ?", (file_id,))

            agent_row = conn.execute(
                "SELECT state FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            prior_state = agent_row["state"] if agent_row is not None else None
            conn.execute("UPDATE agents SET state = 'handed_up' WHERE agent_id = ?", (agent_id,))
            self._log_event(conn, agent_id, prior_state, "handed_up", "handoff_submit")
            wakeup = self._owe_wakeup(
                conn,
                c,
                c.parent_agent_id,
                "handoff_submit",
                f"Handoff {handoff_id} for {file_row['path']} is waiting in the ledger.",
            )

        handoff = dict(
            self.conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        )
        handoff["next"] = self.next_step(wakeup)
        return handoff

    # -- Review -----------------------------------------------------------------

    def review_compare(self, caller: str, agent_id: str, handoff_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if handoff_row is None:
            raise LedgerError(f"unknown handoff_id {handoff_id!r}")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (handoff_row["file_id"],)
        ).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{caller!r} is not the Lead of this file's module")

        lead_review = self.conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' AND created_at >= ? "
            "ORDER BY review_id DESC LIMIT 1",
            (file_row["file_id"], handoff_row["created_at"]),
        ).fetchone()
        if lead_review is None:
            raise LedgerError("no lead review exists for this handoff yet")

        self_scores = (
            self._scores_for(handoff_row["self_review_id"])
            if handoff_row["self_review_id"] is not None
            else {}
        )
        lead_scores = self._scores_for(lead_review["review_id"])
        disagreements = rubric.disagreements(self_scores, lead_scores, self._thresholds())

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE handoffs SET compared_at = {_NOW} WHERE handoff_id = ?", (handoff_id,)
            )

        return {
            "handoff_id": handoff_id,
            "self_scores": self_scores,
            "lead_scores": lead_scores,
            "disagreements": disagreements,
        }

    def approve(
        self, caller: str, agent_id: str, handoff_id: int, notes: str | None = None
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if handoff_row is None:
            raise LedgerError(f"unknown handoff_id {handoff_id!r}")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (handoff_row["file_id"],)
        ).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{caller!r} is not the Lead of this file's module")
        if handoff_row["compared_at"] is None:
            raise LedgerError("review_compare has not run for this handoff")

        lead_review = self.conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' AND created_at >= ? "
            "ORDER BY review_id DESC LIMIT 1",
            (file_row["file_id"], handoff_row["created_at"]),
        ).fetchone()
        if lead_review is None:
            raise LedgerError("no lead review exists for this handoff")
        lead_scores = self._scores_for(lead_review["review_id"])
        ok, reasons = rubric.passes(
            lead_scores, self._ratings_for(lead_review["review_id"]), self._thresholds()
        )
        if not ok:
            raise LedgerError(f"the lead review does not pass: {'; '.join(reasons)}")

        open_issue = self.conn.execute(
            "SELECT 1 FROM issues WHERE file_id = ? AND state = 'open' LIMIT 1",
            (file_row["file_id"],),
        ).fetchone()
        if open_issue is not None:
            raise LedgerError("the file has an open issue")
        self._block_approve_for_cr(self.conn, file_row["file_id"])
        self._block_approve_for_departures(self.conn, handoff_id)

        with self._release_tx() as conn:
            conn.execute(
                f"UPDATE handoffs SET state = 'approved', decided_notes = ?, "
                f"decided_at = {_NOW}, decided_by = ? WHERE handoff_id = ?",
                (notes, c.agent_id, handoff_id),
            )
            conn.execute(
                f"UPDATE files SET state = 'approved', released_at = {_NOW} WHERE file_id = ?",
                (file_row["file_id"],),
            )
            self._mark_departures_reworked(conn, file_row["file_id"], handoff_id)
            self._release_agent(conn, handoff_row["agent_id"], "approve")
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM files WHERE module_id = ? "
                "AND state NOT IN ('approved', 'incomplete', 'superseded')",
                (file_row["module_id"],),
            ).fetchone()["n"]
            if pending == 0:
                conn.execute(
                    "UPDATE modules SET state = 'approved' WHERE module_id = ?",
                    (file_row["module_id"],),
                )

        return dict(
            self.conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        )

    def return_work(
        self, caller: str, agent_id: str, handoff_id: int, issues: list[str], targeted: list[str]
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if handoff_row is None:
            raise LedgerError(f"unknown handoff_id {handoff_id!r}")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (handoff_row["file_id"],)
        ).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{caller!r} is not the Lead of this file's module")

        self._block_return_for_open_departures(self.conn, handoff_id)

        next_round = self.conn.execute(
            "SELECT COALESCE(MAX(round), 0) + 1 AS n FROM attempts WHERE file_id = ?",
            (file_row["file_id"],),
        ).fetchone()["n"]

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE handoffs SET state = 'returned', decided_at = {_NOW}, decided_by = ? "
                "WHERE handoff_id = ?",
                (c.agent_id, handoff_id),
            )
            conn.execute(
                "UPDATE files SET state = 'returned' WHERE file_id = ?", (file_row["file_id"],)
            )
            coder_row = conn.execute(
                "SELECT state FROM agents WHERE agent_id = ?", (handoff_row["agent_id"],)
            ).fetchone()
            # 'idle', not 'working': the Coder's turn ended at its handoff, and the Stop
            # hook reads 'working' as a turn in progress. Its next tool use marks it working.
            if coder_row is not None:
                conn.execute(
                    "UPDATE agents SET state = 'idle' WHERE agent_id = ?",
                    (handoff_row["agent_id"],),
                )
                self._log_event(
                    conn, handoff_row["agent_id"], coder_row["state"], "idle", "return_work"
                )
            cur = conn.execute(
                "INSERT INTO attempts (file_id, handoff_id, issue_ids_json, targeted_json, "
                "round) VALUES (?, ?, ?, ?, ?)",
                (
                    file_row["file_id"],
                    handoff_id,
                    json.dumps(issues),
                    json.dumps(targeted),
                    next_round,
                ),
            )
            attempt_id = cur.lastrowid
            coder_name = conn.execute(
                "SELECT name FROM agents WHERE agent_id = ?", (handoff_row["agent_id"],)
            ).fetchone()
            pointer = f"Handoff {handoff_id} for {file_row['path']} is returned"
            if coder_name is not None:
                body = "\n".join(
                    [
                        f"{pointer} (fix round {next_round}).",
                        "Issues:",
                        *(f"- {issue}" for issue in issues),
                        f"Dimensions to move: {', '.join(targeted) or 'none named'}",
                    ]
                )
                conn.execute(
                    "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                    (c.run_id, c.name, coder_name["name"], body),
                )
            wakeup = self._owe_wakeup(
                conn,
                c,
                handoff_row["agent_id"],
                "return_work",
                f"{pointer}; read its issues with message_inbox and fix them.",
            )

        attempt = dict(
            self.conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        )
        attempt["next"] = self.next_step(wakeup)
        return attempt

    def attempt_record(self, caller: str, agent_id: str, file_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        file_row = self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{caller!r} is not the Lead of this file's module")

        lead_reviews = self.conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' "
            "ORDER BY review_id DESC LIMIT 2",
            (file_id,),
        ).fetchall()
        if len(lead_reviews) < 2:
            raise LedgerError("need two lead reviews to classify an attempt")
        after_review, before_review = lead_reviews[0], lead_reviews[1]
        after_scores = self._scores_for(after_review["review_id"])
        before_scores = self._scores_for(before_review["review_id"])

        last_attempt = self.conn.execute(
            "SELECT * FROM attempts WHERE file_id = ? ORDER BY attempt_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        if last_attempt is None:
            raise LedgerError("no attempt is pending for this file")
        targeted = set(json.loads(last_attempt["targeted_json"] or "[]"))

        outcome = rubric.classify(before_scores, after_scores, targeted, self._thresholds())

        open_issues = _rows(
            self.conn.execute(
                "SELECT * FROM issues WHERE file_id = ? AND state = 'open'", (file_id,)
            )
        )
        escalation = self.settings.escalation

        restore_from: sqlite3.Row | None = None
        if outcome == "regression":
            handoffs = self.conn.execute(
                "SELECT * FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 2",
                (file_id,),
            ).fetchall()
            if len(handoffs) < 2:
                raise LedgerError("no earlier handoff to restore for a regression")
            restore_from = handoffs[1]

        rounds: dict[int, int] = {}
        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE attempts SET outcome = ? WHERE attempt_id = ?",
                (outcome, last_attempt["attempt_id"]),
            )

            for issue in open_issues:
                if outcome not in ("plateau", "regression"):
                    rounds[issue["issue_id"]] = issue["round"]
                    continue
                new_attempts = issue["attempts"] + 1
                if (
                    new_attempts >= escalation.attempts_per_round
                    and (issue["round"] + 1) <= escalation.rounds
                ):
                    conn.execute(
                        "UPDATE issues SET round = ?, attempts = 0 WHERE issue_id = ?",
                        (issue["round"] + 1, issue["issue_id"]),
                    )
                    rounds[issue["issue_id"]] = issue["round"] + 1
                else:
                    conn.execute(
                        "UPDATE issues SET attempts = ? WHERE issue_id = ?",
                        (new_attempts, issue["issue_id"]),
                    )
                    rounds[issue["issue_id"]] = issue["round"]

            if restore_from is not None:
                records_dir = ledger_path(self.repo_root).parent
                prior_version = conn.execute(
                    "SELECT * FROM versions WHERE version_id = ?",
                    (restore_from["version_id"],),
                ).fetchone()
                prior_test_version = conn.execute(
                    "SELECT * FROM versions WHERE version_id = ?",
                    (restore_from["test_version_id"],),
                ).fetchone()
                if prior_version is not None and prior_version["stored_path"]:
                    versions.restore_version(
                        records_dir,
                        prior_version["stored_path"],
                        self.repo_root / file_row["path"],
                    )
                if (
                    prior_test_version is not None
                    and prior_test_version["stored_path"]
                    and file_row["test_path"]
                ):
                    versions.restore_version(
                        records_dir,
                        prior_test_version["stored_path"],
                        self.repo_root / file_row["test_path"],
                    )
                restored_agent_id = restore_from["agent_id"]
                if restored_agent_id is not None:
                    self._log_event(
                        conn,
                        restored_agent_id,
                        None,
                        "restored",
                        "attempt_record: regression restored "
                        f"version_id={restore_from['version_id']}",
                    )

        return {"outcome": outcome, "rounds": rounds}

    def accept_incomplete(self, caller: str, agent_id: str, handoff_id: int, reason: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if handoff_row is None:
            raise LedgerError(f"unknown handoff_id {handoff_id!r}")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (handoff_row["file_id"],)
        ).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{caller!r} is not the Lead of this file's module")
        self._block_approve_for_departures(self.conn, handoff_id)

        with self._release_tx() as conn:
            conn.execute(
                f"UPDATE handoffs SET state = 'incomplete', decided_notes = ?, "
                f"decided_at = {_NOW}, decided_by = ? WHERE handoff_id = ?",
                (reason, c.agent_id, handoff_id),
            )
            conn.execute(
                f"UPDATE files SET state = 'incomplete', released_at = {_NOW} WHERE file_id = ?",
                (file_row["file_id"],),
            )
            self._mark_departures_reworked(conn, file_row["file_id"], handoff_id)
            self._release_agent(conn, handoff_row["agent_id"], "accept_incomplete")

            cur = conn.execute(
                "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, state) "
                "VALUES (?, ?, ?, ?, 'open')",
                (c.run_id, file_row["file_id"], c.agent_id, reason),
            )
            deferral_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )

    # -- Agreements ---------------------------------------------------------------

    def deferral_propose(
        self, caller: str, agent_id: str, body: str, file_id: int | None = None
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            cur = conn.execute(
                "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, state) "
                "VALUES (?, ?, ?, ?, 'open')",
                (c.run_id, file_id, c.agent_id, body),
            )
            deferral_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )

    def agreement_decide(
        self,
        caller: str,
        agent_id: str,
        deferral_id: int,
        decision: Literal["agreed", "denied"],
        reason: str,
    ) -> dict:
        if decision not in ("agreed", "denied"):
            raise LedgerError(f"unknown decision {decision!r}")
        c = resolve(self.conn, caller, agent_id)
        row = self.conn.execute(
            "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown deferral_id {deferral_id!r}")

        proposer = self.conn.execute(
            "SELECT role FROM agents WHERE agent_id = ?", (row["proposed_by"],)
        ).fetchone()
        proposer_role = proposer["role"] if proposer is not None else "oracle"
        required_role = _PARENT_ROLE.get(proposer_role, "oracle")
        if _ROLE_RANK[c.role] < _ROLE_RANK[required_role]:
            raise LedgerError(
                f"role {c.role!r} may not decide a deferral proposed by a {proposer_role!r}"
            )

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE deferrals SET state = ?, decided_by = ?, decision_reason = ?, "
                f"decided_at = {_NOW} WHERE deferral_id = ?",
                (decision, c.agent_id, reason, deferral_id),
            )

        return dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )

    # -- Versions -----------------------------------------------------------------

    def version_restore(self, caller: str, agent_id: str, version_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder")
        version_row = self.conn.execute(
            "SELECT * FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if version_row is None:
            raise LedgerError(f"unknown version_id {version_id!r}")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (version_row["file_id"],)
        ).fetchone()
        if file_row is None or file_row["owner_agent_id"] != c.name:
            raise LedgerError(f"{caller!r} does not own the file for version {version_id!r}")

        target_path = self.repo_root / file_row["path"]
        if version_row["stored_path"]:
            records_dir = ledger_path(self.repo_root).parent
            versions.restore_version(records_dir, version_row["stored_path"], target_path)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(version_row["content"])

        with write_tx(self.conn) as conn:
            self._log_event(conn, agent_id, c.role, c.role, "version_restore")

        return {"file_id": file_row["file_id"], "path": file_row["path"], "version_id": version_id}

    # -- Reporting ------------------------------------------------------------------

    def status_tree(self, caller: str, agent_id: str) -> dict:
        conn = self.conn
        resolve(conn, caller, agent_id)
        run = self._active_run(conn)
        run_id = run["run_id"]

        phases = []
        for phase in conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall():
            modules = []
            for module in conn.execute(
                "SELECT * FROM modules WHERE phase_id = ? ORDER BY module_id",
                (phase["phase_id"],),
            ).fetchall():
                files = []
                for file_row in conn.execute(
                    "SELECT * FROM files WHERE module_id = ? ORDER BY file_id",
                    (module["module_id"],),
                ).fetchall():
                    latest_handoff = conn.execute(
                        "SELECT * FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
                        (file_row["file_id"],),
                    ).fetchone()
                    lead_review = conn.execute(
                        "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' "
                        "ORDER BY review_id DESC LIMIT 1",
                        (file_row["file_id"],),
                    ).fetchone()
                    files.append(
                        {
                            "file_id": file_row["file_id"],
                            "path": file_row["path"],
                            "owner": file_row["owner_agent_id"],
                            "state": file_row["state"],
                            "latest_handoff_state": (
                                latest_handoff["state"] if latest_handoff is not None else None
                            ),
                            "latest_lead_scores": (
                                self._scores_for(lead_review["review_id"])
                                if lead_review is not None
                                else {}
                            ),
                        }
                    )
                modules.append(
                    {
                        "module_id": module["module_id"],
                        "name": module["name"],
                        "state": module["state"],
                        "files": files,
                    }
                )
            phases.append(
                {
                    "phase_id": phase["phase_id"],
                    "name": phase["name"],
                    "state": phase["state"],
                    "modules": modules,
                }
            )

        agents = _rows(
            conn.execute(
                "SELECT agent_id, name, role, state, last_heartbeat_at FROM agents "
                "WHERE run_id = ? AND ended_at IS NULL ORDER BY started_at",
                (run_id,),
            )
        )

        return {"run": dict(run), "phases": phases, "agents": agents}

    def report_build(self, caller: str, agent_id: str) -> dict:
        conn = self.conn
        c = resolve(conn, caller, agent_id)
        require_role(c, "oracle")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        return self.write_report(c.run_id)

    def write_report(self, run_id: int) -> dict:
        conn = self.conn
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

        lines = ["# Run report", "", f"Outcome: {run['outcome'] or run['state']}"]
        if run["state"] == "paused":
            lines.append(f"Paused: {self.pause_reason(run_id)}")
        lines.append("")

        lines.append("## Repo")
        lines.append(f"- Branch: {run['branch'] or '(none)'}")
        lines.append(f"- Last repo_check: {run['repo_check_json'] or '(none)'}")
        lines.append("")

        for phase in conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall():
            lines.append(f"## Phase: {phase['name']} ({phase['state']})")
            for module in conn.execute(
                "SELECT * FROM modules WHERE phase_id = ? ORDER BY module_id",
                (phase["phase_id"],),
            ).fetchall():
                lines.append(f"### Module: {module['name']} ({module['state']})")
                for file_row in conn.execute(
                    "SELECT * FROM files WHERE module_id = ? ORDER BY file_id",
                    (module["module_id"],),
                ).fetchall():
                    lead_review = conn.execute(
                        "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' "
                        "ORDER BY review_id DESC LIMIT 1",
                        (file_row["file_id"],),
                    ).fetchone()
                    scores = (
                        self._scores_for(lead_review["review_id"])
                        if lead_review is not None
                        else {}
                    )
                    lines.append(f"- {file_row['path']} -- {file_row['state']} -- {scores}")
            lines.append("")

        lines.append("## Manager and Oracle reviews")
        for r in _rows(
            conn.execute(
                "SELECT r.* FROM reviews r JOIN phases p ON p.phase_id = r.phase_id "
                "WHERE p.run_id = ? AND r.kind IN ('manager', 'oracle') ORDER BY r.review_id",
                (run_id,),
            )
        ):
            scope = (
                f"module {r['module_id']}" if r["kind"] == "manager" else f"phase {r['phase_id']}"
            )
            lines.append(f"- [{r['kind']}] {scope} ({r['outcome']}): {r['notes']}")
        lines.append("")

        lines.append("## Returns and fix attempts")
        for a in _rows(
            conn.execute(
                "SELECT a.*, f.path, h.decided_by FROM attempts a "
                "JOIN files f ON f.file_id = a.file_id "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id "
                "LEFT JOIN handoffs h ON h.handoff_id = a.handoff_id "
                "WHERE p.run_id = ? ORDER BY a.attempt_id",
                (run_id,),
            )
        ):
            targeted = ", ".join(json.loads(a["targeted_json"] or "[]")) or "none named"
            lines.append(
                f"- {a['path']}, fix round {a['round']}, returned by "
                f"{_agent_name(conn, a['decided_by'])} -- targeted: {targeted} -- "
                f"outcome: {a['outcome'] or 'pending'}"
            )
            for issue in json.loads(a["issue_ids_json"] or "[]"):
                lines.append(f"  - {issue}")
        lines.append("")

        lines.append("## Open items")
        for d in _rows(
            conn.execute("SELECT * FROM deferrals WHERE run_id = ? AND state = 'open'", (run_id,))
        ):
            lines.append(f"- Deferral #{d['deferral_id']}: {d['reason']}")
        issues = _rows(conn.execute("SELECT * FROM issues WHERE run_id = ?", (run_id,)))
        for round_no in sorted({i["round"] for i in issues}):
            lines.append(f"- Round {round_no}:")
            for issue in (i for i in issues if i["round"] == round_no):
                detail = f" -- {issue['body']}" if issue["body"] else ""
                lines.append(
                    f"  - Issue #{issue['issue_id']} ({issue['state']}, "
                    f"{issue['attempts']} attempts): {issue['title']}{detail}"
                )
                if issue["resolution"]:
                    lines.append(f"    - Resolution: {issue['resolution']}")
        lines.append("")

        lines.append("## Departures")
        for dep in _rows(
            conn.execute(
                "SELECT d.*, f.path FROM departures d LEFT JOIN files f ON f.file_id = d.file_id "
                "WHERE d.run_id = ? AND d.kind = 'departure' ORDER BY d.departure_id",
                (run_id,),
            )
        ):
            where = f" on {dep['path']}" if dep["path"] else ""
            lines.append(
                f"- Departure #{dep['departure_id']}{where}, recorded by "
                f"{_agent_name(conn, dep['agent_id'])}: {dep['body']}"
            )
            for decision in _rows(
                conn.execute(
                    "SELECT * FROM departure_decisions WHERE departure_id = ? ORDER BY decision_id",
                    (dep["departure_id"],),
                )
            ):
                verb = "agreed" if decision["decision"] == "agree" else "pushed back"
                solution = f" -- solution: {decision['solution']}" if decision["solution"] else ""
                lines.append(
                    f"  - {decision['role']} {_agent_name(conn, decision['agent_id'])} {verb}: "
                    f"{decision['reason']}{solution}"
                )
            final = dep["state"]
            if dep["state"] == "reworked" and dep["reworked_by_handoff_id"] is not None:
                final = f"reworked, approved in handoff {dep['reworked_by_handoff_id']}"
            elif dep["level"]:
                final = f"{dep['state']}, waits on the {dep['level']}"
            lines.append(f"  - Final state: {final}")
        lines.append("")

        lines.append("## Shortfalls")
        for sf in _rows(
            conn.execute(
                "SELECT * FROM departures WHERE run_id = ? AND kind = 'shortfall' "
                "ORDER BY departure_id",
                (run_id,),
            )
        ):
            lines.append(f"- Shortfall #{sf['departure_id']}: {sf['body']}")
        lines.append("")

        lines.append("## Change requests")
        for cr in _rows(
            conn.execute("SELECT * FROM change_requests WHERE run_id = ? ORDER BY cr_id", (run_id,))
        ):
            lines.append(
                f"- CR #{cr['cr_id']} for {cr['path']} ({cr['state']}), from "
                f"{_agent_name(conn, cr['from_agent_id'])} to "
                f"{_agent_name(conn, cr['to_agent_id'])}: {cr['body']}"
            )
            if cr["decision_reason"]:
                lines.append(f"  - Decision: {cr['decision_reason']}")
            if cr["completion_notes"]:
                lines.append(f"  - Work done: {cr['completion_notes']}")
            if cr["evidence_test_run_id"] is not None:
                run = conn.execute(
                    "SELECT * FROM test_runs WHERE test_run_id = ?", (cr["evidence_test_run_id"],)
                ).fetchone()
                if run is not None:
                    lines.append(
                        f"  - Evidence: test run {run['test_run_id']} ({run['scope']}), "
                        f"{run['passed']} passed, {run['failed']} failed, exit {run['exit_code']}"
                    )
            if cr["verify_notes"]:
                lines.append(f"  - Verification: {cr['verify_notes']}")
        lines.append("")

        lines.append("## Overrides")
        for o in _rows(conn.execute("SELECT * FROM overrides WHERE run_id = ?", (run_id,))):
            lines.append(
                f"- {o['rule']} for {o['target_agent_name']} on {o['target']}: {o['reason']}"
            )
        lines.append("")

        lines.append("## Directives")
        for d in _rows(conn.execute("SELECT * FROM directives WHERE run_id = ?", (run_id,))):
            lines.append(f"- [{d['source']}] {d['body']} -> {d['outcome'] or d['state']}")
        lines.append("")

        lines.append("## Agents")
        agent_rows = conn.execute(
            "SELECT *, CAST((julianday(COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ', "
            "'now'))) - julianday(started_at)) * 86400000 AS INTEGER) AS elapsed_ms "
            "FROM agents WHERE run_id = ?",
            (run_id,),
        )
        agents = _rows(agent_rows)
        for a in agents:
            lines.append(
                f"- {a['name']} ({a['role']}, {a['model']}): "
                f"tokens in={a['input_tokens']} out={a['output_tokens']} "
                f"cache_read={a['cache_read_tokens']} cache_write={a['cache_write_tokens']}, "
                f"elapsed_ms={a['elapsed_ms']}, tool_uses={a['tool_uses']}, "
                f"context_overflow_count={a['context_overflow_count']}"
            )
        totals = {
            column: sum(a[column] or 0 for a in agents)
            for column in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        }
        lines.append(
            f"- Run total: tokens in={totals['input_tokens']} out={totals['output_tokens']} "
            f"cache_read={totals['cache_read_tokens']} "
            f"cache_write={totals['cache_write_tokens']}"
        )

        text = "\n".join(lines) + "\n"
        records_dir = ledger_path(self.repo_root).parent
        records_dir.mkdir(parents=True, exist_ok=True)
        report_path = records_dir / "report.md"
        report_path.write_text(text, encoding="utf-8", newline="\n")

        return {"path": str(report_path), "text": text}

    def analytics_query(self, caller: str, agent_id: str, sql: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        stripped = sql.strip().rstrip(";")
        if ";" in stripped or not _SELECT_RE.match(stripped):
            raise LedgerError("only a single SELECT statement is allowed")

        db_path = ledger_path(self.repo_root)
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(stripped)
            rows = cursor.fetchmany(500)
            return {"rows": [dict(row) for row in rows]}
        except sqlite3.Error as exc:
            raise LedgerError(f"query failed: {exc}") from exc
        finally:
            conn.close()
