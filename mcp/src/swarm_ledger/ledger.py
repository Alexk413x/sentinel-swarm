from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import __version__, agentfiles, lock, sessions, wake
from .agreements import AgreementsMixin
from .db import connect, ensure_git_exclude, ledger_path, write_tx
from .drive import DriveMixin, findings_named
from .identity import (
    EFFORT_LEVELS,
    ROLES,
    Caller,
    LedgerError,
    child_roles_of,
    require_role,
    resolve,
)
from .oversight import OversightMixin
from .repo import RepoMixin
from .review import ReviewMixin, agent_names, live_agents, run_summary
from .settings import Settings, load_settings

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_RESUME_POINTER = "Re-read your brief and your inbox in the ledger."
_PHASE_STATES = ("planned", "unlocked", "working", "handed_up", "approved")
_DIRECTIVE_SOURCES = ("user_chat", "outside_session", "skill", "watchdog", "driver")
_DIRECTIVE_SOURCE_ALIASES = {"user-chat": "user_chat", "outside-session": "outside_session"}
_DIRECTIVE_OUTCOMES = ("applied", "scheduled", "declined", "needs_user")
_ISSUE_OPEN_ROUND = {"manager": 2, "oracle": 3}
_LIVE_RUN = "state IN ('active', 'paused')"
MESSAGE_BODY_MAX = 32_000
INBOX_BODY_BUDGET = 40_000
INBOX_CLAIM_SECONDS = 120
_MESSAGE_COLUMNS = "message_id, run_id, from_name, to_name, body, read_at, created_at"
_UNCLAIMED = (
    f"(claim_id IS NULL OR claimed_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',"
    f"'-{INBOX_CLAIM_SECONDS} seconds'))"
)
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A build command that serves or watches reloads on edits, and the Driver must test the build
# it made at the start of an exploration. profile_set refuses any of these words.
SERVER_WORDS = frozenset({"--watch", "serve", "dev-server"})
PACKAGE_MANAGERS = frozenset({"npm", "yarn", "pnpm"})
SERVER_SCRIPTS = frozenset({"dev", "start"})
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


def session_name_for(repo_root: Path, run_id: int, child_name: str, stamp: str = "") -> str:
    run = f"r{run_id}-{stamp}" if stamp else f"r{run_id}"
    return f"{repo_slug(repo_root)}-{run}-{child_name}"


def run_stamp(started_at: str) -> str:
    # A stale session, local or over Remote Control, keeps its name after it ends, and a
    # rebuilt host restarts at run 1. The run's start time keeps a new session's name unique.
    return started_at[5:7] + started_at[8:10] + started_at[11:13] + started_at[14:16]


# Matches the shape session_name_for builds for any repo and run: "<slug>-r<run_id>-<name>".
# Used to count other swarms' live sessions against parallelism_cap, without knowing their slug.
_SWARM_SESSION_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-r\d+-.+$")


def looks_like_swarm_session(name: str) -> bool:
    return bool(_SWARM_SESSION_RE.match(name))


def server_word(command: str) -> str | None:
    words = command.split()
    for index, word in enumerate(words):
        if word in SERVER_WORDS or word.startswith("--watch="):
            return word
        if Path(word).stem.lower() not in PACKAGE_MANAGERS:
            continue
        rest = words[index + 1 :]
        if rest[:1] in (["run"], ["run-script"]):
            rest = rest[1:]
        if rest[:1] and rest[0] in SERVER_SCRIPTS:
            return " ".join(words[index : len(words) - len(rest) + 1])
    return None


def require_coder_name(name: str, ordinal: int, module_name: str) -> None:
    prefix = f"coder-p{ordinal}-{module_name}-"
    if not (name.startswith(prefix) and SLUG_RE.fullmatch(name[len(prefix) :])):
        raise LedgerError(
            f"a Coder of module {module_name!r} must be named {prefix}<file slug>, such as "
            f"{prefix}login, not {name!r}"
        )


def require_slug(label: str, value: str) -> None:
    if not SLUG_RE.fullmatch(value):
        raise LedgerError(
            f"{label} {value!r} is not a slug: use lowercase letters and digits joined by "
            "single hyphens, such as 'auth' or 'user-store'"
        )


class Ledger(AgreementsMixin, ReviewMixin, RepoMixin, OversightMixin, DriveMixin):
    # AgreementsMixin first: ReviewMixin declares stub bodies for the gate methods
    # AgreementsMixin implements (for pyright, since review.py's methods are typed
    # against ReviewMixin alone), and MRO resolves the first base's attribute, so
    # ReviewMixin's empty stub would otherwise shadow the real implementation.
    def __init__(
        self,
        repo_root: Path,
        *,
        db_path: Path | None = None,
        server_pid: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.server_pid = server_pid if server_pid is not None else os.getpid()
        self.settings = settings if settings is not None else load_settings(repo_root)
        path = db_path if db_path is not None else ledger_path(repo_root)
        self.conn = connect(path)
        self._pending_stops: list[tuple[str, str]] = []
        if (repo_root / ".git").exists():
            ensure_git_exclude(repo_root)

    def adopt_run_profile(self, run_id: int | None) -> None:
        # profile_set may have run in another worker process, so the run's snapshot, not this
        # process's settings, holds the current commands.
        row = self.conn.execute(
            "SELECT settings_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            self.settings.adopt_profile(row["settings_json"])

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

        resumed: dict | None = None
        run_id: int | None = None
        with write_tx(self.conn) as conn:
            active = conn.execute(f"SELECT * FROM runs WHERE {_LIVE_RUN}").fetchone()
            if active is not None:
                self._refuse_live_oracle(conn, active, session_id, listing, listing_error)
                resumed = self._run_resume(conn, active, session_id, session_name)
            else:
                run_id = self._open_run(conn, prd, session_id, oracle_name, session_name)

        if resumed is not None:
            return resumed | {"oracle": self._agent_dict(session_id)}
        run = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return {"run": dict(run), "oracle": self._agent_dict(session_id), "resumed": False}

    def _open_run(
        self,
        conn: sqlite3.Connection,
        prd: str,
        session_id: str,
        oracle_name: str,
        session_name: str | None,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO runs (prd, state, plugin_version, settings_json) "
            "VALUES (?, 'active', ?, ?)",
            (prd, __version__, self.settings.snapshot()),
        )
        run_id = cur.lastrowid
        assert run_id is not None
        lock.acquire(self.repo_root, run_id, self.server_pid)

        conn.execute(
            "INSERT INTO agents (agent_id, name, role, runtime, model, effort, run_id, state, "
            f"session_name, started_at) VALUES (?, ?, 'oracle', ?, ?, ?, ?, 'working', ?, {_NOW})",
            (
                session_id,
                oracle_name,
                self.settings.runtime.get("oracle"),
                agentfiles.oracle_model(self.repo_root, self.settings.models.get("oracle", [])),
                self.settings.effort.get("oracle"),
                run_id,
                session_name,
            ),
        )
        self._log_event(conn, session_id, None, "working", "run_start")
        return run_id

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
                    oracle["effort"] or self.settings.effort.get("oracle"),
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

    def run_pause(
        self, caller: str, agent_id: str, reason: str, phases: list[int] | None = None
    ) -> dict:
        if not reason.strip():
            raise LedgerError("run_pause needs a reason the user can act on")
        if phases:
            return self._pause_phases(caller, agent_id, reason, phases)
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

    def _pause_phases(self, caller: str, agent_id: str, reason: str, phase_ids: list[int]) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            run = self._active_run(conn)
            rows = _rows(
                conn.execute(
                    "SELECT * FROM phases WHERE run_id = ? AND phase_id IN "
                    f"({','.join('?' for _ in phase_ids)})",
                    (run["run_id"], *phase_ids),
                )
            )
            found = {row["phase_id"] for row in rows}
            missing = [pid for pid in phase_ids if pid not in found]
            if missing:
                raise LedgerError(f"unknown phase_id(s) for this run: {missing}")
            conn.execute(
                f"UPDATE phases SET paused_at = {_NOW}, pause_reason = ? WHERE phase_id IN "
                f"({','.join('?' for _ in phase_ids)})",
                (reason, *phase_ids),
            )

        paused = _rows(
            self.conn.execute(
                "SELECT * FROM phases WHERE phase_id IN "
                f"({','.join('?' for _ in phase_ids)}) ORDER BY ordinal",
                phase_ids,
            )
        )
        run_row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        return dict(run_row) | {"reason": reason, "paused_phases": paused}

    def phase_resume(self, caller: str, agent_id: str, phase_ids: list[int]) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            rows = _rows(
                conn.execute(
                    "SELECT * FROM phases WHERE run_id = ? AND phase_id IN "
                    f"({','.join('?' for _ in phase_ids)})",
                    (c.run_id, *phase_ids),
                )
            )
            found = {row["phase_id"] for row in rows}
            missing = [pid for pid in phase_ids if pid not in found]
            if missing:
                raise LedgerError(f"unknown phase_id(s) for this run: {missing}")
            conn.execute(
                "UPDATE phases SET paused_at = NULL, pause_reason = NULL WHERE phase_id IN "
                f"({','.join('?' for _ in phase_ids)})",
                phase_ids,
            )

        return {
            "resumed_phases": _rows(
                self.conn.execute(
                    "SELECT * FROM phases WHERE phase_id IN "
                    f"({','.join('?' for _ in phase_ids)}) ORDER BY ordinal",
                    phase_ids,
                )
            )
        }

    def run_status(self, caller: str, agent_id: str, include_prd: bool = False) -> dict:
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
        names = agent_names(conn, run_id)
        files = _rows(
            conn.execute(
                "SELECT files.* FROM files "
                "JOIN modules ON modules.module_id = files.module_id "
                "JOIN phases ON phases.phase_id = modules.phase_id "
                "WHERE phases.run_id = ? ORDER BY files.file_id",
                (run_id,),
            )
        )
        for file_row in files:
            file_row["owner"] = names.get(file_row.pop("owner_agent_id"))
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
            "run": run_summary(run, include_prd),
            "phases": phases,
            "modules": modules,
            "files": files,
            "agents": live_agents(conn, run_id),
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
            self._block_run_finish_for_driver(run_id)

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
        lock.release(self.repo_root, run_id)

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
        word = server_word(build_command) if build_command is not None else None
        if word is not None:
            raise LedgerError(
                f"build_command {build_command!r} starts a server or a watcher ({word!r}). The "
                "Driver tests one build made at the start of an exploration, never a server "
                "that reloads on edits: name the one-shot build, such as 'npm run build'"
            )
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "oracle")
            if c.run_id is None:
                raise LedgerError(f"{caller!r} has no run")

            self.adopt_run_profile(c.run_id)
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
            if not name.startswith(f"p{ordinal}-"):
                name = f"p{ordinal}-{name}"
            require_slug("phase name", name)
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

            if state == "unlocked":
                waiting = [
                    f"{row['phase_id']} ({row['name']}, {row['state']})"
                    for row in conn.execute(
                        "SELECT p.phase_id, p.name, p.state FROM phase_deps d "
                        "JOIN phases p ON p.phase_id = d.depends_on_phase_id "
                        "WHERE d.phase_id = ? AND p.state != 'approved' ORDER BY p.ordinal",
                        (phase_id,),
                    )
                ]
                if waiting:
                    raise LedgerError(
                        f"phase {phase_id} waits on phase(s) that are not approved: "
                        f"{', '.join(waiting)}. A phase unlocks once every phase it depends "
                        "on is approved"
                    )

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
            "SELECT * FROM phases WHERE run_id = ? AND state != 'approved' "
            "AND paused_at IS NULL ORDER BY ordinal",
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

    def module_add(
        self,
        caller: str,
        agent_id: str,
        phase_id: int,
        name: str,
        depends_on: list[int] | None = None,
    ) -> dict:
        depends_on = depends_on or []
        require_slug("module name", name)
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "manager")
            if c.phase_id != phase_id:
                raise LedgerError("a manager may only add modules to its own phase")
            outside = [
                dep_id
                for dep_id in depends_on
                if conn.execute(
                    "SELECT 1 FROM modules WHERE module_id = ? AND phase_id = ?",
                    (dep_id, phase_id),
                ).fetchone()
                is None
            ]
            if outside:
                raise LedgerError(
                    f"depends_on names module id(s) {outside} outside phase {phase_id}; a "
                    "module depends only on modules of its own phase. A dependency on another "
                    "phase belongs in the phase plan"
                )
            cur = conn.execute(
                "INSERT INTO modules (phase_id, name, state) VALUES (?, ?, 'planned')",
                (phase_id, name),
            )
            module_id = cur.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO module_deps (module_id, depends_on_module_id) VALUES (?, ?)",
                [(module_id, dep_id) for dep_id in depends_on],
            )

        module = dict(
            self.conn.execute("SELECT * FROM modules WHERE module_id = ?", (module_id,)).fetchone()
        )
        module["depends_on"] = list(depends_on)
        return module

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
        finding_ids: list[int] | None = None,
        effort: str | None = None,
        contract: str | None = None,
    ) -> dict:
        if child_role == "driver":
            raise LedgerError(
                "a Driver starts only through drive_request(focus), which opens the "
                "exploration its findings belong to"
            )
        return self._write_brief(
            caller,
            agent_id,
            child_name,
            child_role,
            model,
            body,
            phase_id,
            module_id,
            file_id,
            finding_ids,
            effort,
            contract,
        )

    def _write_brief(
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
        finding_ids: list[int] | None = None,
        effort: str | None = None,
        contract: str | None = None,
    ) -> dict:
        if effort is not None and effort not in EFFORT_LEVELS:
            raise LedgerError(f"unknown effort {effort!r}; use one of {list(EFFORT_LEVELS)}")
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            if child_role not in child_roles_of(c.role):
                raise LedgerError(f"a {c.role!r} may not brief a {child_role!r}")
            if model not in self.settings.models.get(child_role, []):
                raise LedgerError(f"model {model!r} is not approved for {child_role!r}")
            self._check_child_scope(conn, c, child_role, child_name, phase_id, module_id, file_id)
            missing = self._missing_contracts(conn, child_role, module_id, file_id)
            if missing:
                raise LedgerError(
                    f"brief each dependency first, with its contract: {'; '.join(missing)} "
                    "has no contract on its latest brief. Helpers come before the files and "
                    "modules that use them; pass contract= on the helper's brief_create"
                )
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
            if finding_ids is None:
                if c.role == "oracle" and c.run_id is not None:
                    self._require_finding_ids(conn, c.run_id)
                finding_ids = self._inherited_finding_ids(conn, c.agent_id)
            if c.run_id is not None:
                self._block_fix_for_stops(conn, c.run_id, finding_ids)
                if child_role == "coder":
                    self._require_fix_claim(conn, c.run_id, file_id, finding_ids)

            cur = conn.execute(
                "INSERT INTO briefs "
                "(run_id, parent_agent_id, child_name, child_role, model, effort, body, "
                "phase_id, module_id, file_id, finding_ids_json, contract) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.run_id,
                    c.agent_id,
                    child_name,
                    child_role,
                    model,
                    effort,
                    body,
                    phase_id if phase_id is not None else c.phase_id,
                    module_id if module_id is not None else c.module_id,
                    file_id if file_id is not None else c.file_id,
                    json.dumps(finding_ids) if finding_ids else None,
                    contract.strip() if contract and contract.strip() else None,
                ),
            )
            brief_id = cur.lastrowid

        return dict(
            self.conn.execute("SELECT * FROM briefs WHERE brief_id = ?", (brief_id,)).fetchone()
        )

    def _check_child_scope(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        child_role: str,
        child_name: str,
        phase_id: int | None,
        module_id: int | None,
        file_id: int | None,
    ) -> None:
        if child_role == "manager":
            phase = conn.execute(
                "SELECT * FROM phases WHERE phase_id = ? AND run_id = ?", (phase_id, c.run_id)
            ).fetchone()
            if phase is None:
                raise LedgerError(
                    f"a Manager brief needs the phase_id of a phase of this run, not {phase_id!r}"
                )
            if phase["state"] == "planned":
                raise LedgerError(
                    f"phase {phase_id} ({phase['name']}) is still planned; call "
                    "phase_update(phase_id, 'unlocked') once every phase it depends on is "
                    "approved, then brief its Manager"
                )
            expected = f"mgr-{phase['name']}"
        elif child_role == "lead":
            module = conn.execute(
                "SELECT m.*, p.ordinal FROM modules m JOIN phases p ON p.phase_id = m.phase_id "
                "WHERE m.module_id = ?",
                (module_id,),
            ).fetchone()
            if module is None or module["phase_id"] != c.phase_id:
                raise LedgerError(
                    f"a Lead brief needs the module_id of a module of your own phase "
                    f"{c.phase_id}, not {module_id!r}; add it with module_add first"
                )
            expected = f"lead-p{module['ordinal']}-{module['name']}"
        elif child_role == "coder":
            file_row = conn.execute(
                "SELECT f.*, m.name AS module_name, p.ordinal FROM files f "
                "JOIN modules m ON m.module_id = f.module_id "
                "JOIN phases p ON p.phase_id = m.phase_id WHERE f.file_id = ?",
                (file_id,),
            ).fetchone()
            if file_row is None or file_row["module_id"] != c.module_id:
                raise LedgerError(
                    f"a Coder brief needs the file_id of a file your module claimed, not "
                    f"{file_id!r}; call claim_file first"
                )
            if file_row["released_at"] is not None or file_row["owner_agent_id"] != child_name:
                raise LedgerError(
                    f"file {file_id} ({file_row['path']}) has no live claim for "
                    f"{child_name!r}; call claim_file(path, test_path, for_name="
                    f"{child_name!r}) first, or release_file and claim it again for a new Coder"
                )
            require_coder_name(child_name, file_row["ordinal"], file_row["module_name"])
            return
        else:
            return
        if child_name != expected:
            raise LedgerError(
                f"this {child_role} must be named {expected!r}, not {child_name!r}. Names "
                "are mgr-<phase name>, lead-p<ordinal>-<module>, and "
                "coder-p<ordinal>-<module>-<file slug>"
            )

    def _missing_contracts(
        self,
        conn: sqlite3.Connection,
        child_role: str,
        module_id: int | None,
        file_id: int | None,
    ) -> list[str]:
        return [
            f"{dep['label']} (id {dep['id']})"
            for dep in self._dependencies(conn, child_role, module_id, file_id)
            if not dep["contract"]
        ]

    def _dependencies(
        self,
        conn: sqlite3.Connection,
        child_role: str,
        module_id: int | None,
        file_id: int | None,
    ) -> list[dict]:
        if child_role == "coder" and file_id is not None:
            rows = conn.execute(
                "SELECT f.file_id AS id, f.path AS label FROM file_deps d "
                "JOIN files f ON f.file_id = d.depends_on_file_id WHERE d.file_id = ? "
                "ORDER BY f.file_id",
                (file_id,),
            ).fetchall()
            column = "file_id"
        elif child_role == "lead" and module_id is not None:
            rows = conn.execute(
                "SELECT m.module_id AS id, m.name AS label FROM module_deps d "
                "JOIN modules m ON m.module_id = d.depends_on_module_id WHERE d.module_id = ? "
                "ORDER BY m.module_id",
                (module_id,),
            ).fetchall()
            column = "module_id"
        else:
            return []
        deps = []
        for row in rows:
            latest = conn.execute(
                f"SELECT contract FROM briefs WHERE {column} = ? AND child_role = ? "
                "ORDER BY brief_id DESC LIMIT 1",
                (row["id"], child_role),
            ).fetchone()
            contract = latest["contract"] if latest is not None else None
            deps.append({"id": row["id"], "label": row["label"], "contract": contract})
        return deps

    def _inherited_finding_ids(self, conn: sqlite3.Connection, agent_id: str) -> list[int]:
        row = conn.execute(
            "SELECT finding_ids_json FROM briefs WHERE acked_by_agent_id = ? "
            "ORDER BY brief_id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        return json.loads(row["finding_ids_json"]) if row and row["finding_ids_json"] else []

    def brief_get(self, caller_name: str, child_name: str) -> dict:
        del caller_name
        row = self.conn.execute(
            "SELECT * FROM briefs WHERE child_name = ? "
            "ORDER BY created_at DESC, brief_id DESC LIMIT 1",
            (child_name,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"no brief found for {child_name!r}")
        brief = dict(row)
        brief["findings"] = findings_named(
            self.conn, json.loads(brief["finding_ids_json"] or "[]"), evidence=True
        )
        brief["depends_on_contracts"] = [
            {"id": dep["id"], "name": dep["label"], "contract": dep["contract"]}
            for dep in self._dependencies(
                self.conn, brief["child_role"], brief["module_id"], brief["file_id"]
            )
        ]
        return brief

    def brief_read(self, agent_id: str, child_name: str) -> bool:
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                f"UPDATE briefs SET last_read_by_child_at = {_NOW} WHERE brief_id = ("
                "SELECT b.brief_id FROM briefs b JOIN agents a ON a.agent_id = ? "
                "AND a.name = b.child_name AND a.ended_at IS NULL "
                "WHERE b.child_name = ? ORDER BY b.created_at DESC, b.brief_id DESC LIMIT 1)",
                (agent_id, child_name),
            )
        return cur.rowcount > 0

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
        if brief["parent_agent_id"] != c.agent_id or brief["child_role"] not in child_roles_of(
            c.role
        ):
            raise LedgerError(f"{caller!r} is not the parent named in the brief for {child_name!r}")
        if brief["child_role"] == "manager":
            phase = self.conn.execute(
                "SELECT name, state FROM phases WHERE phase_id = ?", (brief["phase_id"],)
            ).fetchone()
            if phase is None or phase["state"] == "planned":
                raise LedgerError(
                    f"phase {brief['phase_id']} is not unlocked; call phase_update(phase_id, "
                    "'unlocked') once every phase it depends on is approved"
                )
        self._block_fix_for_stops(
            self.conn, brief["run_id"], json.loads(brief["finding_ids_json"] or "[]")
        )
        if brief["child_role"] == "manager":
            run_row = self.conn.execute(
                "SELECT repo_checked_at FROM runs WHERE run_id = ?", (brief["run_id"],)
            ).fetchone()
            if run_row is None or run_row["repo_checked_at"] is None:
                raise LedgerError(
                    "call repo_check first; create a branch or ask the user as its advice says"
                )
        if brief["phase_id"] is not None:
            paused = self.conn.execute(
                "SELECT pause_reason FROM phases WHERE phase_id = ? AND paused_at IS NOT NULL",
                (brief["phase_id"],),
            ).fetchone()
            if paused is not None:
                raise LedgerError(
                    f"phase {brief['phase_id']} is paused: {paused['pause_reason']}; call "
                    "phase_resume before spawning into it"
                )
        spawned = self.conn.execute(
            "SELECT session_name FROM agents WHERE name = ? AND ended_at IS NULL", (child_name,)
        ).fetchone()
        if spawned is not None:
            raise LedgerError(
                f"{child_name!r} already runs as session {spawned['session_name']!r}; wait for its "
                "brief_ack, or call agent_resume if that session stopped"
            )

        session_name = self.session_name(brief["run_id"], child_name)
        listing = sessions.list_sessions()
        if session_name in sessions.live_names(listing):
            raise LedgerError(f"a running session already has the name {session_name!r}")

        role = brief["child_role"]
        cap = self.settings.parallelism_cap
        if cap is not None:
            own_names = {
                row["session_name"]
                for row in self.conn.execute(
                    "SELECT session_name FROM agents WHERE run_id = ? AND ended_at IS NULL "
                    "AND session_name IS NOT NULL",
                    (brief["run_id"],),
                )
            }
            own_live = self.conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND ended_at IS NULL",
                (brief["run_id"],),
            ).fetchone()["n"]
            other_swarms = sum(
                1
                for entry in listing
                if sessions.is_running(entry)
                and str(entry.get("name") or "") not in own_names
                and looks_like_swarm_session(str(entry.get("name") or ""))
            )
            live = own_live + other_swarms
            if live >= cap:
                raise LedgerError(
                    f"the parallelism cap of {cap} is reached ({live} live sessions, "
                    f"{other_swarms} from other swarms on this machine); call agent_spawn again "
                    "after a release frees a slot"
                )
        role_cap = self.settings.role_parallelism_cap.get(role)
        if role_cap is not None:
            live_role = self.conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND role = ? "
                "AND ended_at IS NULL",
                (brief["run_id"], role),
            ).fetchone()["n"]
            if live_role >= role_cap:
                raise LedgerError(
                    f"the {role} parallelism cap of {role_cap} is reached ({live_role} live "
                    f"{role} sessions); call agent_spawn again after a release frees a slot"
                )

        effort = brief["effort"] or self.settings.effort.get(role)
        options = self._session_options(role, brief["model"], effort)
        prompt = f"You are {child_name}. Read your brief from the swarm ledger and follow it."
        bg_id, session_id = sessions.spawn(prompt, session_name, options, cwd=self.repo_root)

        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, runtime, model, effort, "
                "parent_agent_id, run_id, phase_id, module_id, file_id, state, session_name, "
                f"bg_id, started_at) VALUES (?, ?, ?, 'session', ?, ?, ?, ?, ?, ?, ?, "
                f"'registered', ?, ?, {_NOW})",
                (
                    session_id,
                    child_name,
                    role,
                    brief["model"],
                    effort,
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

        spawned = self._agent_dict(session_id)
        return {
            key: value
            for key, value in spawned.items()
            if value is not None and key != "settings_json"
        }

    def session_name(self, run_id: int, child_name: str) -> str:
        started_at = self.conn.execute(
            "SELECT started_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()["started_at"]
        return session_name_for(self.repo_root, run_id, child_name, run_stamp(started_at))

    def _session_options(
        self, role: str, model: str | None, effort: str | None = None
    ) -> list[str]:
        # Imported here: serve pulls in urllib, and every hook subprocess imports this module.
        from . import serve

        return agentfiles.session_options(
            self.repo_root,
            role,
            model,
            serve.server_url(self.repo_root),
            effort=effort or self.settings.effort.get(role),
            prompt_cache_ttl=self.settings.prompt_cache_ttl.get(role),
        )

    def _resume_options(self, role: str, model: str | None, effort: str | None = None) -> list[str]:
        # A resume still goes ahead without them: the host's installed plugin supplies the
        # ledger server, and a wake-up matters more than the exact launch flags.
        try:
            return self._session_options(role, model, effort)
        except LedgerError:
            return []

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
        message = wake.timed(
            " ".join(w["pointer"] for w in owed) or _RESUME_POINTER,
            wake.signal_for(self.conn, target["agent_id"]),
        )
        bg_id = sessions.resume(
            target["agent_id"],
            message,
            cwd=self.repo_root,
            name=target["session_name"],
            options=self._resume_options(target["role"], target["model"], target["effort"]),
        )

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
        if owed:
            self.release_closed_driver(c.agent_id)

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
        if self.is_mod_session(wakeup["from_agent_id"]):
            return wake.mod_step(wakeup)
        return self.wake_step(wakeup)

    def mod_session(self, session_id: str) -> None:
        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mod_sessions (session_id) VALUES (?)", (session_id,)
            )

    def is_mod_session(self, session_id: str | None) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM mod_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def wake_step(self, wakeup: dict) -> str:
        return wake.route_wakeup(
            wakeup,
            live=self._is_live(wakeup["to_agent_id"]),
            signal=wake.signal_for(self.conn, wakeup["to_agent_id"]),
        ).next

    def _is_live(self, session_id: str) -> bool | None:
        try:
            return sessions.is_live(session_id)
        except LedgerError:
            return None

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
        if cur.rowcount:
            self.release_closed_driver(agent_id)
        return cur.rowcount

    def wakeups_paid(self, agent_id: str, wakeup_ids: list[int]) -> int:
        if not wakeup_ids:
            return 0
        placeholders = ",".join("?" for _ in wakeup_ids)
        with write_tx(self.conn) as conn:
            cur = conn.execute(
                f"UPDATE wakeups SET sent_at = {_NOW} WHERE from_agent_id = ? "
                f"AND sent_at IS NULL AND wakeup_id IN ({placeholders})",
                (agent_id, *wakeup_ids),
            )
        if cur.rowcount:
            self.release_closed_driver(agent_id)
        return cur.rowcount

    # -- File ownership -------------------------------------------------------

    def claim_file(
        self,
        caller: str,
        agent_id: str,
        path: str,
        test_path: str | None,
        for_name: str,
        depends_on: list[int] | None = None,
    ) -> dict:
        depends_on = depends_on or []
        if test_path is not None and test_path.strip().lower() in ("", "none", "null"):
            test_path = None
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            require_role(c, "lead")
            module = conn.execute(
                "SELECT m.name, p.ordinal FROM modules m JOIN phases p ON p.phase_id = m.phase_id "
                "WHERE m.module_id = ?",
                (c.module_id,),
            ).fetchone()
            if module is not None:
                require_coder_name(for_name, module["ordinal"], module["name"])
            outside = [
                dep_id
                for dep_id in depends_on
                if conn.execute(
                    "SELECT 1 FROM files WHERE file_id = ? AND module_id = ? "
                    "AND state != 'superseded'",
                    (dep_id, c.module_id),
                ).fetchone()
                is None
            ]
            if outside:
                raise LedgerError(
                    f"depends_on names file id(s) {outside} that are not claimed files of your "
                    "module; claim each helper first and pass the file_id claim_file returned"
                )
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
            conn.executemany(
                "INSERT OR IGNORE INTO file_deps (file_id, depends_on_file_id) VALUES (?, ?)",
                [(file_id, dep_id) for dep_id in depends_on],
            )

        claimed = dict(
            self.conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        )
        claimed["depends_on"] = list(depends_on)
        return claimed

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
        if len(body) > MESSAGE_BODY_MAX:
            raise LedgerError(
                f"the body has {len(body)} characters; a message takes at most "
                f"{MESSAGE_BODY_MAX}. Store the detail in its ledger record and point at it"
            )
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
            allowed = {row["name"] for row in self.message_peers(conn, c)}
            if to_name not in allowed:
                raise LedgerError(
                    f"{c.name!r} posts only to its parent, its children, and its siblings: "
                    f"{sorted(allowed)}. Reach {to_name!r} through that chain; a change to a "
                    "file goes through cr_open"
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

    def message_peers(self, conn: sqlite3.Connection, c: Caller) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND agent_id != ? AND (agent_id = ? "
            "OR parent_agent_id = ? OR (? IS NOT NULL AND parent_agent_id = ?))",
            (
                c.run_id,
                c.agent_id,
                c.parent_agent_id,
                c.agent_id,
                c.parent_agent_id,
                c.parent_agent_id,
            ),
        ).fetchall()

    @staticmethod
    def _inbox_batch(
        conn: sqlite3.Connection, run_id: int | None, name: str
    ) -> tuple[list[int], int]:
        rows = conn.execute(
            "SELECT message_id, LENGTH(body) AS size FROM messages "
            f"WHERE run_id = ? AND to_name = ? AND read_at IS NULL AND {_UNCLAIMED} "
            "ORDER BY message_id",
            (run_id, name),
        ).fetchall()
        ids: list[int] = []
        used = 0
        for row in rows:
            # The first message always goes out, so one that the ledger wrote over the
            # budget cannot block the inbox.
            if ids and used + row["size"] > INBOX_BODY_BUDGET:
                break
            ids.append(row["message_id"])
            used += row["size"]
        return ids, len(rows) - len(ids)

    @staticmethod
    def _messages(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return _rows(
            conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE message_id IN ({placeholders}) "
                "ORDER BY message_id",
                ids,
            )
        )

    def message_inbox(self, caller: str, agent_id: str) -> dict:
        with write_tx(self.conn) as conn:
            c = resolve(conn, caller, agent_id)
            ids, remaining = self._inbox_batch(conn, c.run_id, c.name)
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET read_at = {_NOW} WHERE message_id IN ({placeholders})",
                    ids,
                )
            messages = self._messages(conn, ids)

        return {"messages": messages, "remaining": remaining}

    def inbox_take(self, c: Caller) -> dict:
        with write_tx(self.conn) as conn:
            ids, remaining = self._inbox_batch(conn, c.run_id, c.name)
            if not ids:
                return {"claim": None, "messages": [], "remaining": remaining}
            claim = secrets.token_hex(8)
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE messages SET claim_id = ?, claimed_at = {_NOW} "
                f"WHERE message_id IN ({placeholders})",
                (claim, *ids),
            )
            messages = self._messages(conn, ids)
        for message in messages:
            del message["read_at"]
        return {"claim": claim, "messages": messages, "remaining": remaining}

    def inbox_settle(self, c: Caller, claim: str, *, read: bool) -> int:
        with write_tx(self.conn) as conn:
            change = f"read_at = {_NOW}" if read else "claim_id = NULL, claimed_at = NULL"
            cur = conn.execute(
                f"UPDATE messages SET {change} WHERE claim_id = ? AND run_id = ? "
                "AND to_name = ? AND read_at IS NULL",
                (claim, c.run_id, c.name),
            )
        return cur.rowcount

    # -- Directives -------------------------------------------------------------

    def directive_submit(
        self, source: str, sender_name: str | None, body: str, reply_to: int | None = None
    ) -> dict:
        source = _DIRECTIVE_SOURCE_ALIASES.get(source, source)
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
                # Resolves any open directive it answers, not only a needs_user one: the
                # outcome column is left as-is, so a needs_user question stays on the row.
                conn.execute(
                    f"UPDATE directives SET state = 'resolved', resolved_at = {_NOW} "
                    "WHERE directive_id = ? AND state = 'open'",
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
                    "resolved_at = NULL, question = ? WHERE directive_id = ?",
                    (outcome, resolution, resolution, directive_id),
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
            is_oracle_name = conn.execute(
                "SELECT 1 FROM agents WHERE run_id = ? AND role = 'oracle' AND name = ?",
                (c.run_id, target_agent_name),
            ).fetchone()
            if is_oracle_name is not None:
                raise LedgerError("the Oracle must not grant itself an override")
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

    def override_consume(self, run_id: int | None, rule: str, agent_name: str, target: str) -> bool:
        if run_id is None:
            return False
        with write_tx(self.conn) as conn:
            row = conn.execute(
                "SELECT override_id FROM overrides WHERE run_id = ? AND rule = ? "
                "AND target_agent_name = ? AND target = ? AND used_at IS NULL "
                "ORDER BY created_at ASC LIMIT 1",
                (run_id, rule, agent_name, target),
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

    def graph_gap(
        self,
        agent_id: str,
        tool: str,
        pattern: str | None,
        path: str | None,
        results: list[str],
    ) -> None:
        with write_tx(self.conn) as conn:
            conn.execute(
                "INSERT INTO graph_gaps (run_id, agent_id, tool, pattern, path, results_json) "
                "SELECT run_id, agent_id, ?, ?, ?, ? FROM agents "
                "WHERE agent_id = ? AND run_id IS NOT NULL",
                (tool, pattern, path, json.dumps(results[:20]), agent_id),
            )

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
