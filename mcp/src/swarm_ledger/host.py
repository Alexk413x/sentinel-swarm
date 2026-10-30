from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

from .identity import Caller
from .settings import Settings


class LedgerHost:
    """The attributes and methods every Ledger mixin calls across mixin lines.

    Ledger.__init__ sets the attributes, and Ledger or another mixin implements each
    method. This base sits last in Ledger's MRO, so a stub here never shadows them; it
    exists so pyright checks each mixin file as part of Ledger.
    """

    conn: sqlite3.Connection
    settings: Settings
    repo_root: Path
    server_pid: int
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

    def _release_agent(
        self, conn: sqlite3.Connection, target_agent_id: str, reason: str
    ) -> None: ...

    def _owe_wakeup(
        self,
        conn: sqlite3.Connection,
        c: Caller,
        to_agent_id: str | None,
        reason: str,
        pointer: str,
    ) -> dict | None: ...

    def next_step(self, wakeup: dict | None) -> str | None: ...

    def _wake_file_waiters(
        self, conn: sqlite3.Connection, c: Caller, path: str, state: str
    ) -> list[dict]: ...

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
    ) -> dict: ...

    def agent_spawn(self, caller: str, agent_id: str, child_name: str) -> dict: ...

    def _issue_chain(self, conn: sqlite3.Connection, issue: sqlite3.Row) -> list[sqlite3.Row]: ...

    def _scores_for(self, review_id: int) -> dict[str, float]: ...

    def _block_handoff_for_open_cr(self, conn: sqlite3.Connection, file_id: int) -> None: ...

    def _reassign_crs_of(self, conn: sqlite3.Connection, agent: sqlite3.Row) -> None: ...

    def _block_approve_for_cr(self, conn: sqlite3.Connection, file_id: int) -> None: ...

    def _block_approve_for_departures(self, conn: sqlite3.Connection, handoff_id: int) -> None: ...

    def _block_return_for_open_departures(
        self, conn: sqlite3.Connection, handoff_id: int
    ) -> None: ...

    def _mark_departures_reworked(
        self, conn: sqlite3.Connection, file_id: int, handoff_id: int
    ) -> None: ...

    def _block_module_review_for_departures(
        self, conn: sqlite3.Connection, module_id: int
    ) -> None: ...

    def _block_phase_review_for_departures(
        self, conn: sqlite3.Connection, phase_id: int
    ) -> None: ...

    def _start_child(self, brief: sqlite3.Row, parent_id: str, session_name: str) -> dict: ...

    def _require_spawnable(self, brief: sqlite3.Row, child_name: str) -> None: ...

    def _require_spawn_slot(self, brief: sqlite3.Row, listing: list[dict]) -> None: ...

    def session_name(self, run_id: int, child_name: str) -> str: ...

    def resume_agent(self, target: sqlite3.Row, reason: str) -> dict: ...

    def adopt_resumed(self, old_id: str, new_id: str) -> str: ...
