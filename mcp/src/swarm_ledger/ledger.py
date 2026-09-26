from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import __version__, agentfiles, serve, sessions
from .agreements import AgreementsMixin
from .db import connect, ensure_git_exclude, ledger_path, write_tx
from .identity import ROLES, Caller, LedgerError, child_role_of, require_role, resolve
from .oversight import OversightMixin
from .repo import RepoMixin
from .review import ReviewMixin
from .settings import load_settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_RESUME_POINTER = "Re-read your brief and your inbox in the ledger."
_PHASE_STATES = ("planned", "unlocked", "working", "handed_up", "approved")
_DIRECTIVE_SOURCES = ("user-chat", "outside-session", "skill", "watchdog")
_DIRECTIVE_OUTCOMES = ("applied", "scheduled", "declined", "needs_user")
_ISSUE_OPEN_ROUND = {"manager": 2, "oracle": 3}
_LIVE_RUN = "state IN ('active', 'paused')"
_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "context_pct_peak",
    "context_pct_at_end",
    "context_overflow_count",
    "tool_uses",
    "duration_ms",
)


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def repo_slug(repo_root: Path) -> str:
    return re.sub(r"[^a-z0-9]", "-", repo_root.resolve().name.lower())


def session_name_for(repo_root: Path, run_id: int, child_name: str) -> str:
    return f"{repo_slug(repo_root)}-r{run_id}-{child_name}"


class Ledger(AgreementsMixin, ReviewMixin, RepoMixin, OversightMixin):
    # AgreementsMixin first: ReviewMixin declares stub bodies for the gate methods
    # AgreementsMixin implements (for pyright, since review.py's methods are typed
    # against ReviewMixin alone), and MRO resolves the first base's attribute, so
    # ReviewMixin's empty stub would otherwise shadow the real implementation.
    def __init__(self, repo_root: Path, *, db_path: Path | None = None) -> None:
        self.repo_root = repo_root
        self.settings = load_settings(repo_root)
        path = db_path if db_path is not None else ledger_path(repo_root)
        self.conn = connect(path)
        self._pending_stops: list[tuple[str, str]] = []
        if (repo_root / ".git").exists():
            ensure_git_exclude(repo_root)

    @contextmanager
    def _release_tx(self) -> Iterator[sqlite3.Connection]:
        self._pending_stops = []
        with write_tx(self.conn) as conn:
            yield conn
        self._stop_released_sessions()

    def _stop_released_sessions(self) -> None:
        pending, self._pending_stops = self._pending_stops, []
        for agent_id, bg_id in pending:
            try:
                sessions.stop(bg_id)
                outcome = f"session {bg_id} stopped"
            except Exception as exc:
                outcome = f"session {bg_id} was not stopped: {exc}"
            with write_tx(self.conn) as conn:
                self._log_event(conn, agent_id, "released", "released", outcome)

    def _log_event(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        from_state: str | None,
        to_state: str,
        reason: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) VALUES (?, ?, ?, ?)",
            (agent_id, from_state, to_state, reason),
        )

    def _agent_dict(self, agent_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return dict(row) if row is not None else {}

    def _active_run(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(f"SELECT * FROM runs WHERE {_LIVE_RUN}").fetchone()
        if row is None:
            raise LedgerError("no active run")
        return row

    def pause_reason(self, run_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT e.reason FROM agent_events e JOIN agents a ON a.agent_id = e.agent_id "
            "WHERE a.run_id = ? AND e.to_state = 'paused' ORDER BY e.event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["reason"] if row is not None else None

    # -- Run and plan -----------------------------------------------------

    def run_start(self, prd: str, session_id: str, oracle_name: str = "oracle") -> dict:
        listing: list[dict] | None = None
        listing_error: LedgerError | None = None
        try:
            listing = sessions.list_sessions()
        except LedgerError as exc:
            listing_error = exc
        own = sessions.find_session(listing or [], session_id)
        session_name = str(own["name"]) if own is not None and own.get("name") else None

        with write_tx(self.conn) as conn:
            active = conn.execute(f"SELECT * FROM runs WHERE {_LIVE_RUN}").fetchone()
            if active is not None:
                self._refuse_live_oracle(conn, active, session_id, listing, listing_error)
                return self._run_resume(conn, active, session_id, session_name)

            cur = conn.execute(
                "INSERT INTO runs (prd, state, plugin_version, settings_json) "
                "VALUES (?, 'active', ?, ?)",
                (prd, __version__, self.settings.snapshot()),
            )
            run_id = cur.lastrowid

            conn.execute(
                "INSERT INTO agents "
                "(agent_id, name, role, runtime, model, run_id, state, session_name, started_at) "
                f"VALUES (?, ?, 'oracle', ?, ?, ?, 'working', ?, {_NOW})",
                (
                    session_id,
                    oracle_name,
                    self.settings.runtime.get("oracle"),
                    agentfiles.oracle_model(self.repo_root, self.settings.models.get("oracle", [])),
                    run_id,
                    session_name,
                ),
            )
            self._log_event(conn, session_id, None, "working", "run_start")

        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return {"run": dict(run), "oracle": self._agent_dict(session_id), "resumed": False}

    def _refuse_live_oracle(
        self,
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        session_id: str,
        listing: list[dict] | None,
        listing_error: LedgerError | None,
    ) -> None:
        holder = conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
            "AND agent_id != ? ORDER BY started_at DESC LIMIT 1",
            (run["run_id"], session_id),
        ).fetchone()
        if holder is None:
            return
        if listing is None:
            raise LedgerError(
                f"cannot tell whether run {run['run_id']}'s Oracle session is still running, "
                f"so run_start refuses: {listing_error}"
            )
        entry = sessions.find_session(listing, holder["agent_id"])
        if entry is not None and sessions.is_running(entry):
            label = holder["session_name"] or entry.get("name") or holder["agent_id"]
            raise LedgerError(
                f"run {run['run_id']} is held by the Oracle session {label!r}, which is still "
                "running. One swarm runs per repo: finish or stop that session first"
            )

    def _run_resume(
        self,
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        session_id: str,
        session_name: str | None,
    ) -> dict:
        oracle = conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' "
            "ORDER BY started_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        if oracle is None:
            raise LedgerError("the active run has no Oracle row")
        old_id = oracle["agent_id"]
        if old_id == session_id:
            if session_name is not None:
                conn.execute(
                    "UPDATE agents SET session_name = ? WHERE agent_id = ?",
                    (session_name, session_id),
                )
        else:
            conn.execute(
                f"UPDATE agents SET ended_at = COALESCE(ended_at, {_NOW}), "
                "end_reason = COALESCE(end_reason, 'resumed by a new session'), "
                "state = 'released' WHERE agent_id = ?",
                (old_id,),
            )
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, runtime, model, effort, "
                "settings_json, run_id, state, phase_at_start, session_name, started_at) "
                f"VALUES (?, ?, 'oracle', ?, ?, ?, ?, ?, 'working', ?, ?, {_NOW})",
                (
                    session_id,
                    oracle["name"],
                    oracle["runtime"],
                    agentfiles.oracle_model(self.repo_root, self.settings.models.get("oracle", [])),
                    oracle["effort"],
                    oracle["settings_json"],
                    run["run_id"],
                    oracle["phase_at_start"],
                    session_name,
                ),
            )
            conn.execute(
                "UPDATE agents SET parent_agent_id = ? WHERE parent_agent_id = ?",
                (session_id, old_id),
            )
            conn.execute(
                "UPDATE briefs SET parent_agent_id = ? WHERE parent_agent_id = ?",
                (session_id, old_id),
            )
            self._log_event(conn, session_id, oracle["state"], "working", f"resumed from {old_id}")
        if run["state"] == "paused":
            conn.execute("UPDATE runs SET state = 'active' WHERE run_id = ?", (run["run_id"],))
            self._log_event(conn, session_id, "paused", "active", "resumed from pause")
        current = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()
        return {"run": dict(current), "oracle": self._agent_dict(session_id), "resumed": True}

    def run_pause(self, caller: str, agent_id: str, reason: str) -> dict:
        if not reason.strip():
            raise LedgerError("run_pause needs a reason the user can act on")
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            run = self._active_run(conn)
            if run["state"] == "paused":
                raise LedgerError("the run is already paused; run_start resumes it")
            conn.execute("UPDATE runs SET state = 'paused' WHERE run_id = ?", (run["run_id"],))
            self._log_event(conn, c.agent_id, "active", "paused", reason)

        run_row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        return dict(run_row) | {"reason": reason}

    def run_status(self, caller: str, agent_id: str) -> dict:
        conn = self.conn
        resolve(conn, caller, agent_id)
        run = self._active_run(conn)
        run_id = run["run_id"]

        phases = _rows(
            conn.execute("SELECT * FROM phases WHERE run_id = ? ORDER BY ordinal", (run_id,))
        )
        modules = _rows(
            conn.execute(
                "SELECT modules.* FROM modules "
                "JOIN phases ON phases.phase_id = modules.phase_id "
                "WHERE phases.run_id = ? ORDER BY modules.module_id",
                (run_id,),
            )
        )
        files = _rows(
            conn.execute(
                "SELECT files.* FROM files "
                "JOIN modules ON modules.module_id = files.module_id "
                "JOIN phases ON phases.phase_id = modules.phase_id "
                "WHERE phases.run_id = ? ORDER BY files.file_id",
                (run_id,),
            )
        )
        agents = _rows(
            conn.execute(
                "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL ORDER BY started_at",
                (run_id,),
            )
        )
        issues = _rows(
            conn.execute(
                "SELECT * FROM issues WHERE run_id = ? AND state = 'open' ORDER BY issue_id",
                (run_id,),
            )
        )
        directives = _rows(
            conn.execute(
                "SELECT * FROM directives WHERE run_id = ? AND state = 'open' "
                "ORDER BY directive_id",
                (run_id,),
            )
        )

        return {
            "run": dict(run),
            "phases": phases,
            "modules": modules,
            "files": files,
            "agents": agents,
            "issues": issues,
            "directives": directives,
        }

    def run_finish(self, caller: str, agent_id: str, outcome: str) -> dict:
        with self._release_tx() as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            run = self._active_run(conn)
            run_id = run["run_id"]

            unapproved = conn.execute(
                "SELECT COUNT(*) AS n FROM phases WHERE run_id = ? AND state != 'approved'",
                (run_id,),
            ).fetchone()["n"]
            if unapproved:
                raise LedgerError(f"{unapproved} phase(s) are not approved")

            live_claims = conn.execute(
                "SELECT COUNT(*) AS n FROM files "
                "JOIN modules ON modules.module_id = files.module_id "
                "JOIN phases ON phases.phase_id = modules.phase_id "
                "WHERE phases.run_id = ? AND files.released_at IS NULL",
                (run_id,),
            ).fetchone()["n"]
            if live_claims:
                raise LedgerError(f"{live_claims} file claim(s) are still live")

            open_directives = conn.execute(
                "SELECT COUNT(*) AS n FROM directives WHERE run_id = ? AND state = 'open'",
                (run_id,),
            ).fetchone()["n"]
            if open_directives:
                raise LedgerError(f"{open_directives} directive(s) are still open")

            open_deferrals = conn.execute(
                "SELECT COUNT(*) AS n FROM deferrals WHERE run_id = ? AND state = 'open'",
                (run_id,),
            ).fetchone()["n"]
            if open_deferrals:
                raise LedgerError(f"{open_deferrals} deferral(s) are still open")

            self._block_run_finish_for_cr(conn, run_id)
            self._block_run_finish_for_departures(conn, run_id)

            conn.execute(
                f"UPDATE runs SET state = 'finished', outcome = ?, ended_at = {_NOW} "
                "WHERE run_id = ?",
                (outcome, run_id),
            )
            for row in conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND role != 'oracle' "
                "AND ended_at IS NULL",
                (run_id,),
            ).fetchall():
                self._release_agent(conn, row["agent_id"], "run_finish")

        report = self.report_build(caller, agent_id)
        with write_tx(self.conn) as conn:
            conn.execute(
                f"UPDATE agents SET state = 'released', ended_at = {_NOW}, "
                "end_reason = 'run_finish' WHERE agent_id = ?",
                (c.agent_id,),
            )
            self._log_event(conn, c.agent_id, "working", "released", "run_finish")

        run_row = dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
        run_row["report_path"] = report["path"]
        return run_row

    def profile_set(
        self,
        caller: str,
        agent_id: str,
        test_command: str | None = None,
        build_command: str | None = None,
        lint_command: str | None = None,
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            if c.run_id is None:
                raise LedgerError(f"{caller!r} has no run")

            if test_command is not None:
                self.settings.test_command = test_command
            if build_command is not None:
                self.settings.build_command = build_command
            if lint_command is not None:
                self.settings.lint_command = lint_command

            conn.execute(
                "UPDATE runs SET settings_json = ? WHERE run_id = ?",
                (self.settings.snapshot(), c.run_id),
            )

        return {
            "test_command": self.settings.test_command,
            "build_command": self.settings.build_command,
            "lint_command": self.settings.lint_command,
        }

    def guidelines_set(self, caller: str, agent_id: str, body: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            cur = conn.execute(
                "INSERT INTO guidelines (run_id, body) VALUES (?, ?)", (c.run_id, body)
            )
            guideline_id = cur.lastrowid

        row = self.conn.execute(
            "SELECT * FROM guidelines WHERE guideline_id = ?", (guideline_id,)
        ).fetchone()
        return dict(row)

    def guidelines_get(self, caller: str, agent_id: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        row = self.conn.execute(
            "SELECT * FROM guidelines WHERE run_id = ? ORDER BY guideline_id DESC LIMIT 1",
            (c.run_id,),
        ).fetchone()
        return dict(row) if row is not None else {}

    def phase_add(
        self, caller: str, agent_id: str, name: str, depends_on: list[int] | None = None
    ) -> dict:
        depends_on = depends_on or []
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            ordinal = conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM phases WHERE run_id = ?",
                (c.run_id,),
            ).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, ?, ?, 'planned')",
                (c.run_id, name, ordinal),
            )
            phase_id = cur.lastrowid
            for dep_id in depends_on:
                conn.execute(
                    "INSERT INTO phase_deps (phase_id, depends_on_phase_id) VALUES (?, ?)",
                    (phase_id, dep_id),
                )

        phase = dict(
            self.conn.execute("SELECT * FROM phases WHERE phase_id = ?", (phase_id,)).fetchone()
        )
        phase["depends_on"] = list(depends_on)
        return phase

    def phase_update(self, caller: str, agent_id: str, phase_id: int, state: str) -> dict:
        if state not in _PHASE_STATES:
            raise LedgerError(f"unknown phase state {state!r}")
        wakeup = None
        with self._release_tx() as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle", "manager")
            if c.role == "manager" and (
                c.phase_id != phase_id or state not in ("working", "handed_up")
            ):
                raise LedgerError("a Manager sets only its own phase to working or handed_up")
            if (
                conn.execute("SELECT 1 FROM phases WHERE phase_id = ?", (phase_id,)).fetchone()
                is None
            ):
                raise LedgerError(f"unknown phase_id {phase_id!r}")

            if state == "handed_up":
                live_leads = [
                    row["name"]
                    for row in conn.execute(
                        "SELECT a.name FROM agents a JOIN modules m ON m.module_id = a.module_id "
                        "WHERE m.phase_id = ? AND a.role = 'lead' AND a.ended_at IS NULL",
                        (phase_id,),
                    )
                ]
                if live_leads:
                    raise LedgerError(
                        f"release every Lead of the phase with agent_release first: {live_leads}"
                    )
                self._require_phase_modules_reviewed(conn, phase_id)

            if state == "approved":
                open_deferrals = conn.execute(
                    "SELECT COUNT(*) AS n FROM deferrals d JOIN files f ON f.file_id = d.file_id "
                    "JOIN modules m ON m.module_id = f.module_id "
                    "WHERE m.phase_id = ? AND d.state = 'open'",
                    (phase_id,),
                ).fetchone()["n"]
                if open_deferrals:
                    raise LedgerError(
                        f"{open_deferrals} deferral(s) in the phase are still open; the Manager "
                        "decides them before approval releases it"
                    )
                self._block_phase_approval_for_cr(conn, phase_id)
                phase_row = conn.execute(
                    "SELECT handed_up_at FROM phases WHERE phase_id = ?", (phase_id,)
                ).fetchone()
                accepted_review = conn.execute(
                    "SELECT 1 FROM reviews WHERE phase_id = ? AND kind = 'oracle' "
                    "AND outcome = 'accepted' AND (? IS NULL OR created_at >= ?) LIMIT 1",
                    (phase_id, phase_row["handed_up_at"], phase_row["handed_up_at"]),
                ).fetchone()
                if accepted_review is None:
                    raise LedgerError(
                        "no accepted Oracle phase_review of this phase exists since it was "
                        "handed up; call phase_review first"
                    )
                conn.execute(
                    f"UPDATE phases SET state = ?, ended_at = {_NOW} WHERE phase_id = ?",
                    (state, phase_id),
                )
                for row in conn.execute(
                    "SELECT agent_id FROM agents WHERE ended_at IS NULL AND ("
                    "phase_id = ? OR module_id IN (SELECT module_id FROM modules "
                    "WHERE phase_id = ?) OR file_id IN (SELECT f.file_id FROM files f "
                    "JOIN modules m ON m.module_id = f.module_id WHERE m.phase_id = ?))",
                    (phase_id, phase_id, phase_id),
                ).fetchall():
                    self._release_agent(conn, row["agent_id"], "phase approved")
            elif state == "handed_up":
                conn.execute(
                    f"UPDATE phases SET state = ?, handed_up_at = {_NOW} WHERE phase_id = ?",
                    (state, phase_id),
                )
            else:
                conn.execute("UPDATE phases SET state = ? WHERE phase_id = ?", (state, phase_id))

            if state == "handed_up" and c.parent_agent_id is not None:
                phase_name = conn.execute(
                    "SELECT name FROM phases WHERE phase_id = ?", (phase_id,)
                ).fetchone()["name"]
                wakeup = self._owe_wakeup(
                    conn,
                    c,
                    c.parent_agent_id,
                    "phase_update",
                    f"Phase {phase_id} ({phase_name}) is handed up and waiting in the ledger.",
                )

        phase = dict(
            self.conn.execute("SELECT * FROM phases WHERE phase_id = ?", (phase_id,)).fetchone()
        )
        if state == "handed_up":
            phase["next"] = self.next_step(wakeup)
        return phase

    def plan_unlocked(self, caller: str, agent_id: str) -> list[dict]:
        conn = self.conn
        c = resolve(conn, caller, agent_id)
        rows = conn.execute(
            "SELECT * FROM phases WHERE run_id = ? AND state != 'approved' ORDER BY ordinal",
            (c.run_id,),
        ).fetchall()

        unlocked = []
        for row in rows:
            deps = conn.execute(
                "SELECT depends_on_phase_id FROM phase_deps WHERE phase_id = ?",
                (row["phase_id"],),
            ).fetchall()
            if not deps:
                unlocked.append(dict(row))
                continue
            satisfied = all(
                conn.execute(
                    "SELECT state FROM phases WHERE phase_id = ?", (dep["depends_on_phase_id"],)
                ).fetchone()["state"]
                == "approved"
                for dep in deps
            )
            if satisfied:
                unlocked.append(dict(row))
        return unlocked

    def module_add(self, caller: str, agent_id: str, phase_id: int, name: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "manager")
            if c.phase_id != phase_id:
                raise LedgerError("a manager may only add modules to its own phase")
            cur = conn.execute(
                "INSERT INTO modules (phase_id, name, state) VALUES (?, ?, 'planned')",
                (phase_id, name),
            )
            module_id = cur.lastrowid

        return dict(
            self.conn.execute("SELECT * FROM modules WHERE module_id = ?", (module_id,)).fetchone()
        )

    # -- Briefs -------------------------------------------------------------

    def brief_create(
        self,
        caller: str,
        agent_id: str,
        child_name: str,
        child_role: str,
        model: str,
        body: str,
        phase_id: int | None = None,
        module_id: int | None = None,
        file_id: int | None = None,
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            if child_role_of(c.role) != child_role:
                raise LedgerError(f"a {c.role!r} may not brief a {child_role!r}")
            if model not in self.settings.models.get(child_role, []):
                raise LedgerError(f"model {model!r} is not approved for {child_role!r}")
            if (
                conn.execute(
                    "SELECT 1 FROM agents WHERE name = ? AND ended_at IS NULL", (child_name,)
                ).fetchone()
                is not None
            ):
                raise LedgerError(f"a live agent already holds the name {child_name!r}")
            if (
                conn.execute(
                    "SELECT 1 FROM briefs WHERE child_name = ? AND acked_at IS NULL",
                    (child_name,),
                ).fetchone()
                is not None
            ):
                raise LedgerError(f"an unacked brief already exists for {child_name!r}")

            cur = conn.execute(
                "INSERT INTO briefs "
                "(run_id, parent_agent_id, child_name, child_role, model, body, "
                "phase_id, module_id, file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.run_id,
                    c.agent_id,
                    child_name,
                    child_role,
                    model,
                    body,
                    phase_id if phase_id is not None else c.phase_id,
                    module_id if module_id is not None else c.module_id,
                    file_id if file_id is not None else c.file_id,
                ),
            )
            brief_id = cur.lastrowid

        return dict(
            self.conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
        )

    def brief_get(self, caller_name: str, child_name: str) -> dict:
        del caller_name
        row = self.conn.execute(
            "SELECT * FROM briefs WHERE child_name = ? "
            "ORDER BY created_at DESC, brief_id DESC LIMIT 1",
            (child_name,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"no brief found for {child_name!r}")
        return dict(row)

    def brief_ack(self, caller: str, agent_id: str, agent_type: str | None = None) -> dict:
        del agent_type
        with write_tx(self.conn) as conn:
            brief_row = conn.execute(
                "SELECT * FROM briefs WHERE child_name = ? AND acked_at IS NULL "
                "ORDER BY created_at DESC, brief_id DESC LIMIT 1",
                (caller,),
            ).fetchone()
            if brief_row is None:
                raise LedgerError(f"no unacked brief for {caller!r}")

            existing = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if existing is not None and existing["state"] != "registered":
                raise LedgerError(f"agent_id {agent_id!r} is already bound")
            if existing is not None and existing["session_name"] and existing["name"] != caller:
                raise LedgerError(
                    f"agent_id {agent_id!r} was spawned as {existing['name']!r}, not {caller!r}"
                )

            if (
                conn.execute(
                    "SELECT 1 FROM agents WHERE name = ? AND ended_at IS NULL AND agent_id != ?",
                    (caller, agent_id),
                ).fetchone()
                is not None
            ):
                raise LedgerError(f"a live agent already holds the name {caller!r}")

            role = brief_row["child_role"]
            runtime = self.settings.runtime.get(role)
            from_state = existing["state"] if existing is not None else None

            if existing is None:
                conn.execute(
                    "INSERT INTO agents "
                    "(agent_id, name, role, runtime, model, parent_agent_id, "
                    "run_id, phase_id, module_id, file_id, state, started_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'working', {_NOW})",
                    (
                        agent_id,
                        caller,
                        role,
                        runtime,
                        brief_row["model"],
                        brief_row["parent_agent_id"],
                        brief_row["run_id"],
                        brief_row["phase_id"],
                        brief_row["module_id"],
                        brief_row["file_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE agents SET name = ?, role = ?, runtime = COALESCE(runtime, ?), "
                    "model = ?, parent_agent_id = ?, run_id = ?, phase_id = ?, module_id = ?, "
                    f"file_id = ?, state = 'working', started_at = {_NOW} WHERE agent_id = ?",
                    (
                        caller,
                        role,
                        runtime,
                        brief_row["model"],
                        brief_row["parent_agent_id"],
                        brief_row["run_id"],
                        brief_row["phase_id"],
                        brief_row["module_id"],
                        brief_row["file_id"],
                        agent_id,
                    ),
                )

            conn.execute(
                f"UPDATE briefs SET acked_by_agent_id = ?, acked_at = {_NOW} WHERE brief_id = ?",
                (agent_id, brief_row["brief_id"]),
            )
            self._log_event(conn, agent_id, from_state, "working", "brief_ack")

        return self._agent_dict(agent_id)

    # -- Agent lifecycle ------------------------------------------------------

    def agent_heartbeat(self, agent_id: str, activity: str | None = None) -> dict:
        with write_tx(self.conn) as conn:
            if (
                conn.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
                is None
            ):
                return {"known": False}
            conn.execute(
                f"UPDATE agents SET last_heartbeat_at = {_NOW}, "
                "current_activity = COALESCE(?, current_activity) WHERE agent_id = ?",
                (activity, agent_id),
            )

        return {"known": True, "agent_id": agent_id, "current_activity": activity}

    def agent_register_start(
        self, agent_id: str, agent_type: str, parent_agent_id: str | None = None
    ) -> dict:
        with write_tx(self.conn) as conn:
            existing = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)

            role = agent_type if agent_type in ROLES else "unknown"
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, parent_agent_id, state, started_at) "
                f"VALUES (?, ?, ?, ?, 'registered', {_NOW})",
                (agent_id, agent_id, role, parent_agent_id),
            )
            self._log_event(conn, agent_id, None, "registered", "agent_register_start")

        return self._agent_dict(agent_id)

    def agent_stop(
        self,
        agent_id: str,
        transcript_path: str | None = None,
        tokens: dict | None = None,
        end_reason: str | None = None,
    ) -> dict:
        with write_tx(self.conn) as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                return {"ended": False, "state": "unknown"}

            set_clauses: list[str] = []
            params: list[object] = []
            for column in _TOKEN_COLUMNS:
                value = (tokens or {}).get(column)
                if value is not None:
                    set_clauses.append(f"{column} = ?")
                    params.append(value)
            if transcript_path is not None:
                set_clauses.append("transcript_path = ?")
                params.append(transcript_path)

            if row["state"] == "released":
                if row["ended_at"] is None:
                    set_clauses.append(f"ended_at = {_NOW}")
                    self._log_event(conn, agent_id, row["state"], row["state"], "agent_stop")
                if end_reason is not None:
                    set_clauses.append("end_reason = ?")
                    params.append(end_reason)
                if set_clauses:
                    conn.execute(
                        f"UPDATE agents SET {', '.join(set_clauses)} WHERE agent_id = ?",
                        (*params, agent_id),
                    )
                return self._agent_dict(agent_id) | {"ended": True}

            set_clauses.append(f"last_heartbeat_at = {_NOW}")
            conn.execute(
                f"UPDATE agents SET {', '.join(set_clauses)} WHERE agent_id = ?",
                (*params, agent_id),
            )
            return {"ended": False, "state": row["state"]}

    def agent_release(self, caller: str, agent_id: str, target_agent_id: str) -> dict:
        with self._release_tx() as conn:
            c = resolve(conn, caller, agent_id)
            target = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (target_agent_id,)
            ).fetchone()
            if target is None:
                raise LedgerError(f"unknown agent_id {target_agent_id!r}")
            if target["parent_agent_id"] != c.agent_id:
                raise LedgerError(f"{caller!r} is not the parent of {target_agent_id!r}")
            self._release_agent(conn, target_agent_id, "agent_release")

        return self._agent_dict(target_agent_id)

    # -- Sessions -------------------------------------------------------------

    def agent_spawn(self, caller: str, agent_id: str, child_name: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        brief = self.conn.execute(
            "SELECT * FROM briefs WHERE child_name = ? AND acked_at IS NULL "
            "ORDER BY created_at DESC, brief_id DESC LIMIT 1",
            (child_name,),
        ).fetchone()
        if brief is None:
            raise LedgerError(
                f"no unacknowledged brief for {child_name!r}; call brief_create first"
            )
        if brief["parent_agent_id"] != c.agent_id or child_role_of(c.role) != brief["child_role"]:
            raise LedgerError(f"{caller!r} is not the parent named in the brief for {child_name!r}")
        if brief["child_role"] == "manager":
            run_row = self.conn.execute(
                "SELECT repo_checked_at FROM runs WHERE run_id = ?", (brief["run_id"],)
            ).fetchone()
            if run_row is None or run_row["repo_checked_at"] is None:
                raise LedgerError(
                    "call repo_check first; create a branch or ask the user as its advice says"
                )
        spawned = self.conn.execute(
            "SELECT session_name FROM agents WHERE name = ? AND ended_at IS NULL", (child_name,)
        ).fetchone()
        if spawned is not None:
            raise LedgerError(
                f"{child_name!r} already runs as session {spawned['session_name']!r}; wait for its "
                "brief_ack, or call agent_resume if that session stopped"
            )

        session_name = session_name_for(self.repo_root, brief["run_id"], child_name)
        if session_name in sessions.live_names(sessions.list_sessions()):
            raise LedgerError(f"a running session already has the name {session_name!r}")
        cap = self.settings.parallelism_cap
        if cap is not None:
            live = self.conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND ended_at IS NULL",
                (brief["run_id"],),
            ).fetchone()["n"]
            if live >= cap:
                raise LedgerError(
                    f"the parallelism cap of {cap} is reached ({live} live sessions); call "
                    "agent_spawn again after a release frees a slot"
                )

        role = brief["child_role"]
        options = agentfiles.session_options(
            self.repo_root, role, brief["model"], serve.server_url(self.repo_root)
        )
        prompt = f"You are {child_name}. Read your brief from the swarm ledger and follow it."
        bg_id, session_id = sessions.spawn(prompt, session_name, options, cwd=self.repo_root)

        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, runtime, model, parent_agent_id, "
                "run_id, phase_id, module_id, file_id, state, session_name, bg_id, started_at) "
                f"VALUES (?, ?, ?, 'session', ?, ?, ?, ?, ?, ?, 'registered', ?, ?, {_NOW})",
                (
                    session_id,
                    child_name,
                    role,
                    brief["model"],
                    c.agent_id,
                    brief["run_id"],
                    brief["phase_id"],
                    brief["module_id"],
                    brief["file_id"],
                    session_name,
                    bg_id,
                ),
            )
            self._log_event(
                conn, session_id, None, "registered", f"agent_spawn: {session_name} ({bg_id})"
            )

        return self._agent_dict(session_id)

    def agent_resume(self, caller: str, agent_id: str, target_name: str) -> dict:
        c = resolve(self.conn, caller, agent_id)
        target = self.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND name = ? AND ended_at IS NULL",
            (c.run_id, target_name),
        ).fetchone()
        if target is None:
            raise LedgerError(f"no live agent named {target_name!r} in this run")
        if target["agent_id"] == c.agent_id:
            raise LedgerError("an agent cannot resume its own session")
        if sessions.is_live(target["agent_id"]):
            label = target["session_name"] or target["agent_id"]
            raise LedgerError(
                f"{target_name!r} is still running as {label!r}; wake it with "
                f"SendMessage(to={json.dumps(label)}) instead"
            )

        owed = _rows(
            self.conn.execute(
                "SELECT * FROM wakeups WHERE from_agent_id = ? AND to_agent_id = ? "
                "AND sent_at IS NULL ORDER BY wakeup_id",
                (c.agent_id, target["agent_id"]),
            )
        )
        message = " ".join(w["pointer"] for w in owed) or _RESUME_POINTER
        bg_id = sessions.resume(target["agent_id"], message, cwd=self.repo_root)

        with write_tx(self.conn) as conn:
            if bg_id is not None and bg_id != target["bg_id"]:
                conn.execute(
                    "UPDATE agents SET bg_id = ? WHERE agent_id = ?", (bg_id, target["agent_id"])
                )
            conn.execute(
                f"UPDATE wakeups SET sent_at = {_NOW} WHERE from_agent_id = ? "
                "AND to_agent_id = ? AND sent_at IS NULL",
                (c.agent_id, target["agent_id"]),
            )
            self._log_event(
                conn,
                target["agent_id"],
                target["state"],
                target["state"],
                f"agent_resume by {c.name}: {message}",
            )

        return {
            "agent": self._agent_dict(target["agent_id"]),
            "message": message,
            "wakeups_sent": len(owed),
        }

    # -- Owed wake-ups ----------------------------------------------------------

    def _owe_wakeup(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        to_agent_id: str | None,
        reason: str,
        pointer: str,
    ) -> dict | None:
        if to_agent_id is None or to_agent_id == c.agent_id:
            return None
        target = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ? AND ended_at IS NULL", (to_agent_id,)
        ).fetchone()
        if target is None or not target["session_name"]:
            return None
        cur = conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                c.run_id,
                c.agent_id,
                to_agent_id,
                target["name"],
                target["session_name"],
                reason,
                pointer,
            ),
        )
        return dict(
            conn.execute("SELECT * FROM wakeups WHERE wakeup_id = ?", (cur.lastrowid,)).fetchone()
        )

    def next_step(self, wakeup: dict | None) -> str | None:
        if wakeup is None:
            return None
        send = (
            f"SendMessage(to={json.dumps(wakeup['to_session_name'])}, "
            f"message={json.dumps(wakeup['pointer'])})"
        )
        resume = f"agent_resume(target_name={json.dumps(wakeup['to_name'])})"
        try:
            live = sessions.is_live(wakeup["to_agent_id"])
        except LedgerError:
            return f"{send}, or {resume} if that session is not running"
        return send if live else resume

    def owed_wakeups(self, agent_id: str) -> list[dict]:
        return _rows(
            self.conn.execute(
                "SELECT w.* FROM wakeups w JOIN agents a ON a.agent_id = w.to_agent_id "
                "WHERE w.from_agent_id = ? AND w.sent_at IS NULL AND a.ended_at IS NULL "
                "ORDER BY w.wakeup_id",
                (agent_id,),
            )
        )

    def wakeups_sent(self, agent_id: str, to_session_name: str) -> int:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                f"UPDATE wakeups SET sent_at = {_NOW} WHERE from_agent_id = ? "
                "AND to_session_name = ? AND sent_at IS NULL",
                (agent_id, to_session_name),
            )
        return cur.rowcount

    # -- File ownership -------------------------------------------------------

    def claim_file(
        self, caller: str, agent_id: str, path: str, test_path: str | None, for_name: str
    ) -> dict:
        if test_path is not None and test_path.strip().lower() in ("", "none", "null"):
            test_path = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "lead")
            if (
                conn.execute(
                    "SELECT 1 FROM files WHERE path = ? AND released_at IS NULL", (path,)
                ).fetchone()
                is not None
            ):
                raise LedgerError(f"path {path!r} already has a live claim")
            held = conn.execute(
                "SELECT path FROM files WHERE owner_agent_id = ? AND released_at IS NULL",
                (for_name,),
            ).fetchone()
            if held is not None:
                raise LedgerError(
                    f"{for_name!r} already holds the live claim on {held['path']!r}; "
                    "one Coder owns one file, so claim this path for a new Coder name"
                )

            cur = conn.execute(
                "INSERT INTO files (module_id, path, test_path, owner_agent_id, state, "
                f"claimed_at) VALUES (?, ?, ?, ?, 'claimed', {_NOW})",
                (c.module_id, path, test_path, for_name),
            )
            file_id = cur.lastrowid
            conn.execute(
                "UPDATE files SET state = 'superseded' WHERE path = ? AND file_id != ? "
                "AND released_at IS NOT NULL AND module_id IN (SELECT m.module_id FROM modules m "
                "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ?)",
                (path, file_id, c.run_id),
            )

        return dict(
            self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        )

    def release_file(self, caller: str, agent_id: str, path: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "lead")
            row = conn.execute(
                "SELECT * FROM files WHERE path = ? AND released_at IS NULL", (path,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"no live claim for {path!r}")
            conn.execute(
                f"UPDATE files SET released_at = {_NOW}, state = 'released' WHERE file_id = ?",
                (row["file_id"],),
            )
            file_id = row["file_id"]

        return dict(
            self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        )

    def who_owns(self, path: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ? AND released_at IS NULL", (path,)
        ).fetchone()
        if row is None:
            return {"path": path, "owner": None}
        return {"path": path, "owner": row["owner_agent_id"], "file": dict(row)}

    # -- Messages -------------------------------------------------------------

    def message_post(self, caller: str, agent_id: str, to_name: str, body: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            names = {
                row["name"]
                for row in conn.execute("SELECT name FROM agents WHERE run_id = ?", (c.run_id,))
            }
            if to_name not in names:
                raise LedgerError(
                    f"no agent named {to_name!r} in this run; registered names: {sorted(names)}"
                )
            cur = conn.execute(
                "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                (c.run_id, c.name, to_name, body),
            )
            message_id = cur.lastrowid
            recipient = conn.execute(
                "SELECT agent_id FROM agents WHERE run_id = ? AND name = ? AND ended_at IS NULL",
                (c.run_id, to_name),
            ).fetchone()
            wakeup = self._owe_wakeup(
                conn,
                c,
                recipient["agent_id"] if recipient is not None else None,
                "message_post",
                f"Message {message_id} from {c.name} is waiting in the ledger; "
                "read it with message_inbox.",
            )

        message = dict(
            self.conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        )
        message["next"] = self.next_step(wakeup)
        return message

    def message_inbox(self, caller: str, agent_id: str) -> list[dict]:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            rows = conn.execute(
                "SELECT message_id FROM messages WHERE to_name = ? AND read_at IS NULL "
                "ORDER BY message_id",
                (c.name,),
            ).fetchall()
            ids = [row["message_id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET read_at = {_NOW} WHERE message_id IN ({placeholders})",
                    ids,
                )
            result = [
                dict(
                    conn.execute(
                        "SELECT * FROM messages WHERE message_id = ?", (message_id,)
                    ).fetchone()
                )
                for message_id in ids
            ]

        return result

    # -- Directives -------------------------------------------------------------

    def directive_submit(
        self, source: str, sender_name: str | None, body: str, reply_to: int | None = None
    ) -> dict:
        if source not in _DIRECTIVE_SOURCES:
            raise LedgerError(
                f"unknown directive source {source!r}; use one of {sorted(_DIRECTIVE_SOURCES)}. "
                "An agent that needs the Oracle uses message_post"
            )
        with write_tx(self.conn) as conn:
            run = self._active_run(conn)
            if reply_to is not None:
                parent = conn.execute(
                    "SELECT 1 FROM directives WHERE directive_id = ? AND run_id = ?",
                    (reply_to, run["run_id"]),
                ).fetchone()
                if parent is None:
                    raise LedgerError(f"reply_to {reply_to!r} names no directive of this run")
                conn.execute(
                    f"UPDATE directives SET state = 'resolved', resolved_at = {_NOW} "
                    "WHERE directive_id = ? AND state = 'open' AND outcome = 'needs_user'",
                    (reply_to,),
                )
            cur = conn.execute(
                "INSERT INTO directives (run_id, source, sender_name, body, reply_to) "
                "VALUES (?, ?, ?, ?, ?)",
                (run["run_id"], source, sender_name, body, reply_to),
            )
            directive_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM directives WHERE directive_id = ?", (directive_id,)
            ).fetchone()
        )

    def directive_inbox(self, caller: str, agent_id: str) -> list[dict]:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            conn.execute(
                f"UPDATE directives SET notified_at = {_NOW} WHERE run_id = ? "
                "AND state = 'open' AND notified_at IS NULL",
                (c.run_id,),
            )
            return _rows(
                conn.execute(
                    "SELECT * FROM directives WHERE run_id = ? AND state = 'open' "
                    "ORDER BY directive_id",
                    (c.run_id,),
                )
            )

    def watch_armed(self, timeout_ms: int | None) -> dict:
        expires = (
            f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+{timeout_ms / 1000:.3f} seconds')"
            if timeout_ms is not None and timeout_ms > 0
            else "NULL"
        )
        with write_tx(self.conn) as conn:
            run = self._active_run(conn)
            conn.execute(
                f"UPDATE runs SET watch_heartbeat_at = {_NOW}, watch_expires_at = {expires} "
                "WHERE run_id = ?",
                (run["run_id"],),
            )
        return dict(
            self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)).fetchone()
        )

    def directive_resolve(
        self, caller: str, agent_id: str, directive_id: int, outcome: str, resolution: str
    ) -> dict:
        if outcome not in _DIRECTIVE_OUTCOMES:
            raise LedgerError(
                f"unknown directive outcome {outcome!r}; use one of {list(_DIRECTIVE_OUTCOMES)}"
            )
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            if (
                conn.execute(
                    "SELECT 1 FROM directives WHERE directive_id = ?", (directive_id,)
                ).fetchone()
                is None
            ):
                raise LedgerError(f"unknown directive_id {directive_id!r}")
            if outcome == "needs_user":
                conn.execute(
                    "UPDATE directives SET state = 'open', outcome = ?, resolution = ?, "
                    "resolved_at = NULL WHERE directive_id = ?",
                    (outcome, resolution, directive_id),
                )
            else:
                conn.execute(
                    f"UPDATE directives SET state = 'resolved', outcome = ?, resolution = ?, "
                    f"resolved_at = {_NOW} WHERE directive_id = ?",
                    (outcome, resolution, directive_id),
                )

        return dict(
            self.conn.execute(
                "SELECT * FROM directives WHERE directive_id = ?", (directive_id,)
            ).fetchone()
        )

    # -- Overrides -------------------------------------------------------------

    def override_grant(
        self,
        caller: str,
        agent_id: str,
        rule: str,
        target_agent_name: str,
        target: str,
        reason: str,
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            cur = conn.execute(
                "INSERT INTO overrides (run_id, rule, target_agent_name, target, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (c.run_id, rule, target_agent_name, target, reason),
            )
            override_id = cur.lastrowid

        return dict(
            self.conn.execute(
                "SELECT * FROM overrides WHERE override_id = ?", (override_id,)
            ).fetchone()
        )

    def override_consume(self, rule: str, agent_name: str, target: str) -> bool:
        with write_tx(self.conn) as conn:
            row = conn.execute(
                "SELECT override_id FROM overrides WHERE rule = ? AND target_agent_name = ? "
                "AND target = ? AND used_at IS NULL ORDER BY created_at ASC LIMIT 1",
                (rule, agent_name, target),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                f"UPDATE overrides SET used_at = {_NOW} WHERE override_id = ?",
                (row["override_id"],),
            )

        return True

    # -- Issues -------------------------------------------------------------

    def issue_open(self, caller: str, agent_id: str, file_id: int, title: str, body: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            round_ = _ISSUE_OPEN_ROUND.get(c.role, 1)
            cur = conn.execute(
                "INSERT INTO issues (run_id, file_id, opened_by_agent_id, title, body, state, "
                "round) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (c.run_id, file_id, c.agent_id, title, body, round_),
            )
            issue_id = cur.lastrowid

        return dict(
            self.conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        )

    def issue_list(self, caller: str, agent_id: str, file_id: int | None = None) -> list[dict]:
        c = resolve(self.conn, caller, agent_id)
        if file_id is not None:
            rows = _rows(
                self.conn.execute(
                    "SELECT * FROM issues WHERE run_id = ? AND file_id = ? ORDER BY issue_id",
                    (c.run_id, file_id),
                )
            )
        else:
            rows = _rows(
                self.conn.execute(
                    "SELECT * FROM issues WHERE run_id = ? ORDER BY issue_id", (c.run_id,)
                )
            )
        if c.role != "lead":
            return rows
        return [row for row in rows if not self._hidden_from_lead(c, row)]

    def _lead_scored_current_handoff(self, file_id: int) -> bool:
        handoff = self.conn.execute(
            "SELECT created_at FROM handoffs WHERE file_id = ? ORDER BY handoff_id DESC LIMIT 1",
            (file_id,),
        ).fetchone()
        since = handoff["created_at"] if handoff is not None else None
        row = self.conn.execute(
            "SELECT 1 FROM reviews WHERE file_id = ? AND kind = 'lead' "
            "AND (? IS NULL OR created_at >= ?) LIMIT 1",
            (file_id, since, since),
        ).fetchone()
        return row is not None

    def _opened_by_self_review(self, row: dict) -> bool:
        if row["dimension"] is None or row["criterion"] is None:
            return False
        opener = self.conn.execute(
            "SELECT role FROM agents WHERE agent_id = ?", (row["opened_by_agent_id"],)
        ).fetchone()
        return opener is not None and opener["role"] == "coder"

    def _hidden_from_lead(self, c: Caller, row: dict) -> bool:
        # A Lead scores blind: a self-review issue is hidden from that Lead's own view of
        # its own module's file until the Lead records its own score for the handoff.
        if row["file_id"] is None:
            return False
        file_row = self.conn.execute(
            "SELECT module_id FROM files WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        if file_row is None or file_row["module_id"] != c.module_id:
            return False
        if self._lead_scored_current_handoff(row["file_id"]):
            return False
        return self._opened_by_self_review(row)

    def idea_record(
        self, caller: str, agent_id: str, issue_id: int, body: str, outcome: str
    ) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            cur = conn.execute(
                "INSERT INTO ideas (issue_id, agent_id, body, outcome) VALUES (?, ?, ?, ?)",
                (issue_id, c.agent_id, body, outcome),
            )
            idea_id = cur.lastrowid

        return dict(
            self.conn.execute("SELECT * FROM ideas WHERE idea_id = ?", (idea_id,)).fetchone()
        )

    def _issue_chain(self, conn: sqlite3.Connection, issue: sqlite3.Row) -> list[sqlite3.Row]:
        anchor = None
        if issue["file_id"] is not None:
            owner = conn.execute(
                "SELECT owner_agent_id FROM files WHERE file_id = ?", (issue["file_id"],)
            ).fetchone()
            if owner is not None and owner["owner_agent_id"] is not None:
                anchor = conn.execute(
                    "SELECT * FROM agents WHERE run_id = ? AND (agent_id = ? OR name = ?) "
                    "ORDER BY ended_at IS NOT NULL LIMIT 1",
                    (issue["run_id"], owner["owner_agent_id"], owner["owner_agent_id"]),
                ).fetchone()
        if anchor is None:
            anchor = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (issue["opened_by_agent_id"],)
            ).fetchone()
        chain = []
        while anchor is not None:
            chain.append(anchor)
            anchor = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (anchor["parent_agent_id"],)
            ).fetchone()
        return chain

    def issue_escalate(self, caller: str, agent_id: str, issue_id: int) -> dict:
        wakeup = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            row = conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown issue_id {issue_id!r}")
            chain = self._issue_chain(conn, row)
            if c.agent_id not in {a["agent_id"] for a in chain}:
                raise LedgerError(
                    f"{c.name!r} may not escalate issue {issue_id!r}; only its owner and the "
                    f"owner's parent chain may: {[a['name'] for a in chain]}"
                )
            next_round = row["round"] + 1
            if next_round > self.settings.escalation.rounds:
                raise LedgerError(f"issue {issue_id!r} is already at the maximum escalation round")
            target_role = "manager" if next_round == 2 else "oracle"
            target = next((a for a in chain if a["role"] == target_role), chain[-1])
            conn.execute(
                "UPDATE issues SET round = ?, attempts = 0, escalated_to = ? WHERE issue_id = ?",
                (next_round, target["name"], issue_id),
            )
            pointer = (
                f"Issue {issue_id} is escalated to you for round {next_round}. "
                "Read it with issue_list."
            )
            conn.execute(
                "INSERT INTO messages (run_id, from_name, to_name, body) VALUES (?, ?, ?, ?)",
                (row["run_id"], c.name, target["name"], pointer),
            )
            wakeup = self._owe_wakeup(conn, c, target["agent_id"], "issue_escalate", pointer)

        result = dict(
            self.conn.execute("SELECT * FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        )
        result["next"] = self.next_step(wakeup)
        return result

    # -- Hook support ---------------------------------------------------------

    def mark_stale(self, agent_id: str, path: str) -> dict:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                f"UPDATE files SET stale_since = {_NOW} WHERE released_at IS NULL "
                "AND file_id = (SELECT file_id FROM agents WHERE agent_id = ?) "
                "AND (path = ? OR test_path = ?)",
                (agent_id, path, path),
            )
        return {"path": path, "updated": cur.rowcount > 0}

    def agent_transcript(self, agent_id: str, transcript_path: str) -> None:
        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET transcript_path = ? WHERE agent_id = ?",
                (transcript_path, agent_id),
            )

    def agent_idle(self, agent_id: str, reason: str) -> bool:
        return self._agent_transition(agent_id, "working", "idle", reason)

    def agent_active(self, agent_id: str, reason: str) -> bool:
        return self._agent_transition(agent_id, "idle", "working", reason)

    def _agent_transition(self, agent_id: str, from_state: str, to_state: str, reason: str) -> bool:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                "UPDATE agents SET state = ? WHERE agent_id = ? AND state = ? AND ended_at IS NULL",
                (to_state, agent_id, from_state),
            )
            if cur.rowcount == 0:
                return False
            self._log_event(conn, agent_id, from_state, to_state, reason)
        return True

    def agent_compacted(self, agent_id: str) -> dict:
        with write_tx(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET context_overflow_count = "
                "COALESCE(context_overflow_count, 0) + 1 WHERE agent_id = ?",
                (agent_id,),
            )
        return self._agent_dict(agent_id)

    # -- Events -------------------------------------------------------------

    def events(self, agent_id: str | None = None, limit: int = 200) -> list[dict]:
        if agent_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM agent_events WHERE agent_id = ? ORDER BY event_id DESC LIMIT ?",
                (agent_id, limit),
            )
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_events ORDER BY event_id DESC LIMIT ?", (limit,)
            )
        return _rows(rows)
