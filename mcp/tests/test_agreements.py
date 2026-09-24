from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()

    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", f"test_command: {_TEST_COMMAND}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")

    (root / "pkg" / "good.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_good.py").write_text(
        "from pkg.good import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir()
    graph_path = knowledge_dir / "code_graph.db"
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", graph_path)

    conn = sqlite3.connect(str(graph_path))
    try:
        conn.execute(
            "INSERT INTO node (id, kind, description) VALUES ('swarmtest_good', 'function', "
            "'test fixture for good')"
        )
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES "
            "('swarmtest_good', 0, 'pkg/good.py', 'good.py', 'add')"
        )
        conn.commit()
    finally:
        conn.close()

    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "manager-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("manager-1", "mgr-agent")

    module = ledger.module_add("manager-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "manager-1", "mgr-agent", "lead-1", "lead", "sonnet", "Own module-1.", module_id=module_id
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-1", "lead-agent")

    return {
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _spawn_coder(ledger: Ledger, ctx: dict, coder_name: str, path: str, test_path: str) -> dict:
    claimed = ledger.claim_file("lead-1", ctx["lead"]["agent_id"], path, test_path, coder_name)
    ledger.brief_create(
        "lead-1",
        ctx["lead"]["agent_id"],
        coder_name,
        "coder",
        "sonnet",
        "Implement it.",
        module_id=ctx["module_id"],
        file_id=claimed["file_id"],
    )
    agent_id = f"{coder_name}-agent"
    ledger.agent_register_start(agent_id, "coder", parent_agent_id=ctx["lead"]["agent_id"])
    return ledger.brief_ack(coder_name, agent_id)


def _all_ratings(value: int = 10) -> list[dict]:
    ratings = []
    for dim_key, _, criteria in DIMENSIONS:
        for criterion_key, _ in criteria:
            ratings.append({"dimension": dim_key, "criterion": criterion_key, "value": value})
    return ratings


def _all_applicable() -> dict[str, str | None]:
    return {dim_key: None for dim_key, _, _ in DIMENSIONS}


def _latest_file_id(ledger: Ledger, path: str) -> int:
    row = ledger.conn.execute(
        "SELECT file_id FROM files WHERE path = ? ORDER BY file_id DESC LIMIT 1", (path,)
    ).fetchone()
    return row["file_id"]


def _approve_good(ledger: Ledger, ctx: dict, coder_name: str = "coder-good") -> dict:
    """Claims, hands off, and approves pkg/good.py end to end. Returns the handoff row."""
    coder = _spawn_coder(ledger, ctx, coder_name, "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    approved = ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    return {"coder": coder, "file_id": file_id, "handoff": approved}


# -- cr_open ------------------------------------------------------------------------


def test_cr_open_refuses_a_path_with_no_files_row(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="no module plans"):
        ledger.cr_open(
            "manager-1", ctx["manager"]["agent_id"], "pkg/unplanned.py", "please add a helper"
        )


def test_cr_open_refuses_when_caller_already_owns_the_path(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    with pytest.raises(LedgerError, match="already owns"):
        ledger.cr_open("coder-good", coder["agent_id"], "pkg/good.py", "change my own file")


def test_cr_open_routes_to_the_live_owner(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open(
        "manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please add validation"
    )
    assert cr["state"] == "open"
    assert cr["to_agent_id"] == "coder-good-agent"
    assert cr["path"] == "pkg/good.py"


def test_cr_open_falls_back_to_the_lead_once_the_owner_has_ended(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_good(ledger, ctx)  # coder-good is approved and released
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "one more change")
    assert cr["to_agent_id"] == ctx["lead"]["agent_id"]


# -- cr_accept ----------------------------------------------------------------------


def test_cr_accept_refuses_a_non_recipient(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    with pytest.raises(LedgerError, match="not the recipient"):
        ledger.cr_accept("manager-1", ctx["manager"]["agent_id"], cr["cr_id"], True, "ok")


def test_cr_accept_refuses_when_not_open(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")
    with pytest.raises(LedgerError, match="not open"):
        ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "again")


def test_cr_accept_decline_needs_a_non_empty_reason(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    with pytest.raises(LedgerError, match="non-empty reason"):
        ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], False, "")


def test_cr_accept_decline_records_state_and_reason(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    declined = ledger.cr_accept(
        "coder-good", coder["agent_id"], cr["cr_id"], False, "out of scope for this file"
    )
    assert declined["state"] == "declined"
    assert declined["decision_reason"] == "out of scope for this file"
    assert declined["accepted_at"] is None


# -- cr_complete: evidence, not claims -----------------------------------------------


def test_cr_complete_refuses_without_a_fresh_passing_test_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")
    with pytest.raises(LedgerError, match="no passing test run"):
        ledger.cr_complete("coder-good", coder["agent_id"], cr["cr_id"], "done")


def test_cr_complete_succeeds_with_the_coders_own_test_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")
    result = ledger.tests_run("coder-good", coder["agent_id"], "file", "tests/test_good.py")
    completed = ledger.cr_complete("coder-good", coder["agent_id"], cr["cr_id"], "fixed it")
    assert completed["state"] == "completed"
    assert completed["evidence_test_run_id"] == result["test_run_id"]


def test_cr_complete_refuses_a_non_recipient(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")
    with pytest.raises(LedgerError, match="not the recipient"):
        ledger.cr_complete("manager-1", ctx["manager"]["agent_id"], cr["cr_id"], "done")


# -- cr_verify ------------------------------------------------------------------------


def _accept_and_complete(ledger: Ledger, ctx: dict, coder: dict, cr: dict) -> dict:
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")
    ledger.tests_run("coder-good", coder["agent_id"], "file", "tests/test_good.py")
    return ledger.cr_complete("coder-good", coder["agent_id"], cr["cr_id"], "fixed it")


def test_cr_verify_refuses_a_non_requester_and_non_ancestor(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)
    with pytest.raises(LedgerError, match="requester"):
        ledger.cr_verify("lead-1", ctx["lead"]["agent_id"], cr["cr_id"], True, "looks good")


def test_cr_verify_refuses_before_completed(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    with pytest.raises(LedgerError, match="not completed"):
        ledger.cr_verify("manager-1", ctx["manager"]["agent_id"], cr["cr_id"], True, "not yet")


def test_cr_verify_failure_reopens_accepted_and_owes_the_recipient(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = 'host-r1-coder-good' WHERE agent_id = ?",
            (coder["agent_id"],),
        )
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)
    result = ledger.cr_verify(
        "manager-1", ctx["manager"]["agent_id"], cr["cr_id"], False, "not fixed"
    )
    assert result["state"] == "accepted"
    assert result["verify_notes"] == "not fixed"
    owed = ledger.owed_wakeups(ctx["manager"]["agent_id"])
    assert any(w["reason"] == "cr_verify" for w in owed)


def test_cr_verify_success_marks_verified(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)
    result = ledger.cr_verify(
        "manager-1", ctx["manager"]["agent_id"], cr["cr_id"], True, "confirmed"
    )
    assert result["state"] == "verified"
    assert result["verified_at"] is not None


def test_cr_verify_by_the_nearest_live_ancestor_once_the_requester_has_ended(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    asker = _spawn_coder(ledger, ctx, "coder-asker", "pkg/asker.py", "tests/test_asker.py")
    cr = ledger.cr_open("coder-asker", asker["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)

    ledger.agent_release("lead-1", ctx["lead"]["agent_id"], asker["agent_id"])

    result = ledger.cr_verify("lead-1", ctx["lead"]["agent_id"], cr["cr_id"], True, "confirmed")
    assert result["state"] == "verified"


# -- cr_list --------------------------------------------------------------------------


def test_cr_list_scopes_to_sender_and_recipient_but_the_oracle_sees_all(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")

    coder_view = ledger.cr_list("coder-good", "coder-good-agent")
    assert len(coder_view) == 1
    manager_view = ledger.cr_list("manager-1", ctx["manager"]["agent_id"])
    assert len(manager_view) == 1
    lead_view = ledger.cr_list("lead-1", ctx["lead"]["agent_id"])
    assert lead_view == []
    oracle_view = ledger.cr_list("oracle", ctx["oracle_id"])
    assert len(oracle_view) == 1

    open_only = ledger.cr_list("oracle", ctx["oracle_id"], state="open")
    assert len(open_only) == 1
    none_left = ledger.cr_list("oracle", ctx["oracle_id"], state="declined")
    assert none_left == []


# -- gates: handoff_submit, approve, phase_update, run_finish -------------------------


def test_handoff_submit_refused_while_an_accepted_cr_is_not_completed(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.cr_accept("coder-good", coder["agent_id"], cr["cr_id"], True, "will do")

    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    with pytest.raises(LedgerError, match="call cr_complete first"):
        ledger.handoff_submit("coder-good", coder["agent_id"], file_id, [], [])


def test_handoff_submit_succeeds_once_the_cr_is_completed(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)

    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-good", coder["agent_id"], file_id, [], [])
    assert handoff["state"] == "submitted"


def test_approve_refused_while_a_cr_is_open(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-good", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please also fix y")

    with pytest.raises(LedgerError, match="not verified"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_refused_while_a_cr_is_completed_and_succeeds_once_verified(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)

    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-good", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    with pytest.raises(LedgerError, match="not verified"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    ledger.cr_verify("manager-1", ctx["manager"]["agent_id"], cr["cr_id"], True, "confirmed")
    approved = ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    assert approved["state"] == "approved"


def test_phase_update_approved_refused_while_a_cr_is_open(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_good(ledger, ctx)
    ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "one more change")

    with pytest.raises(LedgerError, match="not verified"):
        ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")


def test_run_finish_refused_while_a_cr_is_open(ledger: Ledger) -> None:
    # Phases are approved through SQL: the module and phase review gates would stop the
    # flow before run_finish, and this test isolates run_finish's own change-request check.
    ctx = _bootstrap(ledger)
    _approve_good(ledger, ctx)
    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'approved' WHERE phase_id = ?", (ctx["phase_id"],))
    ledger.agent_release("oracle", ctx["oracle_id"], ctx["manager"]["agent_id"])

    phase2 = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    ledger.phase_update("oracle", ctx["oracle_id"], phase2["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle",
        ctx["oracle_id"],
        "manager-2",
        "manager",
        "opus",
        "Own phase-2.",
        phase_id=phase2["phase_id"],
    )
    ledger.agent_register_start("mgr2-agent", "manager", parent_agent_id=ctx["oracle_id"])
    manager2 = ledger.brief_ack("manager-2", "mgr2-agent")

    cr = ledger.cr_open("manager-2", manager2["agent_id"], "pkg/good.py", "one more change")
    assert cr["to_agent_id"] == ctx["oracle_id"]

    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE phases SET state = 'approved' WHERE phase_id = ?", (phase2["phase_id"],)
        )

    with pytest.raises(LedgerError, match="not verified"):
        ledger.run_finish("oracle", ctx["oracle_id"], "success")


# -- departures --------------------------------------------------------------------------


def test_departure_record_by_a_coder_for_its_own_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    departure = ledger.departure_record(
        "coder-good", coder["agent_id"], "skipped the caching layer for now"
    )
    assert departure["state"] == "open"
    assert departure["kind"] == "departure"
    assert departure["file_id"] == file_id


def test_departure_record_refuses_a_coder_for_another_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    _spawn_coder(ledger, ctx, "coder-other", "pkg/other.py", "tests/test_other.py")
    other_file_id = _latest_file_id(ledger, "pkg/other.py")
    with pytest.raises(LedgerError, match="only for its own file"):
        ledger.departure_record(
            "coder-good", coder["agent_id"], "skipped it", file_id=other_file_id
        )


def test_handoff_submit_creates_linked_departure_rows(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(
        "coder-good", coder["agent_id"], file_id, [], ["skipped input validation for now"]
    )

    rows = ledger.conn.execute(
        "SELECT * FROM departures WHERE handoff_id = ?", (handoff["handoff_id"],)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["body"] == "skipped input validation for now"
    assert rows[0]["state"] == "open"
    assert rows[0]["file_id"] == file_id


def test_departure_decide_refuses_a_role_below_the_recorders_parent(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    other_coder = _spawn_coder(ledger, ctx, "coder-other", "pkg/other.py", "tests/test_other.py")
    departure = ledger.departure_record("coder-good", coder["agent_id"], "skipped it")
    with pytest.raises(LedgerError, match="may not decide"):
        ledger.departure_decide(
            "coder-other",
            other_coder["agent_id"],
            departure["departure_id"],
            "accepted",
            "not my call",
        )


def test_departure_decide_accepts_by_the_recorders_parent(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    departure = ledger.departure_record("coder-good", coder["agent_id"], "skipped it")
    decided = ledger.departure_decide(
        "lead-1", ctx["lead"]["agent_id"], departure["departure_id"], "accepted", "acceptable"
    )
    assert decided["state"] == "accepted"
    assert decided["decided_by"] == ctx["lead"]["agent_id"]


def test_departure_decide_denial_needs_a_solution(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    departure = ledger.departure_record("coder-good", coder["agent_id"], "skipped it")
    with pytest.raises(LedgerError, match="needs a solution"):
        ledger.departure_decide(
            "lead-1", ctx["lead"]["agent_id"], departure["departure_id"], "denied", "no"
        )


def test_approve_refused_by_an_open_departure_then_by_a_denied_one(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(
        "coder-good", coder["agent_id"], file_id, [], ["skipped the retry logic"]
    )
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    departure_row = ledger.conn.execute(
        "SELECT * FROM departures WHERE handoff_id = ?", (handoff["handoff_id"],)
    ).fetchone()

    with pytest.raises(LedgerError, match="decide it with departure_decide"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    ledger.departure_decide(
        "lead-1",
        ctx["lead"]["agent_id"],
        departure_row["departure_id"],
        "denied",
        "no",
        solution="add the retry logic",
    )
    with pytest.raises(LedgerError, match="call return_work"):
        ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_departure_decide_late_denial_of_an_accepted_departure_opens_a_deferral(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(
        "coder-good", coder["agent_id"], file_id, [], ["skipped the retry logic"]
    )
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    departure_row = ledger.conn.execute(
        "SELECT * FROM departures WHERE handoff_id = ?", (handoff["handoff_id"],)
    ).fetchone()
    ledger.departure_decide(
        "lead-1", ctx["lead"]["agent_id"], departure_row["departure_id"], "accepted", "fine for v1"
    )
    approved = ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    assert approved["state"] == "approved"

    with pytest.raises(LedgerError, match="can only be overridden"):
        ledger.departure_decide(
            "lead-1",
            ctx["lead"]["agent_id"],
            departure_row["departure_id"],
            "denied",
            "changed my mind",
            solution="add the retry logic after all",
        )

    denied = ledger.departure_decide(
        "manager-1",
        ctx["manager"]["agent_id"],
        departure_row["departure_id"],
        "denied",
        "found a better answer",
        solution="add the retry logic after all",
    )
    assert denied["state"] == "denied"

    deferral = ledger.conn.execute(
        "SELECT * FROM deferrals WHERE file_id = ? ORDER BY deferral_id DESC LIMIT 1", (file_id,)
    ).fetchone()
    assert deferral is not None
    assert deferral["state"] == "open"
    assert deferral["reason"] == "add the retry logic after all"


# -- shortfalls -----------------------------------------------------------------------


def test_shortfall_record_needs_no_decision(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    shortfall = ledger.shortfall_record(
        "coder-good", coder["agent_id"], "works, but a cleaner API is possible"
    )
    assert shortfall["kind"] == "shortfall"
    assert shortfall["state"] == "recorded"

    manager_shortfall = ledger.shortfall_record(
        "manager-1", ctx["manager"]["agent_id"], "phase finished but slower than hoped"
    )
    assert manager_shortfall["state"] == "recorded"


# -- report ---------------------------------------------------------------------------


def test_write_report_lists_departures_shortfalls_and_change_requests(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    file_id = _latest_file_id(ledger, "pkg/good.py")
    ledger.score_record(
        "coder-good", coder["agent_id"], file_id, _all_ratings(), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(
        "coder-good", coder["agent_id"], file_id, [], ["skipped the retry logic"]
    )
    ledger.score_record(
        "lead-1", ctx["lead"]["agent_id"], file_id, _all_ratings(), _all_applicable(), "lead"
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    departure_row = ledger.conn.execute(
        "SELECT * FROM departures WHERE handoff_id = ?", (handoff["handoff_id"],)
    ).fetchone()
    ledger.departure_decide(
        "lead-1", ctx["lead"]["agent_id"], departure_row["departure_id"], "accepted", "fine for v1"
    )
    ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.shortfall_record("lead-1", ctx["lead"]["agent_id"], "module works, could be tidier")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "one more change")

    run_id = ledger.conn.execute("SELECT run_id FROM runs LIMIT 1").fetchone()["run_id"]
    report = ledger.write_report(run_id)
    text = report["text"]

    assert "## Departures" in text
    assert "skipped the retry logic" in text
    assert "accepted by lead-1" in text
    assert "## Shortfalls" in text
    assert "module works, could be tidier" in text
    assert "## Change requests" in text
    assert f"CR #{cr['cr_id']}" in text


# -- wake lines -------------------------------------------------------------------------


def test_wake_lines_name_an_idle_recipient_of_an_open_cr(ledger: Ledger) -> None:
    from swarm_ledger.hooks.events import _wake_lines

    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = 'host-r1-coder-good' WHERE agent_id = ?",
            (coder["agent_id"],),
        )
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    ledger.agent_idle(coder["agent_id"], "stop")

    run_id = ledger.conn.execute("SELECT run_id FROM runs LIMIT 1").fetchone()["run_id"]
    live = [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL AND role IN "
            "('manager', 'lead', 'coder')",
            (run_id,),
        )
    ]
    lines = _wake_lines(ledger, run_id, live)
    assert any(f"change request {cr['cr_id']}" in line and "coder-good" in line for line in lines)


def test_wake_lines_name_an_idle_verifier_of_a_completed_cr(ledger: Ledger) -> None:
    from swarm_ledger.hooks.events import _wake_lines

    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    cr = ledger.cr_open("manager-1", ctx["manager"]["agent_id"], "pkg/good.py", "please fix it")
    _accept_and_complete(ledger, ctx, coder, cr)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = 'host-r1-mgr-1' WHERE agent_id = ?",
            (ctx["manager"]["agent_id"],),
        )
    ledger.agent_idle(ctx["manager"]["agent_id"], "stop")

    run_id = ledger.conn.execute("SELECT run_id FROM runs LIMIT 1").fetchone()["run_id"]
    live = [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL AND role IN "
            "('manager', 'lead', 'coder')",
            (run_id,),
        )
    ]
    lines = _wake_lines(ledger, run_id, live)
    assert any(
        f"change request {cr['cr_id']}" in line and "manager-1" in line and "verify" in line
        for line in lines
    )
