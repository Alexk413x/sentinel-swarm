"""Regression tests for the ledger-core review findings."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_claims_and_closing import free
from test_rekey import MovingClaude, claude
from test_sessions import (
    _bootstrap,
    _brief_coder,
    _brief_second_lead,
    host,
)

from swarm_ledger import lock
from swarm_ledger.agreements import test_run_passed as run_passed
from swarm_ledger.db import connect
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

__all__ = ["claude", "free", "host"]


def test_release_file_refuses_a_lead_of_another_module(
    ledger: Ledger, claude: MovingClaude, free: dict
) -> None:
    ctx = _bootstrap(ledger, claude)
    _brief_coder(ledger, ctx, "mine")
    second = _brief_second_lead(ledger, ctx)
    lead2 = ledger.agent_spawn(*ctx.manager, second)
    ledger.brief_ack(second, lead2["agent_id"])

    with pytest.raises(LedgerError, match="not the Lead of the module"):
        ledger.release_file(second, lead2["agent_id"], "src/mine.py")
    row = ledger.conn.execute("SELECT released_at FROM files WHERE path = 'src/mine.py'").fetchone()
    assert row["released_at"] is None
    assert ledger.release_file(*ctx.lead, "src/mine.py")["path"] == "src/mine.py"


def test_a_failed_run_start_does_not_leave_the_repo_locked(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude.add("sess-oracle", "my-host-oracle")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("write failed")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_log_event", boom)
        with pytest.raises(RuntimeError, match="write failed"):
            ledger.run_start(prd="Build X", session_id="sess-oracle")
    assert lock.read_lock(ledger.repo_root) is None

    started = ledger.run_start(prd="Build X", session_id="sess-oracle")
    assert started["resumed"] is False


def test_a_report_that_fails_to_write_does_not_leave_the_repo_locked(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)

    def boom(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("no space")

    monkeypatch.setattr(ledger, "write_report", boom)
    with pytest.raises(RuntimeError, match="no space"):
        ledger.run_close(*ctx.oracle, "stopped by hand")
    assert lock.read_lock(ledger.repo_root) is None


def test_phase_add_refuses_a_dependency_outside_the_run(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.conn.execute("INSERT INTO runs (state) VALUES ('closed')")
    other = ledger.conn.execute("SELECT MAX(run_id) AS n FROM runs").fetchone()["n"]
    ledger.conn.execute(
        "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'p1-old', 1, 'done')",
        (other,),
    )
    foreign = ledger.conn.execute("SELECT MAX(phase_id) AS n FROM phases").fetchone()["n"]

    with pytest.raises(LedgerError, match="unknown phase_id"):
        ledger.phase_add(*ctx.oracle, "phase-2", depends_on=[foreign])
    with pytest.raises(LedgerError, match="unknown phase_id"):
        ledger.phase_add(*ctx.oracle, "phase-2", depends_on=[99999])
    ok = ledger.phase_add(*ctx.oracle, "phase-2", depends_on=[ctx.phase_id, ctx.phase_id])
    assert ok["depends_on"] == [ctx.phase_id]


def test_run_pause_with_an_empty_phase_list_does_not_pause_the_whole_run(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="names no phase"):
        ledger.run_pause(*ctx.oracle, "blocked", phases=[])
    state = ledger.conn.execute("SELECT state FROM runs").fetchone()["state"]
    assert state == "active"


def test_an_unacked_brief_of_an_earlier_run_does_not_block_brief_create(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.conn.execute("INSERT INTO runs (state) VALUES ('closed')")
    old_run = ledger.conn.execute("SELECT MAX(run_id) AS n FROM runs").fetchone()["n"]
    ledger.conn.execute(
        "INSERT INTO briefs (run_id, parent_agent_id, child_name, child_role, body) "
        "VALUES (?, ?, 'lead-p1-module-2', 'lead', 'old')",
        (old_run, ctx.oracle[1]),
    )

    brief = _brief_second_lead(ledger, ctx)

    assert brief == "lead-p1-module-2"
    with pytest.raises(LedgerError, match="unacked brief already exists"):
        _brief_second_lead(ledger, ctx)


def test_hot_lookups_have_indexes_and_an_existing_ledger_gains_them(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    conn = connect(db_path)
    conn.execute("DROP INDEX idx_handoffs_file")
    conn.close()
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        names = {r["name"] for r in rows}
        assert {
            "idx_files_module",
            "idx_handoffs_file",
            "idx_reviews_file_kind",
            "idx_scores_review",
            "idx_issues_file_state",
            "idx_briefs_child",
            "idx_agents_run",
            "idx_agent_events_agent",
            "idx_wakeups_to_agent",
        } <= names
    finally:
        conn.close()


def test_a_test_run_with_collection_errors_does_not_count_as_passed() -> None:
    green = {
        "state": "done",
        "exit_code": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "allowed_skipped": 0,
        "passed": 3,
    }
    assert run_passed(green) is True  # type: ignore[arg-type]
    assert run_passed({**green, "errors": 2}) is False  # type: ignore[arg-type]
