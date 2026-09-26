from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"
_GRAPH_MODULES = {"good": "add", "disagree": "sub", "low": "mul"}


def _write_module(host: Path, name: str, symbol: str) -> None:
    (host / "pkg" / f"{name}.py").write_text(
        f"def {symbol}(a, b):\n    return a + b\n", encoding="utf-8", newline="\n"
    )
    (host / "tests" / f"test_{name}.py").write_text(
        f"from pkg.{name} import {symbol}\n\n\n"
        f"def test_{symbol}():\n    assert {symbol}(1, 2) == 3\n",
        encoding="utf-8",
        newline="\n",
    )


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

    for name, symbol in _GRAPH_MODULES.items():
        _write_module(root, name, symbol)

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


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _all_ratings(
    value: int = 10, reason: str = "needs work", overrides: dict[str, int] | None = None
) -> list[dict]:
    overrides = overrides or {}
    ratings = []
    for dim_key, _, criteria in DIMENSIONS:
        dim_value = overrides.get(dim_key, value)
        for criterion_key, _ in criteria:
            ratings.append(
                {
                    "dimension": dim_key,
                    "criterion": criterion_key,
                    "value": dim_value,
                    "reason": reason if dim_value < 9 else None,
                    "ref": "pkg/good.py:1" if dim_value < 9 else None,
                }
            )
    return ratings


def _all_applicable() -> dict[str, str | None]:
    return {dim_key: None for dim_key, _, _ in DIMENSIONS}


def _review_scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10, "reason": None},
        {"dimension": "integration", "value": 10, "reason": None},
        {"dimension": "open_items", "value": 10, "reason": None},
    ]


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
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _spawn_coder(
    ledger: Ledger, ctx: dict, coder_name: str, path: str, test_path: str | None
) -> dict:
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


def _file_id_for(ledger: Ledger, path: str) -> int:
    return ledger.who_owns(path)["file"]["file_id"]


def _insert_passing_test_run(ledger: Ledger, run_id: int, agent_id: str, scope: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, ?, 'x', 'x', 0, 1, 0, 0, '')",
            (run_id, agent_id, scope),
        )


def _approve_file(
    ledger: Ledger,
    ctx: dict,
    coder_name: str,
    path: str,
    test_path: str | None,
    lead_overrides: dict[str, int] | None = None,
) -> int:
    coder = _spawn_coder(ledger, ctx, coder_name, path, test_path)
    file_id = _file_id_for(ledger, path)
    ledger.score_record(
        coder_name, coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit(coder_name, coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(10, overrides=lead_overrides),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.approve("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    return file_id


def _go_idle(ledger: Ledger, agent_id: str) -> None:
    events.handle_stop(ledger, {"session_id": agent_id, "stop_hook_active": True})


def _as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def _release_lead(ledger: Ledger, ctx: dict) -> None:
    ledger.agent_release("manager-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])


def _hand_up_phase(ledger: Ledger, ctx: dict) -> dict:
    return ledger.phase_update(
        "manager-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up"
    )


# -- module_review --------------------------------------------------------------------------


def test_module_review_refuses_a_caller_that_is_not_the_module_s_manager(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    other = ledger.phase_add("oracle", ctx["oracle_id"], "phase-2")
    ledger.brief_create(
        "oracle",
        ctx["oracle_id"],
        "manager-2",
        "manager",
        "opus",
        "Own phase-2.",
        phase_id=other["phase_id"],
    )
    ledger.agent_register_start("mgr-2-agent", "manager", parent_agent_id=ctx["oracle_id"])
    manager_2 = ledger.brief_ack("manager-2", "mgr-2-agent")

    with pytest.raises(LedgerError, match="not the Manager"):
        ledger.module_review("manager-2", manager_2["agent_id"], ctx["module_id"], "accepted", "ok")


def test_module_review_refuses_a_file_that_is_not_approved_or_incomplete(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "pkg/good.py", "tests/test_good.py", "c1")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match="not approved or incomplete"):
        ledger.module_review(
            "manager-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )


def test_module_review_refuses_without_a_passing_test_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_file(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")

    with pytest.raises(LedgerError, match="no passing test run"):
        ledger.module_review(
            "manager-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )


def test_module_review_accepted_succeeds_with_an_agreeing_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_file(ledger, ctx, "coder-good", "pkg/good.py", "tests/test_good.py")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    result = ledger.module_review(
        "manager-1",
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
    ctx = _bootstrap(ledger)
    lead = ctx["lead"]["agent_id"]
    first = ledger.claim_file("lead-1", lead, "pkg/good.py", "tests/test_good.py", "coder-old")
    ledger.release_file("lead-1", lead, "pkg/good.py")
    _approve_file(ledger, ctx, "coder-new", "pkg/good.py", "tests/test_good.py")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    state = ledger.conn.execute(
        "SELECT state FROM files WHERE file_id = ?", (first["file_id"],)
    ).fetchone()["state"]
    assert state == "superseded"
    result = ledger.module_review(
        "manager-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=_review_scores(),
    )
    assert result["outcome"] == "accepted"


def test_module_review_refuses_missing_disagreement_notes(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    file_id = _approve_file(
        ledger,
        ctx,
        "coder-disagree",
        "pkg/disagree.py",
        "tests/test_disagree.py",
        lead_overrides={"testing": 9},
    )
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match=f"disagreement_notes needs a non-empty note.*{file_id}"):
        ledger.module_review(
            "manager-1", ctx["manager"]["agent_id"], ctx["module_id"], "accepted", "ok"
        )

    accepted = ledger.module_review(
        "manager-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        disagreement_notes={str(file_id): "self and lead disagreed on testing; lead's stands"},
        scores=_review_scores(),
    )
    assert accepted["outcome"] == "accepted"


def test_module_review_returned_owes_the_live_lead_a_wake_up(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _as_session(ledger, ctx["lead"]["agent_id"], "host-r1-lead-1")
    result = ledger.module_review(
        "manager-1", ctx["manager"]["agent_id"], ctx["module_id"], "returned", "needs more work"
    )
    assert result["outcome"] == "returned"
    assert result["lead_ended"] is False
    assert result["next"] is not None

    module_row = ledger.conn.execute(
        "SELECT state FROM modules WHERE module_id = ?", (ctx["module_id"],)
    ).fetchone()
    assert module_row["state"] == "returned"


def test_module_review_returned_reports_when_the_lead_has_ended(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _release_lead(ledger, ctx)

    result = ledger.module_review(
        "manager-1", ctx["manager"]["agent_id"], ctx["module_id"], "returned", "needs more work"
    )
    assert result["lead_ended"] is True
    assert result["next"] is None


# -- the phase_update(handed_up) gate --------------------------------------------------------


def test_phase_update_handed_up_refuses_without_an_accepted_module_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _release_lead(ledger, ctx)

    with pytest.raises(LedgerError, match="module_review"):
        ledger.phase_update("manager-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")


def test_phase_update_handed_up_succeeds_after_an_accepted_module_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _release_lead(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "manager-1",
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
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "manager-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    _hand_up_phase(ledger, ctx)


def test_phase_review_refuses_when_the_phase_is_not_handed_up(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="not handed_up"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_phase_review_refuses_without_a_passing_full_test_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)

    with pytest.raises(LedgerError, match="scope 'full'"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_phase_review_accepted_succeeds(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )
    assert result["outcome"] == "accepted"
    assert result["kind"] == "oracle"


def test_phase_review_refuses_code_the_graph_maps_without_a_test_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_file(ledger, ctx, "coder-good", "pkg/good.py", None)
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    with pytest.raises(LedgerError, match=r"\(pkg/good.py\) contain functions .* no test_path"):
        ledger.phase_review("oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok")


def test_re_claiming_an_approved_untested_file_with_a_test_clears_the_gate(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    first = _approve_file(ledger, ctx, "coder-good", "pkg/good.py", None)
    second = _approve_file(ledger, ctx, "coder-good-r2", "pkg/good.py", "tests/test_good.py")
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

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
    ctx = _bootstrap(ledger)
    _approve_file(ledger, ctx, "coder-notes", "pkg/notes.txt", None)
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok", scores=_review_scores()
    )
    assert result["outcome"] == "accepted"


def test_phase_review_refuses_missing_low_score_notes(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-low", "pkg/low.py", "tests/test_low.py")
    file_id = _file_id_for(ledger, "pkg/low.py")
    ledger.score_record(
        "coder-low", coder["agent_id"], file_id, _all_ratings(10), _all_applicable(), "self"
    )
    handoff = ledger.handoff_submit("coder-low", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(10, overrides={"testing": 5}),
        _all_applicable(),
        "lead",
    )
    ledger.accept_incomplete("lead-1", ctx["lead"]["agent_id"], handoff["handoff_id"], "blocked")

    _release_lead(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "manager-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    _hand_up_phase(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

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
    ctx = _bootstrap(ledger)
    manager_id = ctx["manager"]["agent_id"]
    departure = ledger.departure_record(
        "lead-1", ctx["lead"]["agent_id"], "skipped the lint rule for speed"
    )
    departure_id = departure["departure_id"]
    _insert_passing_test_run(ledger, ctx["run_id"], manager_id, "phase")

    with pytest.raises(
        LedgerError, match=rf"\[{departure_id}\] in the module.*waits on the Manager"
    ):
        ledger.module_review("manager-1", manager_id, ctx["module_id"], "accepted", "ok")

    ledger.departure_decide("manager-1", manager_id, departure_id, "agree", "fine for this run")
    _release_lead(ledger, ctx)
    ledger.module_review(
        "manager-1", manager_id, ctx["module_id"], "accepted", "ok", scores=_review_scores()
    )
    _hand_up_phase(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

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
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    _as_session(ledger, ctx["manager"]["agent_id"], "host-r1-manager-1")

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
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    ledger.agent_release("oracle", ctx["oracle_id"], ctx["manager"]["agent_id"])

    result = ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "returned", "not ready"
    )
    assert result["manager_ended"] is True
    assert result["next"] is None


# -- the phase_update(approved) gate ---------------------------------------------------------


def test_phase_update_approved_refuses_without_an_accepted_phase_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)

    with pytest.raises(LedgerError, match="phase_review"):
        ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")


def test_phase_update_approved_succeeds_after_an_accepted_phase_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )

    phase = ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    assert phase["state"] == "approved"


# -- write_report -----------------------------------------------------------------------------


def test_write_report_lists_manager_and_oracle_reviews_and_the_repo_check(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.repo_check("oracle", ctx["oracle_id"])
    _accept_module_and_hand_up(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
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
    ctx = _bootstrap(ledger)
    _accept_module_and_hand_up(ledger, ctx)
    _go_idle(ledger, ctx["manager"]["agent_id"])

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"
    assert "awaiting an Oracle review" in result["reason"]
