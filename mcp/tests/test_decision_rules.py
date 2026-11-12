"""Lead decisions act on a submitted handoff only, and phase_update follows a move table."""

from __future__ import annotations

import pytest
from helpers import (
    all_applicable,
    all_ratings,
    bootstrap_module,
    file_id_for,
    insert_passing_test_run,
    review_scores,
    spawn_coder,
)

from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

LEAD = "lead-p1-module-1"
MANAGER = "mgr-p1-phase-1"


def _submitted(ledger: Ledger, ctx: dict, slug: str, open_issues: list[str] | None = None) -> dict:
    name = f"coder-p1-module-1-{slug}"
    coder = spawn_coder(ledger, ctx, name, f"pkg/{slug}.py", f"tests/test_{slug}.py")
    file_id = file_id_for(ledger, f"pkg/{slug}.py")
    ledger.score_record(name, coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self")
    handoff = ledger.handoff_submit(name, coder["agent_id"], file_id, open_issues or [], [])
    ledger.score_record(
        LEAD, ctx["lead"]["agent_id"], file_id, all_ratings(10), all_applicable(), "lead"
    )
    ledger.review_compare(LEAD, ctx["lead"]["agent_id"], handoff["handoff_id"])
    return {
        "file_id": file_id,
        "handoff_id": handoff["handoff_id"],
        "lead": ctx["lead"]["agent_id"],
        "coder": coder["agent_id"],
        "coder_name": name,
    }


def _all_three_refuse(ledger: Ledger, h: dict, state: str, advice: str) -> None:
    lead, hid = h["lead"], h["handoff_id"]
    for call in (
        lambda: ledger.approve(LEAD, lead, hid),
        lambda: ledger.return_work(LEAD, lead, hid, ["x"], []),
        lambda: ledger.accept_incomplete(LEAD, lead, hid, "stops short"),
    ):
        with pytest.raises(LedgerError, match=f"is {state}") as raised:
            call()
        assert advice in str(raised.value)


def test_decisions_refuse_a_returned_handoff(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    h = _submitted(ledger, ctx, "ret")
    ledger.return_work(LEAD, h["lead"], h["handoff_id"], ["fix it"], [])

    _all_three_refuse(ledger, h, "returned", "wait for the Coder's new handoff")


def test_decisions_refuse_an_approved_handoff(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    h = _submitted(ledger, ctx, "apr")
    ledger.approve(LEAD, h["lead"], h["handoff_id"])

    _all_three_refuse(ledger, h, "approved", "final")


def test_decisions_refuse_an_incomplete_handoff(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    h = _submitted(ledger, ctx, "inc", open_issues=["blocked on an API"])
    ledger.accept_incomplete(LEAD, h["lead"], h["handoff_id"], "stops short")

    _all_three_refuse(ledger, h, "incomplete", "final")


def test_a_superseded_handoff_keeps_its_own_refusal(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    h = _submitted(ledger, ctx, "sup")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE handoffs SET state = 'superseded' WHERE handoff_id = ?", (h["handoff_id"],)
        )

    with pytest.raises(LedgerError, match="superseded"):
        ledger.approve(LEAD, h["lead"], h["handoff_id"])


def test_the_new_handoff_after_a_return_is_decided_and_attempt_record_still_runs(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    h = _submitted(ledger, ctx, "again")
    ledger.return_work(LEAD, h["lead"], h["handoff_id"], ["fix it"], [])
    ledger.score_record(
        h["coder_name"], h["coder"], h["file_id"], all_ratings(10), all_applicable(), "self"
    )
    ledger.brief_read(h["coder"], h["coder_name"])
    second = ledger.handoff_submit(h["coder_name"], h["coder"], h["file_id"], [], [])
    ledger.score_record(LEAD, h["lead"], h["file_id"], all_ratings(10), all_applicable(), "lead")
    ledger.review_compare(LEAD, h["lead"], second["handoff_id"])
    ledger.attempt_record(LEAD, h["lead"], h["file_id"])

    assert ledger.approve(LEAD, h["lead"], second["handoff_id"])


# -- phase_update's move table ---------------------------------------------------------


def _state(ledger: Ledger, phase_id: int) -> str:
    return ledger.conn.execute(
        "SELECT state FROM phases WHERE phase_id = ?", (phase_id,)
    ).fetchone()["state"]


def _set_state(ledger: Ledger, phase_id: int, state: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = ? WHERE phase_id = ?", (state, phase_id))


def _new_phase(ledger: Ledger, oracle_id: str, name: str = "later") -> int:
    return ledger.phase_add("oracle", oracle_id, name)["phase_id"]


def test_the_legal_phase_moves_run(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    phase = _new_phase(ledger, oracle)

    for state in ("unlocked", "working", "handed_up", "working", "handed_up"):
        assert ledger.phase_update("oracle", oracle, phase, state)["state"] == state
    other = _new_phase(ledger, oracle, "other")
    ledger.phase_update("oracle", oracle, other, "unlocked")
    assert ledger.phase_update("oracle", oracle, other, "planned")["state"] == "planned"


@pytest.mark.parametrize(
    ("current", "target", "legal"),
    [
        ("planned", "working", "unlocked"),
        ("planned", "approved", "unlocked"),
        ("planned", "planned", "unlocked"),
        ("unlocked", "handed_up", "working, planned"),
        ("unlocked", "approved", "working, planned"),
        ("unlocked", "unlocked", "working, planned"),
        ("working", "working", "handed_up"),
        ("working", "approved", "handed_up"),
        ("working", "unlocked", "handed_up"),
        ("working", "planned", "handed_up"),
        ("handed_up", "handed_up", "approved, working"),
        ("handed_up", "unlocked", "approved, working"),
        ("approved", "working", "none (final)"),
        ("approved", "planned", "none (final)"),
        ("approved", "approved", "none (final)"),
    ],
)
def test_an_illegal_phase_move_names_the_state_and_the_legal_moves(
    ledger: Ledger, current: str, target: str, legal: str
) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    phase = _new_phase(ledger, oracle)
    _set_state(ledger, phase, current)

    with pytest.raises(LedgerError) as raised:
        ledger.phase_update("oracle", oracle, phase, target)
    assert f"is {current}" in str(raised.value)
    assert legal in str(raised.value)
    assert _state(ledger, phase) == current


def test_planned_to_unlocked_still_checks_its_dependencies(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    first = _new_phase(ledger, oracle, "first")
    second = ledger.phase_add("oracle", oracle, "second", depends_on=[first])["phase_id"]

    with pytest.raises(LedgerError, match="not approved"):
        ledger.phase_update("oracle", oracle, second, "unlocked")


def test_a_phase_of_another_run_is_refused(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    with write_tx(ledger.conn) as conn:
        run_id = conn.execute("INSERT INTO runs (state) VALUES ('closed')").lastrowid
        phase_id = conn.execute(
            "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'old', 1, 'planned')",
            (run_id,),
        ).lastrowid
    assert phase_id is not None

    with pytest.raises(LedgerError, match="unknown phase_id"):
        ledger.phase_update("oracle", oracle, phase_id, "unlocked")
    assert _state(ledger, phase_id) == "planned"


def test_a_manager_still_sets_only_its_own_phase_to_working_or_handed_up(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    other = _new_phase(ledger, ctx["oracle_id"])
    ledger.phase_update("oracle", ctx["oracle_id"], other, "unlocked")

    with pytest.raises(LedgerError, match="only its own phase"):
        ledger.phase_update(MANAGER, ctx["manager"]["agent_id"], other, "working")
    _set_state(ledger, ctx["phase_id"], "unlocked")
    with pytest.raises(LedgerError, match="only its own phase"):
        ledger.phase_update(MANAGER, ctx["manager"]["agent_id"], ctx["phase_id"], "planned")


def test_re_locking_refuses_a_phase_with_a_brief(ledger: Ledger) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    phase = _new_phase(ledger, oracle, "phase-1")
    ledger.phase_update("oracle", oracle, phase, "unlocked")
    ledger.brief_create(
        "oracle", oracle, MANAGER, "manager", "opus", "Own phase-1.", phase_id=phase
    )

    with pytest.raises(LedgerError, match="already has a brief"):
        ledger.phase_update("oracle", oracle, phase, "planned")
    assert _state(ledger, phase) == "unlocked"


@pytest.mark.parametrize("trace", ["agent", "module"])
def test_re_locking_refuses_a_phase_with_an_agent_or_a_module(ledger: Ledger, trace: str) -> None:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle = started["oracle"]["agent_id"]
    run_id = started["run"]["run_id"]
    phase = _new_phase(ledger, oracle)
    ledger.phase_update("oracle", oracle, phase, "unlocked")
    with write_tx(ledger.conn) as conn:
        if trace == "agent":
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, run_id, phase_id, state, ended_at) "
                "VALUES ('gone', 'mgr-gone', 'manager', ?, ?, 'released', "
                "'2026-01-01T00:00:00Z')",
                (run_id, phase),
            )
        else:
            conn.execute(
                "INSERT INTO modules (phase_id, name, state) VALUES (?, 'm', 'working')", (phase,)
            )

    with pytest.raises(LedgerError, match="already has"):
        ledger.phase_update("oracle", oracle, phase, "planned")


def test_an_old_accepted_phase_review_does_not_satisfy_a_later_approval(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    manager, oracle = ctx["manager"]["agent_id"], ctx["oracle_id"]
    ledger.agent_release(MANAGER, manager, ctx["lead"]["agent_id"])
    insert_passing_test_run(ledger, ctx["run_id"], manager, "phase")
    ledger.module_review(
        MANAGER, manager, ctx["module_id"], "accepted", "good", scores=review_scores()
    )
    ledger.phase_update(MANAGER, manager, ctx["phase_id"], "handed_up")
    insert_passing_test_run(ledger, ctx["run_id"], oracle, "full")
    ledger.phase_review(
        "oracle", oracle, ctx["phase_id"], "accepted", "ship it", scores=review_scores()
    )

    ledger.phase_update("oracle", oracle, ctx["phase_id"], "working")
    ledger.phase_update(MANAGER, manager, ctx["phase_id"], "handed_up")
    with write_tx(ledger.conn) as conn:  # the first review is older than the second hand-up
        conn.execute("UPDATE reviews SET created_at = '2000-01-01T00:00:00.000Z'")

    with pytest.raises(LedgerError, match="phase_review"):
        ledger.phase_update("oracle", oracle, ctx["phase_id"], "approved")

    insert_passing_test_run(ledger, ctx["run_id"], oracle, "full")
    ledger.phase_review(
        "oracle", oracle, ctx["phase_id"], "accepted", "again", scores=review_scores()
    )
    assert ledger.phase_update("oracle", oracle, ctx["phase_id"], "approved")["state"] == "approved"


def test_the_first_module_add_sets_an_unlocked_phase_to_working(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)

    def state() -> str:
        row = ledger.conn.execute(
            "SELECT state FROM phases WHERE phase_id = ?", (ctx["phase_id"],)
        ).fetchone()
        return row["state"]

    assert state() == "working"
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'handed_up' WHERE phase_id = ?", (ctx["phase_id"],))
    ledger.module_add(MANAGER, ctx["manager"]["agent_id"], ctx["phase_id"], "module-2")
    assert state() == "handed_up"
