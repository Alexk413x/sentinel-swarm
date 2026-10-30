from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

from . import graph, notify, rubric, settle, testrun, versions
from .agreements import test_run_passed, work_scope
from .db import NOW, dict_rows, in_clause, ledger_path, open_readonly, row_dict, write_tx
from .drive import findings_named, open_findings
from .host import LedgerHost
from .identity import (
    ROLE_RANK,
    Caller,
    LedgerError,
    ledger_errors,
    parent_chain,
    require_role,
    resolve,
)
from .queries import logical_state
from .rubric import Rating
from .settings import command_line
from .testing import build_command, summarize_output, tree_fingerprint

_SCOPE_ROLE = {"file": "coder", "module": "lead", "phase": "manager", "full": "oracle"}
_PARENT_ROLE = {"manager": "oracle", "lead": "manager", "coder": "lead"}
_DEFERRAL_LEVEL = {
    "file": "lead",
    "module": "lead",
    "cross_module": "manager",
    "phase": "manager",
    "plan": "oracle",
    "prd": "oracle",
}
DEFERRAL_KINDS = tuple(_DEFERRAL_LEVEL)
INLINE_WAIT_S = 20.0
_WAIT_POLL_S = 0.2

_SELECT_RE = re.compile(r"(?is)^\s*select\b")

_CRITERION_TEXT: dict[tuple[str, str], str] = {
    (dimension_key, criterion_key): criterion_text
    for dimension_key, _, criteria in rubric.DIMENSIONS
    for criterion_key, criterion_text in criteria
}


def agent_name(conn: sqlite3.Connection, agent_id: str | None) -> str:
    if agent_id is None:
        return "unknown"
    row = conn.execute("SELECT name FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return row["name"] if row is not None else agent_id


def _anchor_key(anchor: object) -> tuple[str, str]:
    if isinstance(anchor, str):
        path, _, symbol = anchor.strip().strip("`").strip().partition("#")
        return path.strip().replace("\\", "/"), symbol.strip()
    if isinstance(anchor, dict):
        path = str(anchor.get("path") or "").strip().replace("\\", "/")
        return path, str(anchor.get("symbol") or "").strip()
    raise LedgerError(f"anchor {anchor!r} must be a string or an object")


def _lead_may_upsert(
    file_of: dict[str, int], claimed: set[str], touched: set[str], after: set[str]
) -> None:
    foreign = sorted(p for p in touched if p not in file_of and p in claimed)
    if foreign:
        raise LedgerError(
            "a Lead updates only nodes whose anchors lie on its own module's files or on files "
            f"no module claims; {', '.join(foreign)} belongs to another module's claim"
        )
    if all(p in file_of for p in after) and len({file_of[p] for p in after}) < 2:
        raise LedgerError(
            "a Lead updates only nodes that span two or more files; a node on one of its "
            "module's files belongs to that file's Coder"
        )


def _criterion_text(dimension: str, criterion: str) -> str:
    return _CRITERION_TEXT.get((dimension, criterion), f"{dimension}.{criterion}")


ISSUE_OPEN_ROUND = {"manager": 2, "oracle": 3}


def escalation_target(chain: list[sqlite3.Row], next_round: int) -> sqlite3.Row:
    target_role = "manager" if next_round == ISSUE_OPEN_ROUND["manager"] else "oracle"
    return next((a for a in chain if a["role"] == target_role), chain[-1])


def latest_lead_review(
    conn: sqlite3.Connection, file_id: int, since: str | None = None
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reviews WHERE file_id = ? AND kind = 'lead' "
        "AND (? IS NULL OR created_at >= ?) ORDER BY review_id DESC LIMIT 1",
        (file_id, since, since),
    ).fetchone()


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


def fix_briefs(conn: sqlite3.Connection, run_id: int) -> list[dict]:
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


def test_run_result(row: sqlite3.Row) -> dict:
    ok = test_run_passed(row)
    output = row["output"] or ""
    result = {
        "reused": row["reused_from"] is not None,
        "test_run_id": row["test_run_id"],
        "state": row["state"],
        "scope": row["scope"],
        "target": row["target"],
        "command": row["command"],
        "exit_code": row["exit_code"],
        "passed": row["passed"],
        "failed": row["failed"],
        "skipped": row["skipped"],
        "allowed_skipped": row["allowed_skipped"],
        "errors": row["errors"],
        "output": summarize_output(output, ok),
        "output_chars": len(output),
        "duration_ms": row["duration_ms"],
        "ok": ok,
        "reason": None if ok else row["reason"],
    }
    if row["reused_from"] is not None:
        result["reused_from"] = row["reused_from"]
    if row["state"] == "running":
        result["reason"] = "still running"
        result["next"] = (
            f"The suite runs in its own process under the ledger server, bounded by "
            f"test_timeout_s ({row['timeout_s']} s). End your turn: a wake-up names the result "
            f"once it is recorded. test_run_get(test_run_id={row['test_run_id']}) reads it."
        )
    return result


def _insert_version(
    conn: sqlite3.Connection, file_id: int, agent_id: str, saved: dict
) -> int | None:
    cur = conn.execute(
        "INSERT INTO versions (file_id, agent_id, content, sha256, stored_path, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            file_id,
            agent_id,
            saved["content"],
            saved["sha256"],
            saved["stored_path"],
            saved["deleted"],
        ),
    )
    return cur.lastrowid


def _attach_departures(
    conn: sqlite3.Connection, c: Caller, file_id: int, handoff_id: int | None, bodies: list[str]
) -> None:
    conn.execute(
        "UPDATE departures SET handoff_id = ? WHERE file_id = ? AND handoff_id IS NULL "
        "AND kind = 'departure' AND state = 'open' AND level = 'lead'",
        (handoff_id, file_id),
    )
    for body in bodies:
        conn.execute(
            "INSERT INTO departures (run_id, agent_id, file_id, handoff_id, kind, "
            "state, body, level) VALUES (?, ?, ?, ?, 'departure', 'open', ?, 'lead')",
            (c.run_id, c.agent_id, file_id, handoff_id, body),
        )


def _supersede_handoffs(
    conn: sqlite3.Connection, file_id: int, handoff_id: int | None
) -> tuple[list[int], list[dict]]:
    earlier = [
        row["handoff_id"]
        for row in conn.execute(
            "SELECT handoff_id FROM handoffs WHERE file_id = ? AND state = 'submitted' "
            "AND handoff_id != ? ORDER BY handoff_id",
            (file_id, handoff_id),
        )
    ]
    if not earlier:
        return [], []
    marks = in_clause(earlier)
    conn.execute(
        "UPDATE handoffs SET state = 'superseded', decided_notes = ? "
        f"WHERE handoff_id IN ({marks})",
        (f"Superseded by handoff {handoff_id}.", *earlier),
    )
    open_departures = dict_rows(
        conn.execute(
            "SELECT departure_id, body FROM departures WHERE kind = 'departure' "
            f"AND state = 'open' AND level = 'lead' AND handoff_id IN ({marks}) "
            "ORDER BY departure_id",
            earlier,
        )
    )
    conn.execute(
        "UPDATE departures SET state = 'superseded', level = NULL, "
        f"decided_at = {NOW}, decision_reason = ? "
        "WHERE kind = 'departure' AND state = 'open' AND level = 'lead' "
        f"AND handoff_id IN ({marks})",
        (f"Handoff {handoff_id} replaced the handoff this departure was recorded on.", *earlier),
    )
    return earlier, open_departures


def live_agents(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    names = agent_names(conn, run_id)
    rows = conn.execute(
        "SELECT name, role, state, current_activity, parent_agent_id, model, effort, "
        "session_name, last_heartbeat_at, park_reason FROM agents "
        "WHERE run_id = ? AND ended_at IS NULL ORDER BY started_at",
        (run_id,),
    ).fetchall()
    agents = []
    for row in rows:
        agent = dict(row)
        agent["parent"] = names.get(agent.pop("parent_agent_id"))
        if agent["state"] == "parked":
            agent["session"] = "closed"
        agents.append({key: value for key, value in agent.items() if value is not None})
    return agents


class ReviewMixin(LedgerHost):
    # -- Shared helpers ---------------------------------------------------

    def _lead_file(self, c: Caller, file_id: int) -> sqlite3.Row:
        file_row = self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        if file_row is None or c.module_id != file_row["module_id"]:
            raise LedgerError(f"{c.name!r} is not the Lead of this file's module")
        return file_row

    def _lead_handoff(self, c: Caller, handoff_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        handoff_row = self.conn.execute(
            "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
        ).fetchone()
        if handoff_row is None:
            raise LedgerError(f"unknown handoff_id {handoff_id!r}")
        if handoff_row["state"] == "superseded":
            raise LedgerError(
                f"handoff {handoff_id} is superseded by a later handoff of the file; "
                "review that one"
            )
        return handoff_row, self._lead_file(c, handoff_row["file_id"])

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
        return row["n"] >= budget

    def _release_agent(self, conn: sqlite3.Connection, target_agent_id: str, reason: str) -> None:
        row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (target_agent_id,)).fetchone()
        if row is None or row["state"] == "released":
            return
        phase_name = _phase_name_at(conn, row["phase_id"])
        conn.execute(
            f"UPDATE agents SET state = 'released', ended_at = {NOW}, phase_at_end = ?, "
            "state_before_park = NULL WHERE agent_id = ?",
            (phase_name, target_agent_id),
        )
        self._log_event(conn, target_agent_id, row["state"], "released", reason)
        self._reassign_crs_of(conn, row)
        conn.execute(
            f"UPDATE drive_requests SET state = 'abandoned', done_at = {NOW} "
            "WHERE agent_id = ? AND state = 'open'",
            (target_agent_id,),
        )
        conn.execute(
            f"UPDATE file_waits SET closed_at = {NOW} WHERE agent_id = ? AND closed_at IS NULL",
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
        include_slow: bool = False,
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
        if scope == "full" and target is None:
            target = self.settings.full_target
        command = self.test_command_for(scope, target, include_slow)
        testrun.sweep(self.conn)
        claimed = self._claimed_paths(c.run_id)
        fingerprint = tree_fingerprint(self.repo_root, claimed)
        if not force:
            earlier = self._reusable_run(c.run_id, command, fingerprint)
            if earlier is not None:
                return self._record_reuse(c, scope, target, earlier)
            running = self.conn.execute(
                "SELECT test_run_id FROM test_runs WHERE run_id = ? AND agent_id = ? "
                "AND command = ? AND fingerprint IS ? AND state = 'running' "
                "ORDER BY test_run_id DESC LIMIT 1",
                (c.run_id, c.agent_id, command, fingerprint),
            ).fetchone()
            if running is not None:
                return self._await_run(running["test_run_id"])

        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO test_runs (run_id, agent_id, scope, target, command, fingerprint, "
                "state, skip_patterns_json, claimed_json, timeout_s) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
                (
                    c.run_id,
                    c.agent_id,
                    scope,
                    target,
                    command,
                    fingerprint,
                    json.dumps(self.settings.allowed_skips),
                    json.dumps(sorted(set(claimed))),
                    self.settings.test_timeout_s,
                ),
            )
            test_run_id = int(cur.lastrowid or 0)
        self._launch_run(test_run_id)
        return self._await_run(test_run_id)

    def test_command_for(self, scope: str, target: str | None, include_slow: bool = False) -> str:
        template = self.settings.test_template(scope)
        if not template:
            raise LedgerError("no test command in the profile")
        if scope == "full" and target is None:
            target = self.settings.full_target
            if target is None and "{target}" in template:
                raise LedgerError(
                    "a full run needs a target, and full_target is not set: set full_target in "
                    ".claude/sentinel-swarm.local.md to the folder that holds the tests, such "
                    "as tests, or pass target"
                )
        return build_command(template, target, self.settings.slow_tests, include_slow)

    def _launch_run(self, test_run_id: int) -> None:
        try:
            pid = testrun.launcher(self.repo_root, self.db_path, test_run_id)
        except OSError as exc:
            testrun.abandon(self.conn, test_run_id, f"the test runner did not start: {exc}")
            return
        if pid is not None:
            with write_tx(self.conn) as conn:
                conn.execute(
                    "UPDATE test_runs SET runner_pid = ? WHERE test_run_id = ? "
                    "AND state = 'running'",
                    (pid, test_run_id),
                )

    def _await_run(self, test_run_id: int) -> dict:
        deadline = time.monotonic() + INLINE_WAIT_S
        while True:
            row = self.conn.execute(
                "SELECT * FROM test_runs WHERE test_run_id = ?", (test_run_id,)
            ).fetchone()
            if row["state"] != "running" or time.monotonic() >= deadline:
                break
            time.sleep(_WAIT_POLL_S)
        if row["state"] != "running":
            self._take_test_wakeup(row)
        return self._test_result(row)

    def _take_test_wakeup(self, row: sqlite3.Row) -> None:
        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE wakeups SET sent_at = {NOW} WHERE from_agent_id = ? AND to_agent_id = ? "
                "AND reason = ? AND sent_at IS NULL AND pointer = ?",
                (
                    row["agent_id"],
                    row["agent_id"],
                    testrun.WAKE_REASON,
                    testrun.wake_pointer(row),
                ),
            )

    def _record_reuse(
        self, c: Caller, scope: str, target: str | None, earlier: sqlite3.Row
    ) -> dict:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
                "passed, failed, skipped, errors, allowed_skipped, output, fingerprint, "
                "reused_from, state, duration_ms, finished_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', 0, {NOW})",
                (
                    c.run_id,
                    c.agent_id,
                    scope,
                    target,
                    earlier["command"],
                    earlier["exit_code"],
                    earlier["passed"],
                    earlier["failed"],
                    earlier["skipped"],
                    earlier["errors"],
                    earlier["allowed_skipped"],
                    earlier["output"],
                    earlier["fingerprint"],
                    earlier["test_run_id"],
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ?", (cur.lastrowid,)
        ).fetchone()
        return self._test_result(row)

    def _test_result(self, row: sqlite3.Row) -> dict:
        result = test_run_result(row)
        if row["state"] != "running" and not result["ok"]:
            named = self._named_commands(row["output"] or "")
            if named:
                result["commands"] = named
        return result

    def _named_commands(self, output: str) -> list[dict[str, str]]:
        return [
            {"name": command["name"], "command": command_line(command)}
            for command in self.settings.commands
            if command["name"] in output or command["run"] in output
        ]

    def _claimed_paths(self, run_id: int | None) -> list[str]:
        return [
            path
            for row in self.conn.execute(
                "SELECT f.path, f.test_path FROM files f "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ?",
                (run_id,),
            )
            for path in (row["path"], row["test_path"])
            if path
        ]

    def _reusable_run(
        self, run_id: int | None, command: str, fingerprint: str | None
    ) -> sqlite3.Row | None:
        if fingerprint is None:
            return None
        for row in self.conn.execute(
            "SELECT * FROM test_runs WHERE run_id = ? AND command = ? AND fingerprint = ? "
            "AND reused_from IS NULL AND state = 'done' ORDER BY test_run_id DESC",
            (run_id, command, fingerprint),
        ):
            if test_run_passed(row):
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
        result = {**dict(row), "ok": test_run_passed(row)}
        named = self._test_result(row).get("commands")
        if named:
            result["commands"] = named
        return result

    # -- Code graph -----------------------------------------------------------

    def graph_upsert(self, caller: str, agent_id: str, nodes: list[dict]) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder", "lead", "oracle")
        claims = self._live_claims(c.run_id) if c.role != "coder" else set()
        own: set[str] = set()
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
        elif c.role == "coder":
            if c.file_id is None:
                raise LedgerError(f"{caller!r} has no claimed file")
            file_row = self.conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (c.file_id,)
            ).fetchone()
            if file_row is None:
                raise LedgerError(f"{caller!r} has no claimed file")
            own = {p for p in (file_row["path"], file_row["test_path"]) if p}

        for node in nodes:
            node_id = str(node.get("id"))
            supplied = [_anchor_key(a) for a in node.get("anchors") or []]
            stored = graph.node_anchors(self.repo_root, node_id)
            after = supplied if "anchors" in node else stored
            touched = {p for p, _ in (*supplied, *stored)}
            if c.role == "coder":
                self._coder_may_upsert(node, own, supplied, stored, touched)
            elif c.role == "lead":
                _lead_may_upsert(file_of, claims, touched, {p for p, _ in after})
            else:
                owned = sorted(p for p in touched if p in claims)
                if owned:
                    raise LedgerError(
                        f"the Oracle updates only nodes that no module owns; {node_id!r} anchors "
                        f"{', '.join(owned)}, which a module claims. That module's Lead updates it"
                    )

        result = graph.graph_upsert(self.repo_root, nodes)

        with write_tx(self.conn) as conn:
            row = conn.execute(
                "SELECT state FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            state = row["state"] if row is not None else None
            self._log_event(conn, agent_id, state, state or "working", "graph_upsert")

        return result

    def _live_claims(self, run_id: int | None) -> set[str]:
        claimed: set[str] = set()
        for row in self.conn.execute(
            "SELECT files.path, files.test_path FROM files "
            "JOIN modules ON modules.module_id = files.module_id "
            "JOIN phases ON phases.phase_id = modules.phase_id "
            "WHERE phases.run_id = ? AND files.released_at IS NULL "
            "AND files.state NOT IN ('released', 'superseded')",
            (run_id,),
        ):
            claimed.update(p for p in (row["path"], row["test_path"]) if p)
        return claimed

    def _coder_may_upsert(
        self,
        node: dict,
        own: set[str],
        supplied: list[tuple[str, str]],
        stored: list[tuple[str, str]],
        touched: set[str],
    ) -> None:
        if touched <= own:
            return
        refusal = (
            "a Coder updates only the nodes that anchor on its own file; ask your Lead to "
            "update a node that spans files. On a node that also anchors other files, a Coder "
            "may only add anchors on its own file, or re-record its baseline, with a node "
            'that carries just "id" and "anchors": every anchor the node has, plus its new ones'
        )
        if set(node) - {"id", "anchors"}:
            raise LedgerError(refusal)
        if "anchors" in node:
            if not set(stored) <= set(supplied):
                raise LedgerError(refusal)
            if any(path not in own for path, _ in set(supplied) - set(stored)):
                raise LedgerError(refusal)
        after = {p for p, _ in (supplied if "anchors" in node else stored)}
        if not after & own:
            raise LedgerError(refusal)
        stale = graph.stale_files(self.repo_root, sorted(after - own))
        if stale:
            raise LedgerError(
                f"node {node.get('id')!r} also anchors {', '.join(stale)}, which changed since "
                "its baseline and is not your file; a write would re-record that baseline too. "
                "Ask your Lead to update the node"
            )

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

        with ledger_errors():
            rating_objs = [rubric.parse_rating(r) for r in ratings]
        if not isinstance(applicable, dict):
            raise LedgerError(f"applicable must be an object. {rubric.schema_help()}")
        with ledger_errors():
            rubric.validate_ratings(rating_objs, applicable)

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
            subject_agent_id = self._lead_review_subject(c, caller, file_row, handoff_row)

        scores = rubric.dimension_scores(rating_objs)
        ok, reasons = rubric.passes(scores, rating_objs, self.settings.rubric)
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
            issue_ids = [
                self._open_rating_issue(conn, c, file_id, rating)
                for rating in rubric.issues_from(rating_objs)
            ]

        return {
            "review_id": review_id,
            "kind": kind,
            "scores": scores,
            "ok": ok,
            "reasons": reasons,
            "issues": issue_ids,
            "closed_issues": closed_ids,
        }

    def _lead_review_subject(
        self, c: Caller, caller: str, file_row: sqlite3.Row, handoff_row: sqlite3.Row | None
    ) -> str | None:
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
        return owner_row["agent_id"] if owner_row is not None else None

    def _open_rating_issue(
        self, conn: sqlite3.Connection, c: Caller, file_id: int, rating: Rating
    ) -> int | None:
        existing = conn.execute(
            "SELECT issue_id FROM issues WHERE file_id = ? AND state = 'open' "
            "AND dimension = ? AND criterion = ?",
            (file_id, rating.dimension, rating.criterion),
        ).fetchone()
        if existing is not None:
            return existing["issue_id"]
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
        return cur.lastrowid

    def _close_resolved_issues(
        self, conn: sqlite3.Connection, closer: str, file_id: int, ratings: list[Rating]
    ) -> list[int]:
        resolved = {
            (r.dimension, r.criterion) for r in ratings if r.value >= rubric.ISSUE_RESOLVED_AT
        }
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
                    f"resolution = ?, closed_at = {NOW} WHERE issue_id = ?",
                    (
                        closer,
                        f"rated {rubric.ISSUE_RESOLVED_AT} or higher in the Lead review",
                        row["issue_id"],
                    ),
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
                f"closed_at = {NOW} WHERE issue_id = ?",
                (c.agent_id, resolution, issue_id),
            )
        return row_dict(self.conn, "issues", "issue_id", issue_id)

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
        test_run_id, self_review = self._require_handoff_evidence(caller, agent_id, file_row)

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
        product_versions = [
            (
                product["file_id"],
                versions.save_version(
                    records_dir, product["file_id"], agent_id, self.repo_root / product["path"]
                ),
            )
            for product in self._products(file_id)
        ]

        with write_tx(self.conn) as conn:
            version_id = _insert_version(conn, file_id, agent_id, path_version)
            test_version_id = (
                _insert_version(conn, file_id, agent_id, test_version)
                if test_version is not None
                else None
            )
            product_version_ids = [
                _insert_version(conn, product_id, agent_id, saved)
                for product_id, saved in product_versions
            ]
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
            superseded_ids, closed_departures = _supersede_handoffs(conn, file_id, handoff_id)
            _attach_departures(conn, c, file_id, handoff_id, departures)
            conn.execute("UPDATE files SET state = 'handed_up' WHERE file_id = ?", (file_id,))
            settle.sync_products(conn, file_id, "handed_up")

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

        handoff = row_dict(self.conn, "handoffs", "handoff_id", handoff_id)
        if product_version_ids:
            handoff["product_version_ids"] = product_version_ids
        if path_version["deleted"]:
            handoff["deleted"] = True
        if superseded_ids:
            handoff["superseded_handoff_ids"] = superseded_ids
        if closed_departures:
            handoff["superseded_departures"] = closed_departures
            handoff["superseded_departures_note"] = (
                "These open departures were recorded on a handoff this one replaced, and are "
                "closed. Record each one that still applies with departure_record."
            )
        handoff["next"] = self.next_step(wakeup)
        return handoff

    def _require_handoff_evidence(
        self, caller: str, agent_id: str, file_row: sqlite3.Row
    ) -> tuple[int | None, sqlite3.Row]:
        test_run_id = None
        deleted = not (self.repo_root / file_row["path"]).exists()
        test_path = file_row["test_path"]
        if test_path and not (deleted and not (self.repo_root / test_path).exists()):
            test_result = self.tests_run(caller, agent_id, "file", test_path)
            if test_result["state"] == "running":
                raise LedgerError(
                    f"test run {test_result['test_run_id']} of {test_path} is still "
                    "running. End your turn; its wake-up names the result, and handoff_submit "
                    "then uses it"
                )
            if not test_result["ok"]:
                raise LedgerError(f"tests are not passing: {test_result['reason']}")
            test_run_id = test_result["test_run_id"]

        if deleted:
            if graph.anchored_paths(self.repo_root, [file_row["path"]]):
                raise LedgerError(
                    f"{file_row['path']} is deleted, but the code graph still anchors nodes on "
                    "it. Find them with kg_find_by_path, then call graph_upsert for each with "
                    "anchors that leave the deleted path out"
                )
        else:
            graph_ok, graph_reasons = graph.graph_current_for(self.repo_root, file_row["path"])
            if not graph_ok:
                raise LedgerError(
                    f"code graph is not current for {file_row['path']}: {'; '.join(graph_reasons)}"
                )
        paths = [p for p in (file_row["path"], file_row["test_path"]) if p]
        stale = graph.stale_files(self.repo_root, paths)
        if stale:
            detail = "; ".join(f"{p} (nodes {', '.join(ids)})" for p, ids in stale.items())
            raise LedgerError(
                f"code graph is stale: changed since its baseline: {detail}. Re-read each node "
                "against its file, then call graph_upsert for it again; the call records the "
                "file's current content as the baseline. A node that spans files is your "
                "Lead's to update"
            )

        self_review = self.conn.execute(
            "SELECT * FROM reviews WHERE file_id = ? AND kind = 'self' "
            "ORDER BY review_id DESC LIMIT 1",
            (file_row["file_id"],),
        ).fetchone()
        stale_since = file_row["stale_since"]
        if self_review is None or (
            stale_since is not None and self_review["created_at"] <= stale_since
        ):
            raise LedgerError("self review is missing or older than the last edit")
        return test_run_id, self_review

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

    def _products(self, file_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE source_file_id = ? AND released_at IS NULL ORDER BY file_id",
            (file_id,),
        ).fetchall()

    def file_delete(self, caller: str, agent_id: str, path: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "coder")
        file_row = self.conn.execute(
            "SELECT * FROM files WHERE file_id = ? AND released_at IS NULL", (c.file_id,)
        ).fetchone()
        owned = (
            {
                file_row["path"],
                file_row["test_path"],
                *(p["path"] for p in self._products(file_row["file_id"])),
            }
            if file_row is not None
            else set()
        )
        if path not in owned:
            raise LedgerError(
                f"{caller!r} may delete only its own claimed file, test file, or products, "
                f"not {path!r}"
            )
        target = self.repo_root / path
        if not target.is_file():
            raise LedgerError(f"{path!r} is not a file on disk")
        target.unlink()
        assert file_row is not None
        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE files SET stale_since = {NOW} WHERE file_id = ?", (file_row["file_id"],)
            )
            row = conn.execute(
                "SELECT state FROM agents WHERE agent_id = ?", (c.agent_id,)
            ).fetchone()
            state = row["state"] if row is not None else None
            self._log_event(conn, c.agent_id, state, state or "working", f"file_delete: {path}")
        return {
            "file_id": file_row["file_id"],
            "path": path,
            "deleted": True,
            "next": (
                "handoff_submit records the deletion as the file's version, and the Lead "
                "reviews it like an edit. Score the file with score_record(kind='self') first."
            ),
        }

    # -- Review -----------------------------------------------------------------

    def review_compare(self, caller: str, agent_id: str, handoff_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row, file_row = self._lead_handoff(c, handoff_id)

        lead_review = latest_lead_review(self.conn, file_row["file_id"], handoff_row["created_at"])
        if lead_review is None:
            raise LedgerError("no lead review exists for this handoff yet")

        self_scores = (
            self._scores_for(handoff_row["self_review_id"])
            if handoff_row["self_review_id"] is not None
            else {}
        )
        lead_scores = self._scores_for(lead_review["review_id"])
        disagreements = rubric.disagreements(self_scores, lead_scores, self.settings.rubric)

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE handoffs SET compared_at = {NOW} WHERE handoff_id = ?", (handoff_id,)
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
        handoff_row, file_row = self._lead_handoff(c, handoff_id)
        if handoff_row["compared_at"] is None:
            raise LedgerError("review_compare has not run for this handoff")

        lead_review = latest_lead_review(self.conn, file_row["file_id"], handoff_row["created_at"])
        if lead_review is None:
            raise LedgerError("no lead review exists for this handoff")
        lead_scores = self._scores_for(lead_review["review_id"])
        floor_dimensions = self._floor_pass_dimensions(
            file_row["file_id"], lead_scores, self._ratings_for(lead_review["review_id"])
        )
        t = self.settings.rubric

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
                f"decided_at = {NOW}, decided_by = ?, floor_pass_json = ? WHERE handoff_id = ?",
                (
                    notes,
                    c.agent_id,
                    json.dumps(floor_dimensions) if floor_dimensions else None,
                    handoff_id,
                ),
            )
            conn.execute(
                f"UPDATE files SET state = 'approved', released_at = {NOW} WHERE file_id = ?",
                (file_row["file_id"],),
            )
            settle.sync_products(conn, file_row["file_id"], "approved", released="set")
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
            woken = self._wake_file_waiters(conn, c, file_row["path"], "approved")
            if not settle.unsettled_files(conn, self.repo_root, file_row["module_id"]):
                conn.execute(
                    "UPDATE modules SET state = 'approved' WHERE module_id = ?",
                    (file_row["module_id"],),
                )

        result = row_dict(self.conn, "handoffs", "handoff_id", handoff_id)
        if floor_dimensions:
            result["floor_pass_dimensions"] = floor_dimensions
        if woken:
            result["woken_waiters"] = [
                {"name": w["to_name"], "next": self.next_step(w)} for w in woken
            ]
        return result

    def _floor_pass_dimensions(
        self, file_id: int, lead_scores: dict[str, float], ratings: list[Rating]
    ) -> list[str]:
        t = self.settings.rubric
        ok, reasons = rubric.passes(lead_scores, ratings, t)
        if ok:
            return []
        at_or_above_floor = all(score >= t.floor for score in lead_scores.values())
        no_criterion_below_floor = all(r.value >= t.criterion_floor for r in ratings)
        if at_or_above_floor and no_criterion_below_floor and self._file_at_last_round(file_id):
            return sorted(d for d, score in lead_scores.items() if score < t.target)
        raise LedgerError(f"the lead review does not pass: {'; '.join(reasons)}")

    def return_work(
        self, caller: str, agent_id: str, handoff_id: int, issues: list[str], targeted: list[str]
    ) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        handoff_row, file_row = self._lead_handoff(c, handoff_id)

        self._block_return_for_open_departures(self.conn, handoff_id)

        next_round = self.conn.execute(
            "SELECT COALESCE(MAX(round), 0) + 1 AS n FROM attempts WHERE file_id = ?",
            (file_row["file_id"],),
        ).fetchone()["n"]

        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE handoffs SET state = 'returned', decided_at = {NOW}, decided_by = ? "
                "WHERE handoff_id = ?",
                (c.agent_id, handoff_id),
            )
            conn.execute(
                "UPDATE files SET state = 'returned' WHERE file_id = ?", (file_row["file_id"],)
            )
            settle.sync_products(conn, file_row["file_id"], "returned")
            coder_row = conn.execute(
                "SELECT state, state_before_park FROM agents WHERE agent_id = ?",
                (handoff_row["agent_id"],),
            ).fetchone()
            # 'idle', not 'working': the Coder's turn ended at its handoff, and the Stop
            # hook reads 'working' as a turn in progress. Its next tool use marks it working.
            if coder_row is not None:
                column = "state_before_park" if coder_row["state"] == "parked" else "state"
                conn.execute(
                    f"UPDATE agents SET {column} = 'idle' WHERE agent_id = ?",
                    (handoff_row["agent_id"],),
                )
                self._log_event(
                    conn, handoff_row["agent_id"], logical_state(coder_row), "idle", "return_work"
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

        attempt = row_dict(self.conn, "attempts", "attempt_id", attempt_id)
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
        return escalation_target(chain, next_round)

    def attempt_record(self, caller: str, agent_id: str, file_id: int) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "lead")
        file_row = self._lead_file(c, file_id)

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

        outcome = rubric.classify(before_scores, after_scores, targeted, self.settings.rubric)

        open_issues = dict_rows(
            self.conn.execute(
                "SELECT * FROM issues WHERE file_id = ? AND state = 'open'", (file_id,)
            )
        )
        restore_from = self._regression_restore_point(file_id) if outcome == "regression" else None

        kept_review = before_review if outcome == "regression" else after_review
        kept_scores = before_scores if outcome == "regression" else after_scores
        kept_ratings = self._ratings_for(kept_review["review_id"])

        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE attempts SET outcome = ? WHERE attempt_id = ?",
                (outcome, last_attempt["attempt_id"]),
            )
            rounds, escalated, notices = self._advance_issue_rounds(
                conn, c, file_row, open_issues, outcome, kept_scores, kept_ratings
            )
            if restore_from is not None:
                self._restore_handoff(conn, file_row, restore_from)

        for notice in notices:
            notify.deliver(notice, self.settings.notify)
        return {
            "outcome": outcome,
            "rounds": rounds,
            "escalated": escalated,
            "notifications": notices,
        }

    def _regression_restore_point(self, file_id: int) -> sqlite3.Row:
        handoffs = self.conn.execute(
            "SELECT * FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 2",
            (file_id,),
        ).fetchall()
        if len(handoffs) < 2:
            raise LedgerError("no earlier handoff to restore for a regression")
        return handoffs[1]

    def _advance_issue_rounds(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        file_row: sqlite3.Row,
        open_issues: list[dict],
        outcome: str,
        kept_scores: dict[str, float],
        kept_ratings: list[Rating],
    ) -> tuple[dict[int, int], list[dict], list[dict]]:
        escalation = self.settings.escalation
        rounds: dict[int, int] = {}
        escalated: list[dict] = []
        notices: list[dict] = []
        for issue in open_issues:
            if outcome not in ("plateau", "regression"):
                rounds[issue["issue_id"]] = issue["round"]
                continue
            new_attempts = issue["attempts"] + 1
            next_round = issue["round"] + 1
            if new_attempts >= escalation.attempts_per_round and next_round <= escalation.rounds:
                moved = self._escalate_issue(conn, c, file_row["path"], issue, next_round)
                rounds[issue["issue_id"]] = next_round
                if moved is not None:
                    escalated.append(moved)
                continue
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
        return rounds, escalated, notices

    def _escalate_issue(
        self, conn: sqlite3.Connection, c: Caller, path: str, issue: dict, next_round: int
    ) -> dict | None:
        target = self._round_advance_target(conn, issue, next_round)
        conn.execute(
            "UPDATE issues SET round = ?, attempts = 0, escalated_to = ? WHERE issue_id = ?",
            (next_round, target["name"] if target is not None else None, issue["issue_id"]),
        )
        if target is None:
            return None
        pointer = (
            f"Issue {issue['issue_id']} for {path} moved to round "
            f"{next_round}; read it with issue_list."
        )
        conn.execute(
            "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
            (c.run_id, c.name, target["name"], pointer),
        )
        wakeup = self._owe_wakeup(conn, c, target["agent_id"], "attempt_record", pointer)
        return {
            "issue_id": issue["issue_id"],
            "round": next_round,
            "escalated_to": target["name"],
            "next": self.next_step(wakeup),
        }

    def _restore_handoff(
        self, conn: sqlite3.Connection, file_row: sqlite3.Row, restore_from: sqlite3.Row
    ) -> None:
        self._restore_stored_version(
            conn, restore_from["version_id"], self.repo_root / file_row["path"]
        )
        if file_row["test_path"]:
            self._restore_stored_version(
                conn, restore_from["test_version_id"], self.repo_root / file_row["test_path"]
            )
        if restore_from["agent_id"] is not None:
            self._log_event(
                conn,
                restore_from["agent_id"],
                None,
                "restored",
                f"attempt_record: regression restored version_id={restore_from['version_id']}",
            )

    def _restore_stored_version(
        self, conn: sqlite3.Connection, version_id: int | None, target: Path
    ) -> None:
        row = conn.execute("SELECT * FROM versions WHERE version_id = ?", (version_id,)).fetchone()
        if row is not None and row["stored_path"]:
            versions.restore_version(ledger_path(self.repo_root).parent, row["stored_path"], target)

    def _notify_last_round_below_floor(
        self,
        conn: sqlite3.Connection,
        issue: dict,
        path: str,
        scores: dict[str, float],
        ratings: list[Rating],
    ) -> dict | None:
        t = self.settings.rubric
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
        handoff_row, file_row = self._lead_handoff(c, handoff_id)
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
                f"decided_at = {NOW}, decided_by = ? WHERE handoff_id = ?",
                (reason, c.agent_id, handoff_id),
            )
            conn.execute(
                f"UPDATE files SET state = 'incomplete', released_at = {NOW} WHERE file_id = ?",
                (file_row["file_id"],),
            )
            settle.sync_products(conn, file_row["file_id"], "incomplete", released="set")
            self._mark_departures_reworked(conn, file_row["file_id"], handoff_id)
            self._release_agent(conn, handoff_row["agent_id"], "accept_incomplete")
            woken = self._wake_file_waiters(conn, c, file_row["path"], "incomplete")

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

        result = row_dict(self.conn, "deferrals", "deferral_id", deferral_id)
        if woken:
            result["woken_waiters"] = [
                {"name": w["to_name"], "next": self.next_step(w)} for w in woken
            ]
        return result

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

        result = row_dict(self.conn, "deferrals", "deferral_id", deferral_id)
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
        return [row["agent_id"] for row in parent_chain(conn, agent_id)][1:]

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
                    f"{agent_name(self.conn, row['proposed_by'])} and "
                    f"{', '.join(json.loads(row['parties_json'] or '[]'))}; only their closest "
                    f"shared ancestor, {agent_name(self.conn, row['arbiter_agent_id'])}, "
                    "decides it"
                )
        else:
            proposer = self.conn.execute(
                "SELECT role FROM agents WHERE agent_id = ?", (row["proposed_by"],)
            ).fetchone()
            proposer_role = proposer["role"] if proposer is not None else "oracle"
            parent_role = _PARENT_ROLE.get(proposer_role, "oracle")
            kind_role = _DEFERRAL_LEVEL[kind]
            required_role = max(parent_role, kind_role, key=ROLE_RANK.__getitem__)
            if ROLE_RANK.get(c.role, -1) < ROLE_RANK[required_role]:
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
                f"directive_id = ?, decided_at = {NOW} WHERE deferral_id = ?",
                (decision, c.agent_id, reason, directive_id, deferral_id),
            )

        return row_dict(self.conn, "deferrals", "deferral_id", deferral_id)

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
        return work_scope(self.conn, row["file_id"], row["proposed_by"])

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
        if version_row["deleted"]:
            target_path.unlink(missing_ok=True)
        elif version_row["stored_path"]:
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
                    lead_review = latest_lead_review(conn, file_row["file_id"])
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
            "fixes": fix_briefs(conn, run_id),
        }

    def analytics_query(self, caller: str, agent_id: str, sql: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        require_role(c, "oracle")
        stripped = sql.strip().rstrip(";")
        if ";" in stripped or not _SELECT_RE.match(stripped):
            raise LedgerError("only a single SELECT statement is allowed")

        conn = open_readonly(ledger_path(self.repo_root))
        try:
            cursor = conn.execute(stripped)
            rows = cursor.fetchmany(500)
            return {"rows": [dict(row) for row in rows]}
        except sqlite3.Error as exc:
            raise LedgerError(f"query failed: {exc}") from exc
        finally:
            conn.close()
