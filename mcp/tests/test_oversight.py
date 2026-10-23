from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from helpers import (
    all_applicable,
    all_ratings,
    as_session,
    bootstrap_module,
    file_id_for,
    go_idle,
    insert_passing_test_run,
    make_host,
    register_agent,
    settings_text,
    spawn_coder,
    write_module,
)

from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"
_GRAPH_MODULES = {"good": "add", "disagree": "sub", "low": "mul"}


pytestmark = pytest.mark.usefixtures("claude_sessions")


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = make_host(tmp_path / "host", settings_text(test_command=_TEST_COMMAND))
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests").mkdir()

    for name, symbol in _GRAPH_MODULES.items():
        write_module(root, name, symbol)

    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir()
    graph_path = knowledge_dir / "code_graph.db"
    shutil.copyfile(repo_root / "knowledge" / "code_graph.db", graph_path)

    conn = sqlite3.connect(str(graph_path))
    try:
        for name, symbol in _GRAPH_MODULES.items():
            node_id = f"oversighttest_{name}"
            conn.execute(
                "INSERT INTO node (id, kind, description) VALUES (?, 'function', ?)",
                (node_id, f"test fixture for {name}"),
            )
            conn.execute(
                "INSERT INTO anchor (node_id, ord, path, base, symbol) VALUES (?, 0, ?, ?, ?)",
                (node_id, f"pkg/{name}.py", f"{name}.py", symbol),
            )
        conn.commit()
    finally:
        conn.close()

    return root


def _review_scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10, "reason": None},
        {"dimension": "integration", "value": 10, "reason": None},
        {"dimension": "open_items", "value": 10, "reason": None},
    ]


def _spawn_coder(ledger: Ledger, ctx: dict, slug: str, path: str, test_path: str | None) -> dict:
    return spawn_coder(ledger, ctx, f"coder-p1-module-1-{slug}", path, test_path)


def _approve_file(
    ledger: Ledger,
    ctx: dict,
    slug: str,
    path: str,
    test_path: str | None,
    lead_overrides: dict[str, int] | None = None,
) -> int:
    coder = _spawn_coder(ledger, ctx, slug, path, test_path)
    coder_name = coder["name"]
    file_id = file_id_for(ledger, path)
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10, overrides=lead_overrides),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    return file_id


def _release_lead(ledger: Ledger, ctx: dict) -> None:
    ledger.agent_release("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])


def _hand_up_phase(ledger: Ledger, ctx: dict) -> dict:
    return ledger.phase_update(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up"
    )


# -- module_review --------------------------------------------------------------------------


def test_module_review_refuses_a_caller_that_is_not_the_module_s_manager(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    other = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    ledger.phase_update("oracle", ctx["oracle_id"], other["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle",
        ctx["oracle_id"],
        "mgr-p2-phase-2",
        "manager",
        "opus",
        "Own p2-phase-2.",
        phase_id=other["phase_id"],
    )
    register_agent(ledger, "mgr-2-agent", "manager", parent_agent_id=ctx["oracle_id"])
    manager_2 = ledger.brief_ack("mgr-p2-phase-2", "mgr-2-agent")

    with pytest.raises(LedgerError, match="not the Manager"):
        ledger.module_review(
            "mgr-p2-phase-2", manager_2["agent_id"], ctx["module_id"], "accepted", "ok"
        )


def test_module_review_refuses_a_file_that_is_not_approved_or_incomplete(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "pkg/good.py",
        "tests/test_good.py",
        "coder-p1-module-1-c1",
    )
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match="not approved or incomplete"):
        ledger.module_review(
            "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )


def test_module_review_refuses_without_a_passing_test_run(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _approve_file(ledger, ctx, "good", "pkg/good.py", "tests/test_good.py")

    with pytest.raises(LedgerError, match="no passing test run"):
        ledger.module_review(
            "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )


def test_module_review_accepted_succeeds_with_an_agreeing_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _approve_file(ledger, ctx, "good", "pkg/good.py", "tests/test_good.py")
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    result = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=_review_scores(),
    )
    assert result["outcome"] == "accepted"
    assert result["kind"] == "manager"


def test_a_released_claim_that_was_claimed_again_does_not_block_module_review(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    lead = ctx["lead"]["agent_id"]
    first = ledger.claim_file(
        "lead-p1-module-1", lead, "pkg/good.py", "tests/test_good.py", "coder-p1-module-1-old"
    )
    ledger.release_file("lead-p1-module-1", lead, "pkg/good.py")
    _approve_file(ledger, ctx, "new", "pkg/good.py", "tests/test_good.py")
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    state = ledger.conn.execute(
        "SELECT state FROM files WHERE file_id = ?", (first["file_id"],)
    ).fetchone()["state"]
    assert state == "superseded"
    result = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=_review_scores(),
    )
    assert result["outcome"] == "accepted"


def test_module_review_refuses_missing_disagreement_notes(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    file_id = _approve_file(
        ledger,
        ctx,
        "disagree",
        "pkg/disagree.py",
        "tests/test_disagree.py",
        lead_overrides={"testing": 9},
    )
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match=f"disagreement_notes needs a non-empty note.*{file_id}"):
        ledger.module_review(
            "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )

    accepted = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        disagreement_notes={str(file_id): "self and lead disagreed on testing; lead's stands"},
        scores=_review_scores(),
    )
    assert accepted["outcome"] == "accepted"


def test_module_review_returned_owes_the_live_lead_a_wake_up(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    as_session(ledger, ctx["lead"]["agent_id"], "host-r1-lead-p1-module-1")
    result = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "returned",
        "needs more work",
    )
    assert result["outcome"] == "returned"
    assert result["lead_ended"] is False
    assert result["next"] is not None

    module_row = ledger.conn.execute(
        "SELECT state FROM modules WHERE module_id = ?", (ctx["module_id"],)
    ).fetchone()
    assert module_row["state"] == "returned"


def test_module_review_returned_reports_when_the_lead_has_ended(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _release_lead(ledger, ctx)

    result = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "returned",
        "needs more work",
    )
    assert result["lead_ended"] is True
    assert result["next"] is None


# -- the phase_update(handed_up) gate --------------------------------------------------------


def test_phase_update_handed_up_refuses_without_an_accepted_module_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _release_lead(ledger, ctx)

    with pytest.raises(LedgerError, match="module_review"):
        ledger.phase_update(
            "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up"
        )


def test_phase_update_handed_up_succeeds_after_an_accepted_module_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _release_lead(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )

    phase = _hand_up_phase(ledger, ctx)
    assert phase["state"] == "handed_up"


# -- phase_review ----------------------------------------------------------------------------


def _accept_module_and_hand_up(ledger: Ledger, ctx: dict) -> None:
    _release_lead(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    _hand_up_phase(ledger, ctx)


def test_phase_review_refuses_when_the_phase_is_not_handed_up(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    with pytest.raises(LedgerError, match="not handed_up"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_phase_review_refuses_without_a_passing_full_test_run(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)

    with pytest.raises(LedgerError, match="scope 'full'"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_phase_review_accepted_succeeds(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )
    assert result["outcome"] == "accepted"
    assert result["kind"] == "oracle"


def test_phase_review_refuses_code_the_graph_maps_without_a_test_file(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _approve_file(ledger, ctx, "good", "pkg/good.py", None)
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    with pytest.raises(LedgerError, match=r"\(pkg/good.py\) contain functions .* no test_path"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_re_claiming_an_approved_untested_file_with_a_test_clears_the_gate(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    first = _approve_file(ledger, ctx, "good", "pkg/good.py", None)
    second = _approve_file(ledger, ctx, "good-r2", "pkg/good.py", "tests/test_good.py")
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    states = dict(ledger.conn.execute("SELECT file_id, state FROM files").fetchall())
    assert states[first] == "superseded"
    assert states[second] == "approved"
    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok", scores=_review_scores()
    )
    assert result["outcome"] == "accepted"


def test_a_file_without_code_in_the_graph_needs_no_test_file(ledger: Ledger, host: Path) -> None:
    (host / "pkg" / "notes.txt").write_text("notes\n", encoding="utf-8")
    conn = sqlite3.connect(str(host / "knowledge" / "code_graph.db"))
    try:
        conn.execute("INSERT INTO node (id, kind, description) VALUES ('notes', 'doc', 'Notes.')")
        conn.execute(
            "INSERT INTO anchor (node_id, ord, path, base, symbol) "
            "VALUES ('notes', 0, 'pkg/notes.txt', 'notes.txt', NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    ctx = bootstrap_module(ledger)
    _approve_file(ledger, ctx, "notes", "pkg/notes.txt", None)
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok", scores=_review_scores()
    )
    assert result["outcome"] == "accepted"


def test_phase_review_refuses_missing_low_score_notes(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    coder = _spawn_coder(ledger, ctx, "low", "pkg/low.py", "tests/test_low.py")
    file_id = file_id_for(ledger, "pkg/low.py")
    ledger.score_record(
        coder["name"], coder["agent_id"], file_id, all_ratings(10), all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder["name"], coder["agent_id"], file_id, ["blocked"], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        all_ratings(10, overrides={"testing": 5}),
        all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.accept_incomplete(
        "lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "blocked"
    )

    _release_lead(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    _hand_up_phase(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    with pytest.raises(LedgerError, match=f"low_score_notes needs a non-empty note.*{file_id}"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")

    accepted = ledger.phase_review(
        "oracle",
        ctx["oracle_id"],
        ctx["phase_id"],
        "accepted",
        "ok",
        low_score_notes={str(file_id): "recorded shortfall, acceptable for this run"},
        scores=_review_scores(),
    )
    assert accepted["outcome"] == "accepted"


def test_a_lead_recorded_departure_gates_module_review_and_phase_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    manager_id = ctx["manager"]["agent_id"]
    departure = ledger.departure_record(
        "lead-p1-module-1", ctx["lead"]["agent_id"], "skipped the lint rule for speed"
    )
    departure_id = departure["departure_id"]
    insert_passing_test_run(ledger, ctx["run_id"], manager_id, "phase")

    with pytest.raises(
        LedgerError, match=rf"\[{departure_id}\] in the module.*waits on the Manager"
    ):
        ledger.module_review("mgr-p1-phase-1", manager_id, ctx["module_id"], "accepted", "ok")

    ledger.departure_decide(
        "mgr-p1-phase-1", manager_id, departure_id, "agree", "fine for this run"
    )
    _release_lead(ledger, ctx)
    ledger.module_review(
        "mgr-p1-phase-1", manager_id, ctx["module_id"], "accepted", "ok", scores=_review_scores()
    )
    _hand_up_phase(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    with pytest.raises(LedgerError, match=rf"\[{departure_id}\] in the phase are not signed off"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")

    ledger.departure_decide("oracle", ctx["oracle_id"], departure_id, "agree", "acceptable")
    accepted = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok", scores=_review_scores()
    )
    assert accepted["outcome"] == "accepted"
    assert json.loads(accepted["details_json"]) == {
        "low_score_notes": {},
        "review_scores": _review_scores(),
    }


def test_phase_review_returned_owes_the_live_manager_a_wake_up(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    as_session(ledger, ctx["manager"]["agent_id"], "host-r1-mgr-p1-phase-1")

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "returned", "not ready"
    )
    assert result["outcome"] == "returned"
    assert result["manager_ended"] is False
    assert result["next"] is not None

    phase_row = ledger.conn.execute(
        "SELECT state FROM phases WHERE phase_id = ?", (ctx["phase_id"],)
    ).fetchone()
    assert phase_row["state"] == "working"


def test_phase_review_returned_reports_when_the_manager_has_ended(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    ledger.agent_release("oracle", ctx["oracle_id"], ctx["manager"]["agent_id"])

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "returned", "not ready"
    )
    assert result["manager_ended"] is True
    assert result["next"] is None


# -- the phase_update(approved) gate ---------------------------------------------------------


def test_phase_update_approved_refuses_without_an_accepted_phase_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)

    with pytest.raises(LedgerError, match="phase_review"):
        ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")


def test_phase_update_approved_succeeds_after_an_accepted_phase_review(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )

    phase = ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    assert phase["state"] == "approved"


# -- write_report -----------------------------------------------------------------------------


def test_write_report_lists_manager_and_oracle_reviews_and_the_repo_check(ledger: Ledger) -> None:
    ctx = bootstrap_module(ledger)
    ledger.repo_check("oracle", ctx["oracle_id"])
    _accept_module_and_hand_up(ledger, ctx)
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )

    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "## Repo" in text
    assert "Last repo_check: on " in text
    assert '"obvious_start"' not in text
    assert "## Manager and Oracle reviews" in text
    assert "[manager]" in text
    assert "[oracle]" in text


# -- the Stop hook's pending-review reason -----------------------------------------------------


def test_stop_blocks_the_oracle_while_a_handed_up_phase_has_no_oracle_review(
    ledger: Ledger,
) -> None:
    ctx = bootstrap_module(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    go_idle(ledger, ctx["manager"]["agent_id"])

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"
    assert "awaiting an Oracle review" in result["reason"]
