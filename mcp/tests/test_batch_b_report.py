from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from swarm_ledger import sessions
from swarm_ledger.db import connect, write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def fake_repo(tmp_path: Path, repo_root: Path) -> Path:
    host = tmp_path / "host"
    (host / ".git").mkdir(parents=True)
    claude_dir = host / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    return host


@pytest.fixture
def ledger(fake_repo: Path) -> Ledger:
    return Ledger(fake_repo, db_path=fake_repo / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle",
        oracle_id,
        "mgr-p1-phase-1",
        "manager",
        "opus",
        "Own p1-phase-1.",
        phase_id=phase_id,
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


def _insert_test_run(
    ledger: Ledger,
    run_id: int,
    agent_id: str,
    scope: str,
    *,
    passed: int = 1,
    failed: int = 0,
    skipped: int = 0,
    exit_code: int = 0,
) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, ?, 'x', 'x', ?, ?, ?, ?, '')",
            (run_id, agent_id, scope, exit_code, passed, failed, skipped),
        )


def _insert_agent(
    ledger: Ledger,
    agent_id: str,
    name: str,
    role: str,
    *,
    run_id: int,
    phase_id: int | None = None,
    model: str | None = None,
    cost_usd: float | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> None:
    with write_tx(ledger.conn) as conn:
        columns = ["agent_id", "name", "role", "run_id", "phase_id", "model", "state", "cost_usd"]
        values: list[object] = [agent_id, name, role, run_id, phase_id, model, "idle", cost_usd]
        if started_at is not None:
            columns.append("started_at")
            values.append(started_at)
        if ended_at is not None:
            columns.append("ended_at")
            values.append(ended_at)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(f"INSERT INTO agents ({', '.join(columns)}) VALUES ({placeholders})", values)


# -- item 7: reply_to resolves every open directive, not only needs_user -----------


def test_directive_reply_to_resolves_an_open_directive_with_no_outcome_yet(ledger: Ledger) -> None:
    _bootstrap(ledger)
    asked = ledger.directive_submit("user_chat", "alex", "Rename the CLI flag?")
    assert (asked["state"], asked["outcome"]) == ("open", None)

    reply = ledger.directive_submit(
        "user_chat", "alex", "Yes, rename it.", reply_to=asked["directive_id"]
    )

    parent = ledger.conn.execute(
        "SELECT * FROM directives WHERE directive_id = ?", (asked["directive_id"],)
    ).fetchone()
    assert parent["state"] == "resolved"
    assert parent["outcome"] is None
    assert parent["resolved_at"] is not None
    assert reply["reply_to"] == asked["directive_id"]


def test_directive_reply_to_leaves_a_resolved_directive_alone(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    asked = ledger.directive_submit("user_chat", "alex", "Add a CLI?")
    ledger.directive_resolve("oracle", ctx["oracle_id"], asked["directive_id"], "declined", "No.")

    # A reply naming an already-resolved directive touches nothing on that row; state
    # = 'open' is required, so the UPDATE is a no-op and the reply is just recorded.
    ledger.directive_submit("user_chat", "alex", "Are you sure?", reply_to=asked["directive_id"])
    parent = ledger.conn.execute(
        "SELECT * FROM directives WHERE directive_id = ?", (asked["directive_id"],)
    ).fetchone()
    assert (parent["state"], parent["outcome"]) == ("resolved", "declined")


# -- item 23: snake_case directive sources ------------------------------------------


def test_directive_submit_normalizes_old_source_spellings(ledger: Ledger) -> None:
    _bootstrap(ledger)
    old_chat = ledger.directive_submit("user-chat", "alex", "Old spelling.")
    old_outside = ledger.directive_submit("outside-session", "ci", "Also old.")
    assert old_chat["source"] == "user_chat"
    assert old_outside["source"] == "outside_session"


def test_directive_submit_accepts_the_new_snake_case_sources(ledger: Ledger) -> None:
    _bootstrap(ledger)
    submitted = ledger.directive_submit("outside_session", "ci", "New spelling.")
    assert submitted["source"] == "outside_session"


def test_migrate_normalizes_stored_directive_source_spellings(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    old = sqlite3.connect(str(db_path))
    old.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL);"
        "INSERT INTO schema_version (id, version) VALUES (1, 1);"
        "CREATE TABLE directives (directive_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, "
        "source TEXT NOT NULL, sender_name TEXT, body TEXT NOT NULL, reply_to INTEGER, "
        "state TEXT NOT NULL DEFAULT 'open', outcome TEXT, resolved_at TEXT, resolution TEXT, "
        "created_at TEXT);"
        "INSERT INTO directives (directive_id, run_id, source, body, state) VALUES "
        "(1, 1, 'user-chat', 'Old row.', 'open'), (2, 1, 'outside-session', 'Also old.', "
        "'resolved');"
    )
    old.close()

    conn = connect(db_path)
    try:
        rows = {r["directive_id"]: r["source"] for r in conn.execute("SELECT * FROM directives")}
        assert rows == {1: "user_chat", 2: "outside_session"}
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(directives)")}
        assert "question" in columns
    finally:
        conn.close()


# -- item 10: the Oracle never grants itself an override ----------------------------


def test_override_grant_refuses_a_target_that_names_the_oracle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    with pytest.raises(LedgerError, match="must not grant itself"):
        ledger.override_grant(
            "oracle", ctx["oracle_id"], "write", "oracle", "src/a.py", "self-serve"
        )


def test_override_grant_still_allows_a_non_oracle_target(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    granted = ledger.override_grant(
        "oracle", ctx["oracle_id"], "write", "coder-1", "src/a.py", "urgent fix"
    )
    assert granted["target_agent_name"] == "coder-1"


# -- item 15: a report per run, report.md as the latest copy ------------------------


def test_write_report_keeps_a_report_per_run_and_report_md_as_the_latest(ledger: Ledger) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, prd, state, outcome) VALUES (1, 'PRD A', 'finished', "
            "'success')"
        )
        conn.execute(
            "INSERT INTO runs (run_id, prd, state, outcome) VALUES (2, 'PRD B', 'finished', "
            "'partial')"
        )

    first = ledger.write_report(1)
    second = ledger.write_report(2)

    records_dir = Path(first["path"]).parent
    assert (records_dir / "report-1.md").read_text(encoding="utf-8") == first["text"]
    assert (records_dir / "report-2.md").read_text(encoding="utf-8") == second["text"]
    assert "Outcome: success" in first["text"]
    assert "Outcome: partial" in second["text"]

    latest = (records_dir / "report.md").read_text(encoding="utf-8")
    assert latest == second["text"]
    assert "Outcome: partial" in latest


# -- item 8: decided deferrals, notifications, and the final test run --------------


def test_report_lists_decided_deferrals_with_proposer_decider_and_reason(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    claimed = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/a.py",
        "tests/test_a.py",
        "coder-p1-module-1-a",
    )
    _insert_agent(
        ledger,
        "coder-agent-1",
        "coder-p1-module-1-a",
        "coder",
        run_id=ctx["run_id"],
        phase_id=ctx["phase_id"],
    )
    decided = ledger.deferral_propose(
        "coder-p1-module-1-a",
        "coder-agent-1",
        "Skip caching for now.",
        file_id=claimed["file_id"],
        kind="file",
    )
    ledger.agreement_decide(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        decided["deferral_id"],
        "agreed",
        "Fine for v1.",
    )
    still_open = ledger.deferral_propose(
        "coder-p1-module-1-a",
        "coder-agent-1",
        "Rename the module later.",
        file_id=claimed["file_id"],
        kind="file",
    )

    report = ledger.write_report(ctx["run_id"])["text"]
    assert "## Decided deferrals" in report
    assert (
        f"- Deferral #{decided['deferral_id']} on src/a.py: Skip caching for now. -- "
        "proposed by coder-p1-module-1-a, decided by lead-p1-module-1 (agreed): Fine for v1."
    ) in report

    # The still-open deferral stays out of the decided section, and shows in Open items.
    decided_section = report.split("## Decided deferrals", 1)[1].split("## Departures", 1)[0]
    assert f"Deferral #{still_open['deferral_id']}" not in decided_section
    open_section = report.split("## Open items", 1)[1].split("## Decided deferrals", 1)[0]
    assert f"Deferral #{still_open['deferral_id']}: Rename the module later." in open_section


def test_report_lists_notifications_and_their_replies(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    asked = ledger.directive_submit("user_chat", "alex", "Use Postgres or SQLite?")
    ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "needs_user", "Which database?"
    )
    ledger.directive_submit("outside_session", "alex", "SQLite.", reply_to=asked["directive_id"])
    # A later resolve to a final outcome must not erase the question already asked.
    ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "applied", "Switched to SQLite."
    )

    report = ledger.write_report(ctx["run_id"])["text"]
    assert "## Notifications to the user" in report
    assert f"- Directive #{asked['directive_id']} (user_chat): Which database?" in report
    assert "  - Reply from alex: SQLite." in report


def test_report_notifications_section_is_empty_when_no_directive_asked_the_user(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    ledger.directive_submit("skill", "setup", "Detected pytest as the runner.")
    report = ledger.write_report(ctx["run_id"])["text"]
    notifications = report.split("## Notifications to the user", 1)[1].split("## Final test run")[0]
    assert "Directive #" not in notifications


def test_report_final_test_run_section_shows_the_last_full_scope_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _insert_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full", passed=10, skipped=1)
    _insert_test_run(
        ledger, ctx["run_id"], ctx["oracle_id"], "full", passed=12, failed=1, exit_code=1
    )
    report = ledger.write_report(ctx["run_id"])["text"]
    assert "## Final test run" in report
    assert "- 12 passed, 1 failed, 0 skipped, exit 1" in report


def test_report_final_test_run_section_notes_when_none_recorded(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    report = ledger.write_report(ctx["run_id"])["text"]
    assert "- No full-suite test run is recorded." in report


# -- item 17: measures section -------------------------------------------------------


def test_report_measures_lists_returns_per_file(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    file_a = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/a.py",
        "tests/test_a.py",
        "coder-p1-module-1-a",
    )
    file_b = ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/b.py",
        "tests/test_b.py",
        "coder-p1-module-1-b",
    )
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO attempts (file_id, round) VALUES (?, 1), (?, 2)",
            (file_a["file_id"], file_a["file_id"]),
        )
        conn.execute("INSERT INTO attempts (file_id, round) VALUES (?, 1)", (file_b["file_id"],))

    report = ledger.write_report(ctx["run_id"])["text"]
    assert "### Returns per file" in report
    assert "- src/a.py: 2 return(s)" in report
    assert "- src/b.py: 1 return(s)" in report


def test_report_measures_notes_no_returns_when_no_file_was_returned(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    report = ledger.write_report(ctx["run_id"])["text"]
    assert "- No file was returned." in report


def test_report_measures_lists_role_time_totals_and_cost_per_phase(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _insert_agent(
        ledger,
        "coder-agent-1",
        "coder-p1-module-1-1",
        "coder",
        run_id=ctx["run_id"],
        phase_id=ctx["phase_id"],
        model="sonnet",
        cost_usd=1.5,
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:10:00.000Z",
    )

    report = ledger.write_report(ctx["run_id"])["text"]
    assert "### Time per stage" in report
    assert "- Phase p1-phase-1 working time:" in report
    assert "- oracle agent time total:" in report
    assert "- manager agent time total:" in report
    assert "- lead agent time total:" in report
    assert "- coder agent time total: 10m 0s" in report

    assert "### Cost per phase" in report
    assert "- Phase p1-phase-1: $1.50" in report
    assert "- Oracle (run total): $" in report
