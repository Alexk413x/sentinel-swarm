from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import checklist
from swarm_ledger.db import connect, ledger_path
from swarm_ledger.identity import LedgerError

# -- checklist ----------------------------------------------------------------


def _build_ledger(
    tmp_path: Path,
    *,
    outcome: str | None = "success",
    run_state: str = "finished",
    phase_states: tuple[str, ...] = ("approved", "approved"),
    file_rows: tuple[tuple[str, str], ...] = (("a.py", "approved"), ("b.py", "superseded")),
    agent_states: tuple[str, ...] = ("released", "released"),
    full_test_run: bool = True,
    full_test_exit_code: int = 0,
    full_test_passed: int = 3,
    full_test_failed: int = 0,
    open_directives: int = 0,
    findings: tuple[tuple[str, str, bool], ...] = (),
    write_report: bool = True,
) -> tuple[Path, int]:
    db_path = ledger_path(tmp_path)
    conn = connect(db_path)
    try:
        cur = conn.execute("INSERT INTO runs (state, outcome) VALUES (?, ?)", (run_state, outcome))
        run_id = cur.lastrowid
        assert run_id is not None

        phase_ids = []
        for i, state in enumerate(phase_states):
            cur = conn.execute(
                "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, ?, ?, ?)",
                (run_id, f"phase-{i}", i, state),
            )
            phase_ids.append(cur.lastrowid)

        cur = conn.execute(
            "INSERT INTO modules (phase_id, name, state) VALUES (?, ?, ?)",
            (phase_ids[0], "module-0", "approved"),
        )
        module_id = cur.lastrowid

        for path, state in file_rows:
            conn.execute(
                "INSERT INTO files (module_id, path, state) VALUES (?, ?, ?)",
                (module_id, path, state),
            )

        for i, state in enumerate(agent_states):
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, run_id, state, session_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"agent-{i}", f"swarm-agent-{i}", "coder", run_id, state, f"session-{i}"),
            )

        if full_test_run:
            conn.execute(
                "INSERT INTO test_runs (run_id, scope, exit_code, passed, failed) "
                "VALUES (?, 'full', ?, ?, ?)",
                (run_id, full_test_exit_code, full_test_passed, full_test_failed),
            )

        for i in range(open_directives):
            conn.execute(
                "INSERT INTO directives (run_id, source, body, state) "
                "VALUES (?, 'watchdog', ?, 'open')",
                (run_id, f"directive-{i}"),
            )

        for kind, detail, live in findings:
            cleared = None if live else "2026-01-01T00:00:00.000Z"
            conn.execute(
                "INSERT INTO watchdog_findings "
                "(run_id, agent_id, kind, detail, first_seen_at, last_seen_at, cleared_at) "
                "VALUES (?, 'agent-0', ?, ?, '2026-01-01T00:00:00.000Z', "
                "'2026-01-01T00:00:00.000Z', ?)",
                (run_id, kind, detail, cleared),
            )
    finally:
        conn.close()

    if write_report:
        (db_path.parent / "report.md").write_text("# report\n", encoding="utf-8")

    return tmp_path, run_id


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The default fixture keeps every test deterministic on a machine that happens to
    # have claude on PATH; tests that want the "claude is available" path monkeypatch
    # checklist.claude_binary themselves.
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))


def test_missing_ledger_fails(tmp_path: Path) -> None:
    checks = checklist.run_checklist(tmp_path)
    assert len(checks) == 1
    assert checks[0].passed is False
    assert "a run ledger exists" in checks[0].name


def test_ledger_with_no_run_fails(tmp_path: Path) -> None:
    connect(ledger_path(tmp_path)).close()
    checks = checklist.run_checklist(tmp_path)
    assert len(checks) == 1
    assert checks[0].passed is False


def test_happy_path_all_pass_or_warn(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path)
    checks = checklist.run_checklist(root)
    assert checks, "expected at least one check"
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]


@pytest.mark.parametrize(
    "outcome,run_state,expected",
    [
        ("success", "finished", True),
        ("success: all files approved", "finished", True),
        ("succeeded", "finished", True),
        ("completed", "finished", True),
        ("incomplete", "finished", False),
        ("unsuccessful", "finished", False),
        ("failure", "finished", False),
        ("success", "active", False),
        (None, "finished", False),
    ],
)
def test_outcome_check(tmp_path: Path, outcome: str | None, run_state: str, expected: bool) -> None:
    root, _ = _build_ledger(tmp_path, outcome=outcome, run_state=run_state)
    checks = checklist.run_checklist(root)
    outcome_check = next(c for c in checks if "success-like outcome" in c.name)
    assert outcome_check.passed is expected


def test_phases_not_approved_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, phase_states=("approved", "working"))
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "every phase is approved")
    assert check.passed is False
    assert "working" in check.detail


def test_superseded_files_are_ignored(tmp_path: Path) -> None:
    root, _ = _build_ledger(
        tmp_path, file_rows=(("a.py", "approved"), ("b.py", "superseded"), ("c.py", "superseded"))
    )
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name.startswith("every file is approved"))
    assert check.passed is True


def test_incomplete_file_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, file_rows=(("a.py", "approved"), ("b.py", "incomplete")))
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name.startswith("every file is approved"))
    assert check.passed is False
    assert "b.py" in check.detail


def test_unreleased_agent_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, agent_states=("released", "working"))
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "every agent is released")
    assert check.passed is False


def test_no_full_test_run_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, full_test_run=False)
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "the last full test run passed")
    assert check.passed is False
    assert "no full-scope test run recorded" in check.detail


def test_failing_full_test_run_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, full_test_exit_code=1, full_test_passed=2, full_test_failed=1)
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "the last full test run passed")
    assert check.passed is False


def test_open_directive_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, open_directives=1)
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "no open directive")
    assert check.passed is False
    assert "1 open directive" in check.detail


def test_live_watchdog_finding_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, findings=(("stuck", "coder is stuck", True),))
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "no live watchdog findings")
    assert check.passed is False


def test_cleared_watchdog_finding_is_a_warning(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, findings=(("spinning", "coder spun twice", False),))
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "no live watchdog findings")
    assert check.passed is True
    assert check.warning is True


def test_missing_report_fails(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path, write_report=False)
    checks = checklist.run_checklist(root)
    check = next(c for c in checks if c.name == "a report file exists")
    assert check.passed is False


def test_claude_unavailable_skips_session_and_server_checks_as_warnings(tmp_path: Path) -> None:
    root, _ = _build_ledger(tmp_path)
    checks = checklist.run_checklist(root)
    session_check = next(c for c in checks if "swarm session" in c.name)
    server_check = next(c for c in checks if "ledger server process" in c.name)
    for check in (session_check, server_check):
        assert check.passed is True
        assert check.warning is True
        assert "not on PATH" in check.detail


def test_claude_available_with_a_running_session_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _build_ledger(tmp_path)
    monkeypatch.setattr(checklist, "claude_binary", lambda: "python")
    monkeypatch.setattr(
        checklist,
        "list_sessions",
        lambda: [{"name": "session-0", "status": "running", "pid": 123}],
    )
    monkeypatch.setattr(checklist, "read_server_info", lambda repo: None)
    checks = checklist.run_checklist(root)
    session_check = next(c for c in checks if "swarm session" in c.name)
    assert session_check.passed is False
    assert "session-0" in session_check.detail


def test_claude_available_with_no_running_session_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _build_ledger(tmp_path)
    monkeypatch.setattr(checklist, "claude_binary", lambda: "python")
    monkeypatch.setattr(
        checklist,
        "list_sessions",
        lambda: [{"name": "session-0", "status": "stopped", "pid": 123}],
    )
    monkeypatch.setattr(checklist, "read_server_info", lambda repo: None)
    checks = checklist.run_checklist(root)
    session_check = next(c for c in checks if "swarm session" in c.name)
    assert session_check.passed is True


def test_ledger_server_still_answering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _build_ledger(tmp_path)
    monkeypatch.setattr(checklist, "claude_binary", lambda: "python")
    monkeypatch.setattr(checklist, "list_sessions", lambda: [])
    monkeypatch.setattr(checklist, "read_server_info", lambda repo: {"port": 1})
    monkeypatch.setattr(checklist, "is_answering", lambda info, repo: True)
    checks = checklist.run_checklist(root)
    server_check = next(c for c in checks if "ledger server process" in c.name)
    assert server_check.passed is False
    assert "still answering" in server_check.detail


def test_session_listing_error_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _build_ledger(tmp_path)
    monkeypatch.setattr(checklist, "claude_binary", lambda: "python")

    def _boom() -> list[dict]:
        raise LedgerError("claude agents --json failed")

    monkeypatch.setattr(checklist, "list_sessions", _boom)
    monkeypatch.setattr(checklist, "read_server_info", lambda repo: None)
    checks = checklist.run_checklist(root)
    session_check = next(c for c in checks if "swarm session" in c.name)
    server_check = next(c for c in checks if "ledger server process" in c.name)
    assert session_check.passed is False
    assert "could not list sessions" in session_check.detail
    assert server_check.passed is True


def test_line_labels() -> None:
    assert checklist._line(checklist.Check("x", True, "ok")) == "PASS  x: ok"
    assert checklist._line(checklist.Check("x", False, "bad")) == "FAIL  x: bad"
    assert checklist._line(checklist.Check("x", True, "meh", warning=True)) == "WARN  x: meh"
    assert checklist._line(checklist.Check("x", True)) == "PASS  x"


def test_main_prints_lines_and_exits_nonzero_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _build_ledger(tmp_path, phase_states=("approved", "working"))
    code = checklist.main(["--repo", str(root)])
    out = capsys.readouterr().out
    assert code != 0
    assert "FAIL" in out
    assert "every phase is approved" in out


def test_main_exits_zero_when_every_check_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _build_ledger(tmp_path)
    code = checklist.main(["--repo", str(root)])
    out = capsys.readouterr().out
    assert code == 0
    assert "FAIL" not in out


def test_readonly_open_falls_back_when_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _build_ledger(tmp_path)

    real_connect = sqlite3.connect

    def _flaky_connect(target: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if target.startswith("file:") and "mode=ro" in target:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(checklist.sqlite3, "connect", _flaky_connect)
    checks = checklist.run_checklist(root)
    assert any(c.name.startswith("the run finished") for c in checks)


# -- plugin.json userConfig ----------------------------------------------------


def test_user_config_test_command_schema(repo_root: Path) -> None:
    data = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    option = data["userConfig"]["test_command"]
    allowed = {
        "type",
        "title",
        "description",
        "required",
        "default",
        "options",
        "multiple",
        "sensitive",
        "min",
        "max",
    }
    assert set(option) <= allowed
    assert option["type"] == "string"
    assert str(option["title"]).strip()
    assert str(option["description"]).strip()


def test_setup_skill_reads_the_user_config_option(repo_root: Path) -> None:
    text = (repo_root / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "${user_config.test_command}" in text


# -- smoke.sh -------------------------------------------------------------------


def test_smoke_sh_rewrites_a_dev_version_before_install(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    install_at = text.index("plugin marketplace add")
    version_at = text.index("d['version']=sys.argv[2]")
    assert version_at < install_at
    assert "0.0.1-dev." in text


def test_smoke_sh_cleans_up_old_dev_cache_copies(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    assert 'plugins/cache/sentinel-swarm/sentinel-swarm"/*-dev.*' in text


def test_smoke_sh_results_runs_the_checklist(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    results_at = text.index('mode" = results')
    checklist_at = text.index("swarm_ledger.checklist")
    exit_at = text.index('exit "$rc"')
    assert results_at < checklist_at < exit_at
