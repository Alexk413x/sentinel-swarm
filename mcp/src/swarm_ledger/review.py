from __future__ import annotations

import json
import re
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import graph, notify, pricing, rubric, versions
from .db import ledger_path, write_tx
from .drive import findings_named, open_findings
from .identity import ROLES, Caller, LedgerError, require_role, resolve
from .rubric import Rating
from .settings import Settings
from .testing import TestResult, build_command, run_tests, summarize_output, tree_fingerprint

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
_DEFERRAL_LEVEL = {
    "file": "lead",
    "module": "lead",
    "cross_module": "manager",
    "phase": "manager",
    "plan": "oracle",
    "prd": "oracle",
}
DEFERRAL_KINDS = tuple(_DEFERRAL_LEVEL)


def _agent_cost(agent: dict) -> float | None:
    if agent.get("cost_usd") is not None:
        return agent["cost_usd"]
    return pricing.estimate(agent["model"], agent)


def _run_cost(agents: list[dict]) -> float | None:
    costs = [_agent_cost(a) for a in agents]
    return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.2f}"


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration(start: str | None, end: str | None) -> str:
    begin = _stamp(start)
    if begin is None:
        return "not started"
    finish = _stamp(end)
    seconds = int(((finish or datetime.now(timezone.utc)) - begin).total_seconds())
    hours, rest = divmod(seconds, 3600)
    text = f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m {rest % 60}s"
    return text if finish is not None else f"{text} so far"


def _format_ms(ms: int | None) -> str:
    seconds = int((ms or 0) / 1000)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m {rest % 60}s"


def _repo_line(raw: str | None) -> str:
    if not raw:
        return "(none)"
    check = json.loads(raw)
    tree = "clean" if check.get("clean") else f"dirty: {', '.join(check.get('dirty_paths') or [])}"
    parts = [
        f"on {check.get('branch')}",
        tree,
        f"base {check.get('base_branch')}",
        f"{check.get('ahead_of_base')} ahead, {check.get('behind_base')} behind",
    ]
    if check.get("advice"):
        parts.append(f"advice: {check['advice']}")
    return "; ".join(parts)


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


_RUN_FIELDS = ("run_id", "state", "outcome", "branch", "plugin_version", "started_at")


def run_summary(run: sqlite3.Row | dict, include_prd: bool = False) -> dict:
    row = dict(run)
    summary = {key: row.get(key) for key in _RUN_FIELDS}
    if row.get("repo_check_json"):
        summary["repo_check"] = json.loads(row["repo_check_json"])
    if include_prd:
        summary["prd"] = row.get("prd")
    return summary


def agent_names(conn: sqlite3.Connection, run_id: int) -> dict[str, str]:
    return {
        row["agent_id"]: row["name"]
        for row in conn.execute("SELECT agent_id, name FROM agents WHERE run_id = ?", (run_id,))
    }


def live_agents(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    names = agent_names(conn, run_id)
    rows = conn.execute(
        "SELECT name, role, state, current_activity, parent_agent_id, model, effort, "
        "session_name, last_heartbeat_at FROM agents "
        "WHERE run_id = ? AND ended_at IS NULL ORDER BY started_at",
        (run_id,),
    ).fetchall()
    agents = []
    for row in rows:
        agent = dict(row)
        agent["parent"] = names.get(agent.pop("parent_agent_id"))
        agents.append({key: value for key, value in agent.items() if value is not None})
    return agents


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

    def adopt_run_profile(self, run_id: int | None) -> None: ...

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

    def _issue_chain(self, conn: sqlite3.Connection, issue: sqlite3.Row) -> list[sqlite3.Row]: ...

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

    def _file_at_last_round(self, file_id: int) -> bool:
        # attempts.round is a per-file return counter, unlike the per-issue issues.round,
        # so a dimension with no open issue still needs a file-level measure: the full
        # escalation budget, rounds * attempts_per_round.
        escalation = self.settings.escalation
        budget = escalation.rounds * escalation.attempts_per_round
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            "WHERE file_id = ? AND (outcome IS NULL OR outcome != 'improved')",
            (file_id,),
        ).fetchone()
        return row is not None and row["n"] >= budget

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
        conn.execute(
            f"UPDATE drive_requests SET state = 'abandoned', done_at = {_NOW} "
            "WHERE agent_id = ? AND state = 'open'",
            (target_agent_id,),
        )
        if row["bg_id"] and row["role"] != "oracle":
            self._pending_stops.append((target_agent_id, row["bg_id"]))

    # -- Tests --------------------------------------------------------------

    def tests_run(
        self,
        caller: str,
        agent_id: str,
        scope: Literal["file", "module", "phase", "full"],
        target: str | None = None,
        force: bool = False,
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

        self.adopt_run_profile(c.run_id)
        if not self.settings.test_command:
            raise LedgerError("no test command in the profile")

        claimed = [
            path
            for row in self.conn.execute(
                "SELECT f.path, f.test_path FROM files f "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ?",
                (c.run_id,),
            )
            for path in (row["path"], row["test_path"])
            if path
        ]
        fingerprint = tree_fingerprint(self.repo_root, claimed)
        earlier = None if force else self._reusable_run(c.run_id, target, fingerprint)
        if earlier is not None:
            result = TestResult(
                command=earlier["command"],
                exit_code=earlier["exit_code"],
                passed=earlier["passed"],
                failed=earlier["failed"],
                skipped=earlier["skipped"],
                errors=0,
                output=earlier["output"] or "",
                duration_ms=0,
                ok=True,
                reason=None,
            )
        else:
            result = run_tests(self.settings.test_command, target, self.repo_root)

        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
                "passed, failed, skipped, output, fingerprint, reused_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    fingerprint,
                    earlier["test_run_id"] if earlier is not None else None,
                ),
            )
            test_run_id = cur.lastrowid

        reuse = (
            {"reused": True, "reused_from": earlier["test_run_id"]}
            if earlier is not None
            else {"reused": False}
        )
        return {
            **reuse,
            "test_run_id": test_run_id,
            "command": result.command,
            "exit_code": result.exit_code,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "errors": result.errors,
            "output": summarize_output(result.output, result.ok),
            "output_chars": len(result.output),
            "duration_ms": result.duration_ms,
            "ok": result.ok,
            "reason": result.reason,
        }

    def _reusable_run(
        self, run_id: int | None, target: str | None, fingerprint: str | None
    ) -> sqlite3.Row | None:
        if fingerprint is None or not self.settings.test_command:
            return None
        command = build_command(self.settings.test_command, target)
        for row in self.conn.execute(
            "SELECT * FROM test_runs WHERE run_id = ? AND command = ? AND fingerprint = ? "
            "AND reused_from IS NULL ORDER BY test_run_id DESC",
            (run_id, command, fingerprint),
        ):
            if (
                row["exit_code"] == 0
                and (row["failed"] or 0) == 0
                and (row["skipped"] or 0) == 0
                and (row["passed"] or 0) >= 1
            ):
                return row
        return None

    def test_run_get(self, caller: str, agent_id: str, test_run_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        row = self.conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ? AND run_id = ?",
            (test_run_id, c.run_id),
        ).fetchone()
        if row is None:
            raise LedgerError(f"no test run {test_run_id} in this run")
        return dict(row)

    # -- Code graph -----------------------------------------------------------

    def graph_upsert(self, caller: str, agent_id: str, nodes: list[dict]) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder", "lead")
        file_of: dict[str, int] = {}
        if c.role == "lead":
            for row in self.conn.execute(
                "SELECT file_id, path, test_path FROM files WHERE module_id = ? "
                "AND state NOT IN ('released', 'superseded')",
                (c.module_id,),
            ):
                for p in (row["path"], row["test_path"]):
                    if p:
                        file_of[p] = row["file_id"]
            allowed_paths = set(file_of)
            refusal = "a Lead updates only nodes that anchor on its own module's files"
        else:
            if c.file_id is None:
                raise LedgerError(f"{caller!r} has no claimed file")
            file_row = self.conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (c.file_id,)
            ).fetchone()
            if file_row is None:
                raise LedgerError(f"{caller!r} has no claimed file")
            allowed_paths = {file_row["path"], file_row["test_path"]}
            refusal = (
                "a Coder updates only the nodes that anchor on its own file; ask your Lead "
                "to update a node that spans files"
            )

        for node in nodes:
            anchored_files: set[int] = set()
            for anchor in node.get("anchors") or []:
                if isinstance(anchor, str):
                    anchor_path = anchor.split("#", 1)[0].strip()
                elif isinstance(anchor, dict):
                    anchor_path = str(anchor.get("path", "")).strip()
                else:
                    raise LedgerError(f"anchor {anchor!r} must be a string or an object")
                if anchor_path not in allowed_paths:
                    raise LedgerError(refusal)
                if anchor_path in file_of:
                    anchored_files.add(file_of[anchor_path])
            if c.role == "lead" and len(anchored_files) < 2:
                raise LedgerError(
                    "a Lead updates only nodes that span two or more of its module's files; "
                    "a single-file node belongs to that file's Coder"
                )

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
        self._require_brief_reread(c, file_id)

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

    def _require_brief_reread(self, c: Caller, file_id: int) -> None:
        returned = self.conn.execute(
            "SELECT MAX(decided_at) AS at FROM handoffs WHERE file_id = ? AND state = 'returned'",
            (file_id,),
        ).fetchone()["at"]
        if returned is None:
            return
        brief = self.conn.execute(
            "SELECT last_read_by_child_at FROM briefs WHERE child_name = ? "
            "ORDER BY created_at DESC, brief_id DESC LIMIT 1",
            (c.name,),
        ).fetchone()
        read_at = brief["last_read_by_child_at"] if brief is not None else None
        if read_at is None or read_at < returned:
            raise LedgerError(
                f"re-read your brief before this handoff: call brief_get(caller_name={c.name!r}, "
                f"child_name={c.name!r}). The work came back at {returned}, and the brief, not "
                "your memory of it, is the task"
            )

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
        ratings = self._ratings_for(lead_review["review_id"])
        t = self._thresholds()
        ok, reasons = rubric.passes(lead_scores, ratings, t)
        floor_dimensions: list[str] = []
        if not ok:
            at_or_above_floor = all(score >= t.floor for score in lead_scores.values())
            no_criterion_below_floor = all(r.value >= t.criterion_floor for r in ratings)
            if (
                at_or_above_floor
                and no_criterion_below_floor
                and self._file_at_last_round(file_row["file_id"])
            ):
                floor_dimensions = sorted(d for d, score in lead_scores.items() if score < t.target)
            else:
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
                f"decided_at = {_NOW}, decided_by = ?, floor_pass_json = ? WHERE handoff_id = ?",
                (
                    notes,
                    c.agent_id,
                    json.dumps(floor_dimensions) if floor_dimensions else None,
                    handoff_id,
                ),
            )
            conn.execute(
                f"UPDATE files SET state = 'approved', released_at = {_NOW} WHERE file_id = ?",
                (file_row["file_id"],),
            )
            for dimension in floor_dimensions:
                conn.execute(
                    "INSERT INTO departures (run_id, agent_id, file_id, kind, state, body) "
                    "VALUES (?, ?, ?, 'shortfall', 'recorded', ?)",
                    (
                        c.run_id,
                        c.agent_id,
                        file_row["file_id"],
                        f"Floor pass: {dimension} scored {lead_scores[dimension]} (target "
                        f"{t.target}, floor {t.floor}) after the file's last escalation round.",
                    ),
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

        result = dict(
            self.conn.execute(
                "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
            ).fetchone()
        )
        if floor_dimensions:
            result["floor_pass_dimensions"] = floor_dimensions
        return result

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

    def _round_advance_target(
        self, conn: sqlite3.Connection, issue: dict, next_round: int
    ) -> sqlite3.Row | None:
        issue_row = conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue["issue_id"],)
        ).fetchone()
        if issue_row is None:
            return None
        chain = self._issue_chain(conn, issue_row)
        if not chain:
            return None
        target_role = "manager" if next_round == 2 else "oracle"
        return next((a for a in chain if a["role"] == target_role), chain[-1])

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

        kept_review = before_review if outcome == "regression" else after_review
        kept_scores = before_scores if outcome == "regression" else after_scores
        kept_ratings = self._ratings_for(kept_review["review_id"])

        rounds: dict[int, int] = {}
        escalated: list[dict] = []
        notices: list[dict] = []
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
                next_round = issue["round"] + 1
                if (
                    new_attempts >= escalation.attempts_per_round
                    and next_round <= escalation.rounds
                ):
                    target = self._round_advance_target(conn, issue, next_round)
                    conn.execute(
                        "UPDATE issues SET round = ?, attempts = 0, escalated_to = ? "
                        "WHERE issue_id = ?",
                        (
                            next_round,
                            target["name"] if target is not None else None,
                            issue["issue_id"],
                        ),
                    )
                    rounds[issue["issue_id"]] = next_round
                    if target is not None:
                        pointer = (
                            f"Issue {issue['issue_id']} for {file_row['path']} moved to round "
                            f"{next_round}; read it with issue_list."
                        )
                        conn.execute(
                            "INSERT INTO messages (run_id, from_name, to_name, body) "
                            "VALUES (?, ?, ?, ?)",
                            (c.run_id, c.name, target["name"], pointer),
                        )
                        wakeup = self._owe_wakeup(
                            conn, c, target["agent_id"], "attempt_record", pointer
                        )
                        escalated.append(
                            {
                                "issue_id": issue["issue_id"],
                                "round": next_round,
                                "escalated_to": target["name"],
                                "next": self.next_step(wakeup),
                            }
                        )
                else:
                    conn.execute(
                        "UPDATE issues SET attempts = ? WHERE issue_id = ?",
                        (new_attempts, issue["issue_id"]),
                    )
                    rounds[issue["issue_id"]] = issue["round"]
                    if (
                        issue["round"] >= escalation.rounds
                        and new_attempts >= escalation.attempts_per_round
                    ):
                        notice = self._notify_last_round_below_floor(
                            conn, issue, file_row["path"], kept_scores, kept_ratings
                        )
                        if notice is not None:
                            notices.append(notice)

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

        for notice in notices:
            notify.deliver(notice, self.settings.notify)
        return {
            "outcome": outcome,
            "rounds": rounds,
            "escalated": escalated,
            "notifications": notices,
        }

    def _notify_last_round_below_floor(
        self,
        conn: sqlite3.Connection,
        issue: dict,
        path: str,
        scores: dict[str, float],
        ratings: list[Rating],
    ) -> dict | None:
        t = self._thresholds()
        dimension, criterion = issue["dimension"], issue["criterion"]
        if dimension is not None:
            low_scores = (
                {dimension: scores[dimension]} if scores.get(dimension, 100) < t.floor else {}
            )
            low_ratings = [
                r
                for r in ratings
                if r.dimension == dimension
                and (criterion is None or r.criterion == criterion)
                and r.value < t.criterion_floor
            ]
        else:
            low_scores = {d: s for d, s in scores.items() if s < t.floor}
            low_ratings = [r for r in ratings if r.value < t.criterion_floor]
        if not low_scores and not low_ratings:
            return None
        below = [f"{d} {s:g} (floor {t.floor})" for d, s in sorted(low_scores.items())] + [
            f"{r.dimension}.{r.criterion} {r.value} (floor {t.criterion_floor})"
            for r in low_ratings
        ]
        return notify.record(
            conn,
            issue["run_id"],
            kind="error",
            event_key=f"issue:{issue['issue_id']}",
            message=(
                f"Issue {issue['issue_id']} on {path} ended round {issue['round']} below the "
                f"floor: {issue['title']}; {', '.join(below)}"
            ),
            channels=self.settings.notify,
        )

    def accept_incomplete(self, caller: str, agent_id: str, handoff_id: int, reason: str) -> dict:
        if not reason.strip():
            raise LedgerError("accept_incomplete needs a reason: why the work stops short")
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
            raise LedgerError(
                "review_compare has not run for this handoff; score it blind with "
                "score_record(kind='lead') and compare before you accept less"
            )
        open_issues = json.loads(handoff_row["open_issues_json"] or "[]")
        issue_ids = [
            row["issue_id"]
            for row in self.conn.execute(
                "SELECT issue_id FROM issues WHERE file_id = ? AND state = 'open' "
                "ORDER BY issue_id",
                (file_row["file_id"],),
            )
        ]
        if not open_issues and not issue_ids:
            raise LedgerError(
                "nobody reported this work as incomplete: the handoff lists no open issue and "
                "the file has no open issue. Return the work, or approve it"
            )
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
                "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, kind, state, "
                "open_issues_json, issue_ids_json) VALUES (?, ?, ?, ?, 'file', 'open', ?, ?)",
                (
                    c.run_id,
                    file_row["file_id"],
                    c.agent_id,
                    reason,
                    json.dumps(open_issues),
                    json.dumps(issue_ids),
                ),
            )
            deferral_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )

    # -- Agreements ---------------------------------------------------------------

    def deferral_propose(
        self,
        caller: str,
        agent_id: str,
        body: str,
        file_id: int | None = None,
        *,
        kind: str,
        parties: list[str] | None = None,
    ) -> dict:
        if kind not in DEFERRAL_KINDS:
            raise LedgerError(
                f"unknown deferral kind {kind!r}; use one of {list(DEFERRAL_KINDS)}: file "
                "(a file's task or tests), module (a module's scope or a contract between its "
                "files), cross_module (that, when another module is affected), phase (a phase's "
                "scope or a contract between modules), plan (the phase plan, or work moved to a "
                "later phase), prd (what the PRD asks for)"
            )
        parties = parties or []
        wakeup = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            arbiter = self._arbiter(conn, c, parties) if parties else None
            cur = conn.execute(
                "INSERT INTO deferrals (run_id, file_id, proposed_by, reason, kind, parties_json, "
                "arbiter_agent_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (
                    c.run_id,
                    file_id,
                    c.agent_id,
                    body,
                    kind,
                    json.dumps(parties) if parties else None,
                    arbiter["agent_id"] if arbiter is not None else None,
                ),
            )
            deferral_id = cur.lastrowid
            if arbiter is not None:
                wakeup = self._owe_wakeup(
                    conn,
                    c,
                    arbiter["agent_id"],
                    "deferral_propose",
                    f"Dispute {deferral_id} between {c.name} and {', '.join(parties)} waits for "
                    "your decision; read it with status_tree and decide it with agreement_decide.",
                )

        result = dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )
        if arbiter is not None:
            result["arbiter"] = arbiter["name"]
            result["next"] = self.next_step(wakeup)
        return result

    def _arbiter(self, conn: sqlite3.Connection, c: Caller, parties: list[str]) -> sqlite3.Row:
        members = [c.agent_id]
        for name in parties:
            row = conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND name = ? "
                "ORDER BY ended_at IS NOT NULL, started_at DESC LIMIT 1",
                (c.run_id, name),
            ).fetchone()
            if row is None:
                raise LedgerError(f"no agent named {name!r} in this run")
            if row["agent_id"] == c.agent_id:
                raise LedgerError("a dispute names the other side, not the proposer")
            members.append(row["agent_id"])
        chains = [self._ancestors(conn, member) for member in members]
        shared = set.intersection(*(set(chain) for chain in chains))
        arbiter_id = next((agent_id for agent_id in chains[0] if agent_id in shared), None)
        if arbiter_id is None:
            raise LedgerError("the parties share no ancestor in this run")
        return conn.execute("SELECT * FROM agents WHERE agent_id = ?", (arbiter_id,)).fetchone()

    def _ancestors(self, conn: sqlite3.Connection, agent_id: str) -> list[str]:
        chain: list[str] = []
        row = conn.execute(
            "SELECT parent_agent_id FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        parent_id = row["parent_agent_id"] if row is not None else None
        while parent_id is not None and parent_id not in chain:
            chain.append(parent_id)
            row = conn.execute(
                "SELECT parent_agent_id FROM agents WHERE agent_id = ?", (parent_id,)
            ).fetchone()
            parent_id = row["parent_agent_id"] if row is not None else None
        return chain

    def _may_arbitrate(self, conn: sqlite3.Connection, c: Caller, arbiter_id: str) -> bool:
        arbiter = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (arbiter_id,)).fetchone()
        if arbiter is None:
            return False
        if arbiter["agent_id"] == c.agent_id:
            return True
        # A resumed Oracle has a new agent_id under the same name.
        return arbiter["ended_at"] is not None and (arbiter["name"], arbiter["role"]) == (
            c.name,
            c.role,
        )

    def agreement_decide(
        self,
        caller: str,
        agent_id: str,
        deferral_id: int,
        decision: Literal["agreed", "denied"],
        reason: str,
        directive_id: int | None = None,
    ) -> dict:
        if decision not in ("agreed", "denied"):
            raise LedgerError(f"unknown decision {decision!r}")
        c = resolve(self.conn, caller, agent_id)
        row = self.conn.execute(
            "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown deferral_id {deferral_id!r}")
        kind = row["kind"] or "file"

        if row["arbiter_agent_id"] is not None:
            if not self._may_arbitrate(self.conn, c, row["arbiter_agent_id"]):
                raise LedgerError(
                    f"deferral {deferral_id} is a dispute between "
                    f"{_agent_name(self.conn, row['proposed_by'])} and "
                    f"{', '.join(json.loads(row['parties_json'] or '[]'))}; only their closest "
                    f"shared ancestor, {_agent_name(self.conn, row['arbiter_agent_id'])}, "
                    "decides it"
                )
        else:
            proposer = self.conn.execute(
                "SELECT role FROM agents WHERE agent_id = ?", (row["proposed_by"],)
            ).fetchone()
            proposer_role = proposer["role"] if proposer is not None else "oracle"
            parent_role = _PARENT_ROLE.get(proposer_role, "oracle")
            kind_role = _DEFERRAL_LEVEL[kind]
            required_role = max(parent_role, kind_role, key=_ROLE_RANK.__getitem__)
            if _ROLE_RANK.get(c.role, -1) < _ROLE_RANK[required_role]:
                raise LedgerError(
                    f"a {kind} deferral proposed by a {proposer_role} is decided by the "
                    f"{required_role} or above, not a {c.role}. A decider who sees the kind "
                    "is too low denies it with that reason, and the proposer files it again"
                )
            module_id, phase_id = self._deferral_scope(row)
            in_scope = c.run_id == row["run_id"] and (
                c.role == "oracle"
                or (c.role == "manager" and c.phase_id == phase_id)
                or (c.role == "lead" and c.module_id == module_id)
            )
            if not in_scope:
                raise LedgerError(
                    f"deferral {deferral_id} is outside the {c.role} scope of {caller!r}; "
                    "the Lead of its module, the Manager of its phase, or the Oracle decides it"
                )
        if kind == "prd":
            self._require_user_directive(row, directive_id)

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE deferrals SET state = ?, decided_by = ?, decision_reason = ?, "
                f"directive_id = ?, decided_at = {_NOW} WHERE deferral_id = ?",
                (decision, c.agent_id, reason, directive_id, deferral_id),
            )

        return dict(
            self.conn.execute(
                "SELECT * FROM deferrals WHERE deferral_id = ?", (deferral_id,)
            ).fetchone()
        )

    def _require_user_directive(self, row: sqlite3.Row, directive_id: int | None) -> None:
        directive = self.conn.execute(
            "SELECT 1 FROM directives WHERE directive_id = ? AND run_id = ? "
            "AND source = 'user_chat' AND created_at >= ?",
            (directive_id, row["run_id"], row["created_at"]),
        ).fetchone()
        if directive is None:
            raise LedgerError(
                f"deferral {row['deferral_id']} changes what the PRD asks for, which the user "
                "decides: ask the user, record the answer with directive_submit(source="
                "'user_chat', ...), and pass that directive_id"
            )

    def _deferral_scope(self, row: sqlite3.Row) -> tuple[int | None, int | None]:
        if row["file_id"] is not None:
            scope = self.conn.execute(
                "SELECT m.module_id, m.phase_id FROM files f "
                "JOIN modules m ON m.module_id = f.module_id WHERE f.file_id = ?",
                (row["file_id"],),
            ).fetchone()
            if scope is not None:
                return scope["module_id"], scope["phase_id"]
        agent = self.conn.execute(
            "SELECT module_id, phase_id FROM agents WHERE agent_id = ?", (row["proposed_by"],)
        ).fetchone()
        if agent is None:
            return None, None
        return agent["module_id"], agent["phase_id"]

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

    def status_tree(self, caller: str, agent_id: str, include_prd: bool = False) -> dict:
        conn = self.conn
        resolve(conn, caller, agent_id)
        run = self._active_run(conn)
        run_id = run["run_id"]
        owner_names = agent_names(conn, run_id)

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
                            "owner": owner_names.get(file_row["owner_agent_id"]),
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

        return {
            "run": run_summary(run, include_prd),
            "phases": phases,
            "agents": live_agents(conn, run_id),
            "open_findings": open_findings(conn, run_id),
            "fixes": self._fixes(conn, run_id),
        }

    def _fixes(self, conn: sqlite3.Connection, run_id: int) -> list[dict]:
        return [
            {
                "brief_id": b["brief_id"],
                "child_name": b["child_name"],
                "child_role": b["child_role"],
                "findings": findings_named(conn, json.loads(b["finding_ids_json"])),
            }
            for b in conn.execute(
                "SELECT brief_id, child_name, child_role, finding_ids_json FROM briefs "
                "WHERE run_id = ? AND finding_ids_json IS NOT NULL ORDER BY brief_id",
                (run_id,),
            )
        ]

    def report_build(self, caller: str, agent_id: str) -> dict:
        conn = self.conn
        c = resolve(conn, caller, agent_id)
        require_role(c, "oracle")
        if c.run_id is None:
            raise LedgerError(f"{caller!r} has no run")
        return self.write_report(c.run_id)

    def _phase_work_start(self, phase: sqlite3.Row) -> str | None:
        row = self.conn.execute(
            "SELECT MIN(started_at) AS at FROM agents WHERE phase_id = ? AND role = 'manager'",
            (phase["phase_id"],),
        ).fetchone()
        return row["at"] if row is not None and row["at"] else phase["started_at"]

    def write_report(self, run_id: int) -> dict:
        conn = self.conn
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

        lines = ["# Run report", "", f"Outcome: {run['outcome'] or run['state']}"]
        lines.append(f"Duration: {_duration(run['started_at'], run['ended_at'])}")
        if run["state"] == "paused":
            lines.append(f"Paused: {self.pause_reason(run_id)}")
        lines.append("")

        lines.append("## Repo")
        lines.append(f"- Branch: {run['branch'] or '(none)'}")
        lines.append(f"- Last repo_check: {_repo_line(run['repo_check_json'])}")
        lines.append("")

        agents = _rows(
            conn.execute(
                "SELECT *, CAST((julianday(COALESCE(ended_at, strftime('%Y-%m-%dT%H:%M:%fZ', "
                "'now'))) - julianday(started_at)) * 86400000 AS INTEGER) AS elapsed_ms "
                "FROM agents WHERE run_id = ?",
                (run_id,),
            )
        )

        for phase in conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall():
            lines.append(
                f"## Phase: {phase['name']} ({phase['state']}, "
                f"{_duration(self._phase_work_start(phase), phase['ended_at'])})"
            )
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
                    floor_note = ""
                    approved_handoff = conn.execute(
                        "SELECT floor_pass_json FROM handoffs WHERE file_id = ? "
                        "AND state = 'approved' ORDER BY handoff_id DESC LIMIT 1",
                        (file_row["file_id"],),
                    ).fetchone()
                    if approved_handoff is not None and approved_handoff["floor_pass_json"]:
                        dims = ", ".join(json.loads(approved_handoff["floor_pass_json"]))
                        floor_note = (
                            f" -- floor pass: {dims} below target but at or above the floor"
                        )
                    lines.append(
                        f"- {file_row['path']} -- {file_row['state']} -- {scores}{floor_note}"
                    )
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
            details = json.loads(r["details_json"] or "{}")
            review_scores = details.get("review_scores") or []
            if review_scores:
                parts = ", ".join(
                    f"{s['dimension']}={s['value']}"
                    + (f" ({s['reason']})" if s.get("reason") else "")
                    for s in review_scores
                )
                lines.append(f"  - Scores: {parts}")
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

        lines.append("## Explorations")
        drive_requests = _rows(
            conn.execute(
                "SELECT * FROM drive_requests WHERE run_id = ? ORDER BY ordinal", (run_id,)
            )
        )
        if not drive_requests:
            lines.append("- No exploration ran.")
        for req in drive_requests:
            lines.append(
                f"### Exploration {req['ordinal']} (request {req['request_id']}, "
                f"{req['state']}): {req['focus']}"
            )
            findings = _rows(
                conn.execute(
                    "SELECT * FROM drive_findings WHERE request_id = ? ORDER BY finding_id",
                    (req["request_id"],),
                )
            )
            if not findings:
                lines.append("- No finding.")
                continue
            later_fingerprints = {
                row["fingerprint"]
                for row in conn.execute(
                    "SELECT DISTINCT df.fingerprint FROM drive_findings df "
                    "JOIN drive_requests dr ON dr.request_id = df.request_id "
                    "WHERE dr.run_id = ? AND dr.ordinal > ?",
                    (run_id, req["ordinal"]),
                )
            }
            for f in findings:
                fix = (
                    "still open in a later exploration"
                    if f["fingerprint"] in later_fingerprints
                    else "not seen again"
                )
                lines.append(
                    f"- [{f['severity']}] finding {f['finding_id']}: {f['title']} "
                    f"({f['fingerprint']}), area {f['area'] or 'unknown'} -- {fix}"
                )
        fixes = self._fixes(conn, run_id)
        if fixes:
            lines.append("### Fixes")
        for fix in fixes:
            named = "; ".join(f"finding {f['finding_id']} {f['title']}" for f in fix["findings"])
            lines.append(f"- {fix['child_name']} ({fix['child_role']}): {named}")
        stop_directives = _rows(
            conn.execute(
                "SELECT * FROM directives WHERE run_id = ? AND source = 'driver' "
                "AND substr(body, 1, 13) = '[driver-stop]' ORDER BY directive_id",
                (run_id,),
            )
        )
        if stop_directives:
            lines.append("### Stop rules")
        for d in stop_directives:
            head, *detail = d["body"].removeprefix("[driver-stop] ").splitlines()
            lines.append(f"- {head} -> {d['outcome'] or d['state']}")
            lines.extend(f"  - {line.removeprefix('- ')}" for line in detail)
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

        lines.append("## Decided deferrals")
        for d in _rows(
            conn.execute(
                "SELECT d.*, f.path FROM deferrals d LEFT JOIN files f ON f.file_id = d.file_id "
                "WHERE d.run_id = ? AND d.state IN ('agreed', 'denied') ORDER BY d.deferral_id",
                (run_id,),
            )
        ):
            where = f" on {d['path']}" if d["path"] else ""
            lines.append(
                f"- Deferral #{d['deferral_id']}{where}: {d['reason']} -- proposed by "
                f"{_agent_name(conn, d['proposed_by'])}, decided by "
                f"{_agent_name(conn, d['decided_by'])} ({d['state']}): {d['decision_reason']}"
            )
        lines.append("")

        lines.append("## Disputes")
        for d in _rows(
            conn.execute(
                "SELECT * FROM deferrals WHERE run_id = ? AND arbiter_agent_id IS NOT NULL "
                "ORDER BY deferral_id",
                (run_id,),
            )
        ):
            parties = ", ".join(json.loads(d["parties_json"] or "[]"))
            outcome = (
                f"{d['state']} by {_agent_name(conn, d['decided_by'])}: {d['decision_reason']}"
                if d["state"] != "open"
                else "open"
            )
            lines.append(
                f"- Dispute #{d['deferral_id']} ({d['kind']}) between "
                f"{_agent_name(conn, d['proposed_by'])} and {parties}, arbiter "
                f"{_agent_name(conn, d['arbiter_agent_id'])}: {d['reason']} -> {outcome}"
            )
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

        lines.append("## Graph gaps")
        gaps = _rows(
            conn.execute("SELECT * FROM graph_gaps WHERE run_id = ? ORDER BY gap_id", (run_id,))
        )
        anchored = graph.anchored_paths(
            self.repo_root, sorted({g["path"] for g in gaps if g["path"]})
        )
        for gap in gaps:
            found = json.loads(gap["results_json"] or "[]")
            scope = f" in {gap['path']}" if gap["path"] else ""
            mapped = " (the graph anchors this file)" if gap["path"] in anchored else ""
            lines.append(
                f"- {_agent_name(conn, gap['agent_id'])} ran {gap['tool']} "
                f"{gap['pattern']!r}{scope}{mapped}: {len(found)} path(s)"
                + (f", {', '.join(found[:5])}" if found else "")
            )
        lines.append("")

        lines.append("## Directives")
        for d in _rows(conn.execute("SELECT * FROM directives WHERE run_id = ?", (run_id,))):
            head, *rest = d["body"].splitlines() or [""]
            lines.append(f"- [{d['source']}] {head} -> {d['outcome'] or d['state']}")
            if d["source"] != "driver":
                lines.extend(f"  {line}" for line in rest)
        lines.append("")

        lines.append("## Notifications to the user")
        notified = _rows(
            conn.execute(
                "SELECT * FROM directives WHERE run_id = ? AND question IS NOT NULL "
                "ORDER BY directive_id",
                (run_id,),
            )
        )
        for d in notified:
            lines.append(f"- Directive #{d['directive_id']} ({d['source']}): {d['question']}")
            for reply in _rows(
                conn.execute(
                    "SELECT * FROM directives WHERE reply_to = ? ORDER BY directive_id",
                    (d["directive_id"],),
                )
            ):
                sender = reply["sender_name"] or reply["source"]
                lines.append(f"  - Reply from {sender}: {reply['body']}")
        for n in _rows(
            conn.execute(
                "SELECT * FROM notifications WHERE run_id = ? ORDER BY notification_id",
                (run_id,),
            )
        ):
            lines.append(f"- [{n['kind']}] {n['message']} ({n['created_at']})")
        lines.append("")

        lines.append("## Final test run")
        final_run = conn.execute(
            "SELECT * FROM test_runs WHERE run_id = ? AND scope = 'full' "
            "ORDER BY test_run_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if final_run is None:
            lines.append("- No full-suite test run is recorded.")
        else:
            lines.append(
                f"- {final_run['passed']} passed, {final_run['failed']} failed, "
                f"{final_run['skipped']} skipped, exit {final_run['exit_code']}"
            )
        lines.append("")

        lines.append("## Measures")
        lines.append("### Time per stage")
        for phase in conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall():
            lines.append(
                f"- Phase {phase['name']} working time: "
                f"{_duration(self._phase_work_start(phase), phase['ended_at'])}"
            )
        role_ms: dict[str, int] = dict.fromkeys(ROLES, 0)
        for a in agents:
            if a["role"] in role_ms:
                role_ms[a["role"]] += a["elapsed_ms"] or 0
        for role, ms in role_ms.items():
            lines.append(f"- {role} agent time total: {_format_ms(ms)}")
        lines.append("")

        lines.append("### Returns per file")
        returns = _rows(
            conn.execute(
                "SELECT f.path, COUNT(*) AS n FROM attempts a "
                "JOIN files f ON f.file_id = a.file_id "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id "
                "WHERE p.run_id = ? GROUP BY f.path ORDER BY f.path",
                (run_id,),
            )
        )
        if not returns:
            lines.append("- No file was returned.")
        for r in returns:
            lines.append(f"- {r['path']}: {r['n']} return(s)")
        lines.append("")

        lines.append("### Cost per phase")
        phase_agents: dict[int, list[dict]] = {}
        oracle_agents: list[dict] = []
        for a in agents:
            if a["role"] == "oracle":
                oracle_agents.append(a)
            elif a["phase_id"] is not None:
                phase_agents.setdefault(a["phase_id"], []).append(a)
        for phase in conn.execute(
            "SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ).fetchall():
            lines.append(
                f"- Phase {phase['name']}: "
                f"{_money(_run_cost(phase_agents.get(phase['phase_id'], [])))}"
            )
        lines.append(f"- Oracle (run total): {_money(_run_cost(oracle_agents))}")
        lines.append("")

        lines.append("## Agents")
        for a in agents:
            lines.append(
                f"- {a['name']} ({a['role']}, {a['model']}): "
                f"tokens in={a['input_tokens']} out={a['output_tokens']} "
                f"cache_read={a['cache_read_tokens']} cache_write={a['cache_write_tokens']}, "
                f"elapsed_ms={a['elapsed_ms']}, tool_uses={a['tool_uses']}, "
                f"context_overflow_count={a['context_overflow_count']}, "
                f"cost {_money(_agent_cost(a))}"
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
            f"cache_write={totals['cache_write_tokens']}, "
            f"cost {_money(_run_cost(agents))}"
        )
        lines.append(
            "- Costs are at list prices, from each response's usage in the agent's transcript."
        )

        text = "\n".join(lines) + "\n"
        records_dir = ledger_path(self.repo_root).parent
        records_dir.mkdir(parents=True, exist_ok=True)
        per_run_path = records_dir / f"report-{run_id}.md"
        with per_run_path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        report_path = records_dir / "report.md"
        with report_path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(text)

        return {"path": str(report_path), "per_run_path": str(per_run_path), "text": text}

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
