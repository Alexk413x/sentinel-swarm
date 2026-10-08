from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.db import connect, write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.rubric import DIMENSIONS

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_TEST_COMMAND = f"{_PYTHON} -m pytest -q -p no:cacheprovider {{target}}"

_GRAPH_MODULES: dict[str, str] = {
    "modfile": "mod_fn",
    "floorfile": "floor_fn",
    "floorfile2": "floor_fn2",
    "blindfile": "blind_fn",
    "roundfile": "round_fn",
}


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


def _touch_module(host: Path, name: str, marker: str) -> None:
    path = host / "pkg" / f"{name}.py"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n# {marker}\n", encoding="utf-8", newline="\n"
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
    # rounds=2, attempts_per_round=1: the full escalation budget is 2 attempts, small
    # enough for a floor-pass test to reach, and rounds=2 leaves room to watch an issue
    # actually advance from round 1 to round 2.
    text = (
        template.replace("test_command:\n", f"test_command: {_TEST_COMMAND}\n")
        .replace("  rounds: 3\n", "  rounds: 2\n")
        .replace("  attempts_per_round: 3\n", "  attempts_per_round: 1\n")
    )
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
            node_id = f"batchatest_{name}"
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


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "mgr-p1-phase-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-1", "mgr-agent")

    module = ledger.module_add("mgr-p1-phase-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "mgr-p1-phase-1",
        "mgr-agent",
        "lead-p1-module-1",
        "lead",
        "sonnet",
        "Own module-1.",
        module_id=module_id,
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-p1-module-1", "lead-agent")

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
    claimed = ledger.claim_file(
        "lead-p1-module-1", ctx["lead"]["agent_id"], path, test_path, coder_name
    )
    ledger.brief_create(
        "lead-p1-module-1",
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


def _as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def _all_ratings(
    value: int = 10, reason: str = "needs work", overrides: dict[str, list[int]] | None = None
) -> list[dict]:
    """Every criterion rated `value`, except dimensions in `overrides`, keyed by dimension to
    a list of per-criterion values in declaration order."""
    overrides = overrides or {}
    ratings = []
    for dim_key, _, criteria in DIMENSIONS:
        dim_overrides = overrides.get(dim_key)
        for index, (criterion_key, _) in enumerate(criteria):
            dim_value = dim_overrides[index] if dim_overrides is not None else value
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
        {"dimension": "completeness", "value": 10},
        {"dimension": "integration", "value": 10},
        {"dimension": "open_items", "value": 10},
    ]


# -- schema migration ---------------------------------------------------------------------


def _untimed(text: str) -> str:
    assert re.search(r" elapsed \d+s(?: / \d+s)?", text), text
    return re.sub(r" elapsed \d+s(?: / \d+s)?", "", text)


def test_connect_adds_the_floor_pass_column_to_an_older_handoffs_table(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL);"
        "INSERT INTO schema_version (id, version) VALUES (1, 1);"
        "CREATE TABLE files (file_id INTEGER PRIMARY KEY, path TEXT, released_at TEXT);"
        "CREATE TABLE handoffs (handoff_id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, "
        "state TEXT NOT NULL);"
        "INSERT INTO files (file_id, path) VALUES (1, 'pkg/good.py');"
        "INSERT INTO handoffs (handoff_id, file_id, state) VALUES (1, 1, 'approved');"
    )
    old.close()

    conn = connect(db_path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(handoffs)")}
        assert "floor_pass_json" in columns
        row = conn.execute("SELECT * FROM handoffs WHERE handoff_id = 1").fetchone()
        assert (row["state"], row["floor_pass_json"]) == ("approved", None)
    finally:
        conn.close()

    connect(db_path).close()


# -- item 1: floor pass -----------------------------------------------------------------------


def test_approve_refuses_a_failing_review_before_the_file_reaches_its_last_round(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-floor", "pkg/floorfile.py", "tests/test_floorfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/floorfile.py")
    ledger.score_record(
        "coder-p1-module-1-floor",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-floor", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])

    with pytest.raises(LedgerError, match="does not pass"):
        ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_improved_attempts_do_not_count_toward_the_last_round(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-floor", "pkg/floorfile.py", "tests/test_floorfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/floorfile.py")
    ledger.score_record(
        "coder-p1-module-1-floor",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-floor", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    for _ in range(2):
        ledger.conn.execute(
            "INSERT INTO attempts (file_id, outcome, round) VALUES (?, 'improved', 1)", (file_id,)
        )

    with pytest.raises(LedgerError, match="does not pass"):
        ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])


def test_approve_accepts_a_floor_pass_at_the_last_round_and_records_a_shortfall(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-floor", "pkg/floorfile.py", "tests/test_floorfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/floorfile.py")
    ledger.score_record(
        "coder-p1-module-1-floor",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff1 = ledger.handoff_submit("coder-p1-module-1-floor", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff1["handoff_id"])
    # rounds=2, attempts_per_round=1 in this fixture: the full escalation budget is 2
    # attempts, so two return_work calls exhaust it and put the file at its last round.
    ledger.return_work(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff1["handoff_id"],
        ["polish it"],
        ["performance"],
    )

    _touch_module(ledger.repo_root, "floorfile", "revised once")
    ledger.score_record(
        "coder-p1-module-1-floor",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-floor")
    handoff2 = ledger.handoff_submit("coder-p1-module-1-floor", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff2["handoff_id"])
    ledger.return_work(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff2["handoff_id"],
        ["still needs polish"],
        ["performance"],
    )

    _touch_module(ledger.repo_root, "floorfile", "revised twice")
    ledger.score_record(
        "coder-p1-module-1-floor",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-floor")
    handoff3 = ledger.handoff_submit("coder-p1-module-1-floor", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff3["handoff_id"])
    ledger.attempt_record("lead-p1-module-1", ctx["lead"]["agent_id"], file_id)

    approved = ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff3["handoff_id"])
    assert approved["state"] == "approved"
    assert approved["floor_pass_dimensions"] == ["performance"]
    assert json.loads(approved["floor_pass_json"]) == ["performance"]

    shortfalls = ledger.conn.execute(
        "SELECT * FROM departures WHERE file_id = ? AND kind = 'shortfall'", (file_id,)
    ).fetchall()
    assert len(shortfalls) == 1
    assert "performance" in shortfalls[0]["body"]

    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "pkg/floorfile.py -- approved" in text
    assert "floor pass: performance below target but at or above the floor" in text


def test_approve_refuses_a_floor_pass_when_a_criterion_is_below_the_criterion_floor(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-floor2", "pkg/floorfile2.py", "tests/test_floorfile2.py"
    )
    file_id = _file_id_for(ledger, "pkg/floorfile2.py")
    ledger.score_record(
        "coder-p1-module-1-floor2",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff1 = ledger.handoff_submit("coder-p1-module-1-floor2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff1["handoff_id"])
    ledger.return_work(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff1["handoff_id"],
        ["polish it"],
        ["performance"],
    )

    _touch_module(ledger.repo_root, "floorfile2", "revised once")
    ledger.score_record(
        "coder-p1-module-1-floor2",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-floor2")
    handoff2 = ledger.handoff_submit("coder-p1-module-1-floor2", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [7, 7, 7]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff2["handoff_id"])
    # This second return_work exhausts the fixture's 2-attempt escalation budget, so the
    # file is at its last round for the third handoff below.
    ledger.return_work(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff2["handoff_id"],
        ["still needs polish"],
        ["performance"],
    )

    _touch_module(ledger.repo_root, "floorfile2", "revised twice")
    ledger.score_record(
        "coder-p1-module-1-floor2",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-floor2")
    handoff3 = ledger.handoff_submit("coder-p1-module-1-floor2", coder["agent_id"], file_id, [], [])
    # One criterion at 4 keeps the dimension average (80) at or above the floor, but the
    # criterion itself is below criterion_floor (5), so a floor pass must not apply.
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(overrides={"performance": [4, 10, 10]}),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff3["handoff_id"])

    with pytest.raises(LedgerError, match="does not pass"):
        ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff3["handoff_id"])


# -- item 2: scored module and phase reviews -----------------------------------------------


def _approve_modfile(ledger: Ledger, ctx: dict) -> int:
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-mod", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    ledger.score_record(
        "coder-p1-module-1-mod",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff = ledger.handoff_submit("coder-p1-module-1-mod", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "lead",
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    ledger.approve("lead-p1-module-1", ctx["lead"]["agent_id"], handoff["handoff_id"])
    return file_id


def test_module_review_accepted_requires_scores_for_all_three_dimensions(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_modfile(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match=r"missing \['integration', 'open_items'\]"):
        ledger.module_review(
            "mgr-p1-phase-1",
            ctx["manager"]["agent_id"],
            ctx["module_id"],
            "accepted",
            "ok",
            scores=[{"dimension": "completeness", "value": 10}],
        )

    accepted = ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    assert accepted["outcome"] == "accepted"
    details = json.loads(accepted["details_json"])
    assert details["review_scores"] == [
        {"dimension": "completeness", "value": 10, "reason": None},
        {"dimension": "integration", "value": 10, "reason": None},
        {"dimension": "open_items", "value": 10, "reason": None},
    ]


def test_module_review_accepted_refuses_a_score_below_9_with_no_reason(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_modfile(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")

    with pytest.raises(LedgerError, match="completeness: a rating below 9 needs a reason"):
        ledger.module_review(
            "mgr-p1-phase-1",
            ctx["manager"]["agent_id"],
            ctx["module_id"],
            "accepted",
            "ok",
            scores=[
                {"dimension": "completeness", "value": 7},
                {"dimension": "integration", "value": 10},
                {"dimension": "open_items", "value": 10},
            ],
        )


def test_module_review_returned_needs_no_scores(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    result = ledger.module_review(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["module_id"], "returned", "not ready"
    )
    assert result["outcome"] == "returned"


def test_phase_review_accepted_requires_scores_and_the_report_shows_them(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _approve_modfile(ledger, ctx)
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "ok",
        scores=_review_scores(),
    )
    ledger.agent_release("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    ledger.phase_update("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")

    with pytest.raises(LedgerError, match=r"missing \['integration', 'open_items'\]"):
        ledger.phase_review(
            "oracle",
            ctx["oracle_id"],
            ctx["phase_id"],
            "accepted",
            "ok",
            scores=[{"dimension": "completeness", "value": 10}],
        )

    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ok", scores=_review_scores()
    )

    text = ledger.report_build("oracle", ctx["oracle_id"])["text"]
    assert "[manager] module" in text
    assert "[oracle] phase" in text
    assert "Scores: completeness=10, integration=10, open_items=10" in text


# -- item 3: a rating below 9 needs a ref --------------------------------------------------


def test_score_record_refuses_a_rating_below_9_with_no_ref(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-ref", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    ratings = _all_ratings(10)
    ratings[0] = {
        "dimension": "meets_the_brief",
        "criterion": "does_what_was_asked",
        "value": 7,
        "reason": "needs work",
        "ref": None,
    }
    with pytest.raises(LedgerError, match="a rating below 9 needs a ref"):
        ledger.score_record(
            "coder-p1-module-1-ref", coder["agent_id"], file_id, ratings, _all_applicable(), "self"
        )


def test_score_record_accepts_a_rating_below_9_with_a_reason_and_a_ref(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-ref2", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    ratings = _all_ratings(10)
    ratings[0] = {
        "dimension": "meets_the_brief",
        "criterion": "does_what_was_asked",
        "value": 7,
        "reason": "needs work",
        "ref": "pkg/modfile.py:1",
    }
    result = ledger.score_record(
        "coder-p1-module-1-ref2", coder["agent_id"], file_id, ratings, _all_applicable(), "self"
    )
    assert result["kind"] == "self"


# -- item 4: blind scoring hides self-review issues until the Lead scores -------------------


def test_issue_list_hides_self_review_issues_from_the_lead_until_it_scores(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    lead_id = ctx["lead"]["agent_id"]
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-blind", "pkg/blindfile.py", "tests/test_blindfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/blindfile.py")
    low = _all_ratings(10, overrides={"performance": [4, 10, 10]})
    self_review = ledger.score_record(
        "coder-p1-module-1-blind", coder["agent_id"], file_id, low, _all_applicable(), "self"
    )
    assert self_review["issues"]

    lead_view = ledger.issue_list("lead-p1-module-1", lead_id, file_id=file_id)
    assert lead_view == []

    coder_view = ledger.issue_list("coder-p1-module-1-blind", coder["agent_id"], file_id=file_id)
    assert len(coder_view) == 1

    ledger.handoff_submit("coder-p1-module-1-blind", coder["agent_id"], file_id, [], [])
    # The Lead rates the same criterion low too, so the issue the self review opened
    # stays open under the same issue_id instead of closing or duplicating.
    ledger.score_record("lead-p1-module-1", lead_id, file_id, low, _all_applicable(), "lead")

    lead_view_after = ledger.issue_list("lead-p1-module-1", lead_id, file_id=file_id)
    assert len(lead_view_after) == 1
    assert lead_view_after[0]["issue_id"] == self_review["issues"][0]
    assert lead_view_after[0]["state"] == "open"


# -- item 5: an issue a Manager or Oracle opens starts at a later round ---------------------


def test_issue_open_starts_at_round_1_for_a_lead(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-r1", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    del coder
    issue = ledger.issue_open(
        "lead-p1-module-1", ctx["lead"]["agent_id"], file_id, "Stray", "found by the Lead"
    )
    assert issue["round"] == 1


def test_issue_open_starts_at_round_2_for_a_manager(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-r2", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    del coder
    issue = ledger.issue_open(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], file_id, "Stray", "found by the Manager"
    )
    assert issue["round"] == 2


def test_issue_open_starts_at_round_3_for_the_oracle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-r3", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    del coder
    issue = ledger.issue_open("oracle", ctx["oracle_id"], file_id, "Stray", "found by the Oracle")
    assert issue["round"] == 3


# -- item 6: a round advance notifies the next level -----------------------------------------


def test_attempt_record_escalates_a_plateaued_issue_and_owes_the_manager_a_wake_up(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger)
    manager_id = ctx["manager"]["agent_id"]
    _as_session(ledger, manager_id, "host-r1-manager-1")
    manager_session = {
        "pid": 9,
        "sessionId": manager_id,
        "name": "host-r1-manager-1",
        "status": "idle",
    }

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        return json.dumps([manager_session]) if args[:2] == ["agents", "--json"] else ""

    monkeypatch.setattr(sessions, "_run", fake_run)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-round", "pkg/roundfile.py", "tests/test_roundfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/roundfile.py")

    ledger.score_record(
        "coder-p1-module-1-round",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    handoff1 = ledger.handoff_submit("coder-p1-module-1-round", coder["agent_id"], file_id, [], [])
    low = _all_ratings(10, overrides={"performance": [4, 10, 10]})
    scored = ledger.score_record(
        "lead-p1-module-1", ctx["lead"]["agent_id"], file_id, low, _all_applicable(), "lead"
    )
    issue_id = scored["issues"][0]
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff1["handoff_id"])
    ledger.return_work(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        handoff1["handoff_id"],
        ["fix performance"],
        ["performance"],
    )

    _touch_module(ledger.repo_root, "roundfile", "revised")
    ledger.score_record(
        "coder-p1-module-1-round",
        coder["agent_id"],
        file_id,
        _all_ratings(10),
        _all_applicable(),
        "self",
    )
    ledger.brief_read(coder["agent_id"], "coder-p1-module-1-round")
    handoff2 = ledger.handoff_submit("coder-p1-module-1-round", coder["agent_id"], file_id, [], [])
    ledger.score_record(
        "lead-p1-module-1", ctx["lead"]["agent_id"], file_id, low, _all_applicable(), "lead"
    )
    ledger.review_compare("lead-p1-module-1", ctx["lead"]["agent_id"], handoff2["handoff_id"])

    result = ledger.attempt_record("lead-p1-module-1", ctx["lead"]["agent_id"], file_id)
    assert result["outcome"] == "plateau"
    (escalated,) = result["escalated"]
    assert [{**escalated, "next": _untimed(escalated["next"])}] == [
        {
            "issue_id": issue_id,
            "round": 2,
            "escalated_to": "mgr-p1-phase-1",
            "next": 'SendMessage(to="host-r1-manager-1", '
            f'message="Issue {issue_id} for pkg/roundfile.py moved to round 2; '
            'read it with issue_list.")',
        }
    ]

    issue_row = ledger.conn.execute(
        "SELECT round, escalated_to FROM issues WHERE issue_id = ?", (issue_id,)
    ).fetchone()
    assert (issue_row["round"], issue_row["escalated_to"]) == (2, "mgr-p1-phase-1")

    messages = ledger.message_inbox("mgr-p1-phase-1", manager_id)["messages"]
    assert any(f"Issue {issue_id}" in m["body"] for m in messages)


def test_issue_escalate_owes_a_wake_up_too(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _bootstrap(ledger)
    manager_id = ctx["manager"]["agent_id"]
    _as_session(ledger, manager_id, "host-r1-manager-1")
    manager_session = {
        "pid": 9,
        "sessionId": manager_id,
        "name": "host-r1-manager-1",
        "status": "idle",
    }

    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        return json.dumps([manager_session]) if args[:2] == ["agents", "--json"] else ""

    monkeypatch.setattr(sessions, "_run", fake_run)
    coder = _spawn_coder(
        ledger, ctx, "coder-p1-module-1-esc", "pkg/modfile.py", "tests/test_modfile.py"
    )
    file_id = _file_id_for(ledger, "pkg/modfile.py")
    del coder
    issue = ledger.issue_open(
        "lead-p1-module-1", ctx["lead"]["agent_id"], file_id, "Stray", "found by the Lead"
    )

    escalated = ledger.issue_escalate(
        "lead-p1-module-1", ctx["lead"]["agent_id"], issue["issue_id"]
    )
    assert escalated["escalated_to"] == "mgr-p1-phase-1"
    assert _untimed(escalated["next"]) == (
        'SendMessage(to="host-r1-manager-1", '
        f'message="Issue {issue["issue_id"]} is escalated to you for round 2. '
        'Read it with issue_list.")'
    )
