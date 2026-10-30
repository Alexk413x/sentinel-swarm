from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import (
    as_session,
    bootstrap_module,
    make_host,
    settings_text,
    spawn_coder,
    untimed,
)

from swarm_ledger import review, setup, testing, testrun
from swarm_ledger.clock import utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import load_settings
from swarm_ledger.watchdog import scan

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_PASS = f"{_PYTHON} -c \"print('3 passed')\" {{target}}"
_CODER = "coder-p1-module-1-good"

pytestmark = pytest.mark.usefixtures("claude_sessions")


def _git_repo(root: Path) -> None:
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A"],
        ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _host(
    tmp_path: Path, full_target: str = "", swaps: list[tuple[str, str]] | None = None
) -> Path:
    text = settings_text(test_command=_PASS, swaps=swaps or [])
    if full_target:
        text = text.replace("full_target:\n", f"full_target: {full_target}\n")
    root = make_host(tmp_path / "host", text)
    (root / "pkg").mkdir()
    (root / "pkg" / "good.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_good.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git_repo(root)
    return root


def _ledger(root: Path) -> Ledger:
    return Ledger(root, db_path=root / ".sentinel-swarm" / "ledger.db")


def _hold_runs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    started: list[int] = []

    def launch(root: Path, db_path: Path, test_run_id: int) -> int:
        started.append(test_run_id)
        return os.getpid()

    monkeypatch.setattr(testrun, "launcher", launch)
    monkeypatch.setattr(review, "INLINE_WAIT_S", 0.0)
    return started


def _coder(ledger: Ledger) -> tuple[dict, dict]:
    ctx = bootstrap_module(ledger)
    coder = spawn_coder(ledger, ctx, _CODER, "pkg/good.py", "tests/test_good.py")
    as_session(ledger, coder["agent_id"], _CODER)
    return ctx, coder


def _row(ledger: Ledger, test_run_id: int) -> dict:
    return dict(
        ledger.conn.execute(
            "SELECT * FROM test_runs WHERE test_run_id = ?", (test_run_id,)
        ).fetchone()
    )


# -- settings ---------------------------------------------------------------------


def test_the_test_settings_load_from_the_settings_file(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(
            test_command="pytest -q {target}",
            full_target="tests",
            swaps=[
                ("test_timeout_s: 600", "test_timeout_s: 1800"),
                ("  full:\n", "  full: pytest -q -n 4 {target}\n  bogus: x\n"),
                ("allowed_skips: []", 'allowed_skips: ["CORPUS is unset"]'),
                ("slow_tests: []", "slow_tests: [tests/bench]"),
            ],
        ),
    )
    settings = load_settings(root)
    assert settings.test_timeout_s == 1800
    assert settings.full_target == "tests"
    assert settings.test_command_by_scope == {"full": "pytest -q -n 4 {target}"}
    assert settings.test_template("full") == "pytest -q -n 4 {target}"
    assert settings.test_template("file") == "pytest -q {target}"
    assert settings.allowed_skips == ["CORPUS is unset"]
    assert settings.slow_tests == ["tests/bench"]


def test_a_bad_test_timeout_reads_as_the_default(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host", settings_text(swaps=[("test_timeout_s: 600", "test_timeout_s: -3")])
    )
    assert load_settings(root).test_timeout_s == 600


# -- commands and counts --------------------------------------------------------------


def test_slow_tests_are_deselected_from_a_pytest_command_unless_asked_for() -> None:
    command = testing.build_command("python -m pytest -q {target}", "tests", ["tests/bench"])
    assert command == "python -m pytest -q tests --deselect tests/bench"
    assert testing.build_command("pytest {target}", "tests", ["b"], include_slow=True) == (
        "pytest tests"
    )
    assert testing.build_command("go test {target}", "./...", ["b"]) == "go test ./..."


def test_collect_only_applies_to_pytest_alone() -> None:
    assert testing.collect_only_command("pytest -q tests") == "pytest -q tests --collect-only -q"
    assert testing.collect_only_command("npx jest x") is None


def test_a_skip_named_in_allowed_skips_counts_as_clean() -> None:
    output = "SKIPPED [1] tests/test_bench.py:12: CORPUS is unset\n3 passed, 1 skipped in 0.1s\n"
    run = testing.Executed(exit_code=0, output=output, duration_ms=5, timed_out=False)
    allowed = testing.result_of("pytest", run, ["CORPUS is unset"])
    assert (allowed.ok, allowed.allowed_skipped) == (True, 1)
    other = testing.result_of("pytest", run, ["something else"])
    assert (other.ok, other.reason) == (False, "1 skipped")
    assert testing.result_of("pytest", run).ok is False


def test_a_verbose_skip_line_matches_too() -> None:
    output = "tests/test_b.py::test_corpus SKIPPED (CORPUS is unset)\n2 passed, 1 skipped\n"
    assert testing.count_allowed_skips(output, ["CORPUS"], 1) == 1


def test_execute_kills_the_process_tree_at_the_timeout(tmp_path: Path) -> None:
    command = f"{_PYTHON} -c \"import time; print('started', flush=True); time.sleep(30)\""
    ran = testing.execute(command, tmp_path, 1, tmp_path / "run.log")
    assert ran.timed_out is True
    assert ran.exit_code == -1
    assert "started" in ran.output
    assert ran.duration_ms < 25_000


# -- the asynchronous run ---------------------------------------------------------------


def test_a_long_run_returns_running_and_records_when_its_runner_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)

    first = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert first["state"] == "running"
    assert first["ok"] is False and first["reason"] == "still running"
    assert "End your turn" in first["next"]
    assert started == [first["test_run_id"]]
    row = _row(ledger, first["test_run_id"])
    assert (row["runner_pid"], row["timeout_s"]) == (os.getpid(), 600)

    again = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert again["test_run_id"] == first["test_run_id"]
    assert started == [first["test_run_id"]]

    testrun.run(ledger.repo_root, ledger.db_path, first["test_run_id"])
    done = ledger.test_run_get(_CODER, coder["agent_id"], first["test_run_id"])
    assert (done["state"], done["passed"], done["ok"]) == ("done", 3, True)

    assert ledger.owed_wakeups(coder["agent_id"]) == []
    owed = ledger.take_self_wakeups(coder["agent_id"])
    assert [w["reason"] for w in owed] == ["test_run"]
    assert (
        f"Test run {first['test_run_id']} (file tests/test_good.py) is recorded, passed"
        in (owed[0]["pointer"])
    )
    assert ledger.take_self_wakeups(coder["agent_id"]) == []


def test_a_run_that_ends_inside_the_wait_comes_back_and_owes_nothing(
    tmp_path: Path,
) -> None:
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    result = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert (result["state"], result["ok"], result["passed"]) == ("done", True, 3)
    assert ledger.take_self_wakeups(coder["agent_id"]) == []


def test_handoff_submit_refuses_while_its_test_run_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    with pytest.raises(LedgerError, match="still running"):
        ledger.handoff_submit(_CODER, coder["agent_id"], coder["file_id"], [], [])


def test_the_test_timeout_setting_bounds_the_runner(tmp_path: Path) -> None:
    sleeper = f'{_PYTHON} -c "import time; time.sleep(30)" {{target}}'
    root = make_host(
        tmp_path / "host",
        settings_text(test_command=sleeper, swaps=[("test_timeout_s: 600", "test_timeout_s: 1")]),
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_good.py").write_text("", encoding="utf-8")
    ledger = _ledger(root)
    _, coder = _coder(ledger)
    result = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert (result["state"], result["reason"], result["exit_code"]) == ("done", "timed out", -1)


# -- full runs ----------------------------------------------------------------------------


def test_a_full_run_refuses_an_empty_target(tmp_path: Path) -> None:
    ledger = _ledger(_host(tmp_path))
    ctx = bootstrap_module(ledger)
    with pytest.raises(LedgerError, match="full_target is not set"):
        ledger.tests_run("oracle", ctx["oracle_id"], "full")


def test_a_full_run_takes_full_target_and_its_scope_command(tmp_path: Path) -> None:
    full = f"{_PYTHON} -c \"print('9 passed')\" --full {{target}}"
    root = _host(tmp_path, full_target="tests", swaps=[("  full:\n", f"  full: {full}\n")])
    ledger = _ledger(root)
    ctx = bootstrap_module(ledger)
    result = ledger.tests_run("oracle", ctx["oracle_id"], "full")
    assert result["command"].endswith("--full tests")
    assert (result["passed"], result["ok"]) == (9, True)


def test_a_full_run_whose_tree_changed_is_stale_and_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path, full_target="tests"))
    ctx, _ = _coder(ledger)
    as_session(ledger, ctx["oracle_id"], "oracle")
    running = ledger.tests_run("oracle", ctx["oracle_id"], "full")

    (ledger.repo_root / "pkg" / "good.py").write_text("X = 2\n", encoding="utf-8")
    testrun.run(ledger.repo_root, ledger.db_path, running["test_run_id"])

    row = _row(ledger, running["test_run_id"])
    assert row["state"] == "stale"
    assert "working tree changed" in row["reason"]
    owed = ledger.take_self_wakeups(ctx["oracle_id"])
    assert "not counted" in owed[0]["pointer"]
    assert not ledger._passing_full_test_after(ledger.conn, ctx["run_id"], None)


def test_phase_review_names_the_scope_and_a_run_still_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path, full_target="tests"))
    ctx = bootstrap_module(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE phases SET state = 'handed_up', handed_up_at = '2000-01-01T00:00:00.000Z' "
            "WHERE phase_id = ?",
            (ctx["phase_id"],),
        )
    phase = ledger.conn.execute(
        "SELECT * FROM phases WHERE phase_id = ?", (ctx["phase_id"],)
    ).fetchone()
    message = ledger._full_run_needed(ledger.conn, phase)
    assert "scope 'full'" in message and "tests" in message
    assert "Call tests_run(scope='full')" in message

    running = ledger.tests_run("oracle", ctx["oracle_id"], "full")
    message = ledger._full_run_needed(ledger.conn, phase)
    assert f"Test run {running['test_run_id']} is still running" in message


# -- abandoned runs ---------------------------------------------------------------------


def test_sweep_abandons_a_run_whose_runner_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    alive = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    gone = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py", force=True)
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE test_runs SET runner_pid = 999999999 WHERE test_run_id = ?",
            (gone["test_run_id"],),
        )

    assert testrun.sweep(ledger.conn) == [gone["test_run_id"]]
    assert _row(ledger, alive["test_run_id"])["state"] == "running"
    abandoned = _row(ledger, gone["test_run_id"])
    assert (abandoned["state"], abandoned["reason"]) == ("abandoned", testrun.NO_RUNNER)
    owed = ledger.take_self_wakeups(coder["agent_id"])
    assert "abandoned" in owed[0]["pointer"]


def test_sweep_abandons_a_run_long_past_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    run = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE test_runs SET created_at = '2000-01-01T00:00:00.000Z' WHERE test_run_id = ?",
            (run["test_run_id"],),
        )
    assert testrun.sweep(ledger.conn) == [run["test_run_id"]]


def test_the_detached_runner_records_the_run_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(testrun, "launcher", testrun.spawn)
    monkeypatch.setattr(review, "INLINE_WAIT_S", 0.0)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    run = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert run["state"] == "running"
    assert _row(ledger, run["test_run_id"])["runner_pid"] is not None

    deadline = time.monotonic() + 60
    while _row(ledger, run["test_run_id"])["state"] == "running":
        assert time.monotonic() < deadline, "the runner did not record the run"
        time.sleep(0.2)
    row = _row(ledger, run["test_run_id"])
    assert (row["state"], row["passed"]) == ("done", 3)
    assert not testrun.log_path(ledger.db_path, run["test_run_id"]).exists()


# -- delivery -------------------------------------------------------------------------------


def test_the_stop_hook_delivers_a_recorded_test_result_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    run = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    testrun.run(ledger.repo_root, ledger.db_path, run["test_run_id"])

    blocked = events.handle_stop(ledger, {"session_id": coder["agent_id"]})
    assert blocked is not None and blocked["decision"] == "block"
    assert f"Test run {run['test_run_id']}" in blocked["reason"]
    assert events.handle_self_owed(ledger, {"session_id": coder["agent_id"]}) == {
        "text": None,
        "running": 0,
    }


def test_self_owed_answers_the_text_and_the_runs_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path, full_target="tests"))
    ctx = bootstrap_module(ledger)
    as_session(ledger, ctx["oracle_id"], "oracle")
    first = ledger.tests_run("oracle", ctx["oracle_id"], "full")
    ledger.tests_run("oracle", ctx["oracle_id"], "full", force=True)
    testrun.run(ledger.repo_root, ledger.db_path, first["test_run_id"])

    answer = events.handle_self_owed(ledger, {"session_id": ctx["oracle_id"]})
    assert answer is not None and answer["running"] == 1
    assert untimed(answer["text"]).startswith(f"Test run {first['test_run_id']} (full tests)")


def test_the_watchdog_reports_no_self_owed_wake_up_as_unsent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    run = ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    testrun.run(ledger.repo_root, ledger.db_path, run["test_run_id"])
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE wakeups SET created_at = '2000-01-01T00:00:00.000Z'")
    listing = [{"sessionId": coder["agent_id"], "pid": 1, "status": "idle", "name": _CODER}]
    findings = scan(ledger.conn, listing, utcnow(), ledger.settings.watchdog)
    assert [f for f in findings if f.kind == "wake_unsent"] == []


# -- setup --------------------------------------------------------------------------------


def _fake_executor(
    output: str, exit_code: int = 0, duration_ms: int = 1000, timed_out: bool = False
) -> tuple[list[str], setup.Executor]:
    ran: list[str] = []

    def execute(command: str, cwd: Path, timeout_s: int) -> testing.Executed:
        ran.append(command)
        return testing.Executed(exit_code, output, duration_ms, timed_out)

    return ran, execute


def test_setup_reports_a_missing_test_command(tmp_path: Path) -> None:
    root = make_host(tmp_path / "host")
    report = setup.SetupReport()
    setup.check_tests(root, report)
    assert report.lines == ["test_command is not set: fill it in before a run"]


def test_setup_warns_when_full_target_is_empty(tmp_path: Path) -> None:
    root = make_host(tmp_path / "host", settings_text(test_command="pytest -q {target}"))
    report = setup.SetupReport()
    ran, execute = _fake_executor("")
    setup.check_tests(root, report, execute=execute)
    assert ran == []
    assert "full_target is not set" in report.text()


def test_setup_dry_runs_the_full_pytest_command(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(test_command="python -m pytest -q {target}", full_target="tests"),
    )
    report = setup.SetupReport()
    ran, execute = _fake_executor("4751 tests collected in 3.2s\n")
    setup.check_tests(root, report, execute=execute)
    assert ran == ["python -m pytest -q tests --collect-only -q"]
    assert "collects 4751 tests" in report.text()
    assert "allowed_skips is empty" in report.text()
    assert "--time-full-run" in report.text()


def test_setup_reports_a_collection_error_with_its_output(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(test_command="python -m pytest -q {target}", full_target="."),
    )
    report = setup.SetupReport()
    _, execute = _fake_executor("ERROR plans/jev/scripts/test_d.py - SystemExit\n1 error\n", 2)
    setup.check_tests(root, report, execute=execute)
    assert "failed with exit code 2" in report.text()
    assert "plans/jev/scripts/test_d.py" in report.text()


def test_setup_runs_the_command_through_the_shell_runner(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(test_command="plugins/run-tests {target}", full_target="tests"),
    )
    report = setup.SetupReport()
    ran, execute = _fake_executor("'plugins' is not recognized\n", 1)
    setup.check_tests(root, report, time_full_run=True, execute=execute)
    assert ran == ["plugins/run-tests tests"]
    assert "no collection dry run" in report.text()
    assert "does not count as clean: exit code 1" in report.text()


def test_setup_warns_when_a_full_run_takes_over_half_the_cap(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(test_command="python -m pytest -q {target}", full_target="tests"),
    )
    report = setup.SetupReport()
    _, execute = _fake_executor("10 passed, 2 skipped in 400s\n", duration_ms=400_000)
    setup.check_tests(root, report, time_full_run=True, execute=execute)
    text = report.text()
    assert "one full run took 400 s of the 600 s test_timeout_s" in text
    assert "more than half of test_timeout_s" in text and "pytest -n auto" in text
    assert "2 skipped test(s) that allowed_skips does not name" in text


def test_setup_warns_that_pytest_needs_rs_for_allowed_skips(tmp_path: Path) -> None:
    root = make_host(
        tmp_path / "host",
        settings_text(
            test_command="python -m pytest -q {target}",
            full_target="tests",
            swaps=[("allowed_skips: []", "allowed_skips: [CORPUS]")],
        ),
    )
    report = setup.SetupReport()
    _, execute = _fake_executor("3 tests collected\n")
    setup.check_tests(root, report, execute=execute)
    assert "-rs or -ra" in report.text()


def test_a_coder_waiting_on_its_test_run_stops_without_the_handoff_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path))
    _, coder = _coder(ledger)
    ledger.tests_run(_CODER, coder["agent_id"], "file", "tests/test_good.py")
    assert events.handle_stop(ledger, {"session_id": coder["agent_id"]}) is None


def test_an_oracle_waiting_on_its_full_run_is_not_told_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hold_runs(monkeypatch)
    ledger = _ledger(_host(tmp_path, full_target="tests"))
    ctx = bootstrap_module(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET state = 'idle' WHERE role IN ('manager', 'lead')")
    run = dict(ledger.conn.execute("SELECT * FROM runs").fetchone())
    oracle = {"agent_id": ctx["oracle_id"], "name": "oracle"}
    assert events._oracle_work_block(ledger, run, oracle) is not None
    ledger.tests_run("oracle", ctx["oracle_id"], "full")
    assert events._oracle_work_block(ledger, run, oracle) is None
