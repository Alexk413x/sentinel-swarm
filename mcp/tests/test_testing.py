from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from swarm_ledger.testing import run_tests

_PYTHON = f'"{sys.executable}"' if " " in sys.executable else sys.executable


def _python_command(script: str) -> str:
    return f'{_PYTHON} -c "{script}"'


def test_run_tests_ok_on_a_clean_pytest_style_pass(tmp_path: Path) -> None:
    command = _python_command("print('5 passed in 0.12s'); raise SystemExit(0)")
    result = run_tests(command, None, tmp_path)
    assert result.exit_code == 0
    assert result.passed == 5
    assert result.failed == 0
    assert result.skipped == 0
    assert result.errors == 0
    assert result.ok is True
    assert result.reason is None
    assert result.duration_ms >= 0


def test_run_tests_hides_the_ledger_venv_from_the_host_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "ledger-venv"
    venv_bin = venv / "Scripts"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("PATH", os.pathsep.join([str(venv_bin), str(tmp_path / "other")]))

    script = "import os; print(os.environ.get('VIRTUAL_ENV')); print(os.environ['PATH'])"
    result = run_tests(_python_command(script), None, tmp_path)

    lines = result.output.splitlines()
    assert lines[0] == "None"
    assert str(venv_bin) not in lines[1]
    assert str(tmp_path / "other") in lines[1]


def test_run_tests_reports_failures_via_pytest_style_summary(tmp_path: Path) -> None:
    command = _python_command("print('1 failed, 2 passed in 0.05s'); raise SystemExit(1)")
    result = run_tests(command, None, tmp_path)
    assert result.exit_code == 1
    assert result.passed == 2
    assert result.failed == 1
    assert result.ok is False
    assert result.reason == "exit code 1"


def test_run_tests_reports_errors_via_pytest_style_summary(tmp_path: Path) -> None:
    command = _python_command("print('1 error in 0.01s'); raise SystemExit(2)")
    result = run_tests(command, None, tmp_path)
    assert result.exit_code == 2
    assert result.errors == 1
    assert result.ok is False
    assert result.reason == "exit code 2"


def test_run_tests_skipped_tests_fail_ok_with_a_skipped_reason(tmp_path: Path) -> None:
    command = _python_command("print('3 passed, 1 skipped in 0.10s'); raise SystemExit(0)")
    result = run_tests(command, None, tmp_path)
    assert result.exit_code == 0
    assert result.passed == 3
    assert result.skipped == 1
    assert result.ok is False
    assert result.reason == "1 skipped"


def test_run_tests_no_tests_ran_is_reported_by_name(tmp_path: Path) -> None:
    command = _python_command("print('no tests ran in 0.00s'); raise SystemExit(5)")
    result = run_tests(command, None, tmp_path)
    assert result.passed == 0
    assert result.ok is False
    assert result.reason == "no tests ran"


def test_run_tests_generic_fallback_when_exit_zero_and_nothing_parses(tmp_path: Path) -> None:
    command = _python_command("print('done'); raise SystemExit(0)")
    result = run_tests(command, None, tmp_path)
    assert result.exit_code == 0
    assert result.passed == 0
    assert result.failed == 0
    assert result.ok is False
    assert result.reason == "no tests ran"


def test_run_tests_parses_jest_vitest_style_summary(tmp_path: Path) -> None:
    command = _python_command(
        "print('Tests: 3 passed, 1 failed, 2 skipped, 6 total'); raise SystemExit(1)"
    )
    result = run_tests(command, None, tmp_path)
    assert result.passed == 3
    assert result.failed == 1
    assert result.skipped == 2
    assert result.ok is False
    assert result.reason == "exit code 1"


def test_run_tests_parses_go_style_ok_lines_as_a_pass(tmp_path: Path) -> None:
    command = _python_command("print('ok  \\tpkg/foo\\t0.005s'); raise SystemExit(0)")
    result = run_tests(command, None, tmp_path)
    assert result.passed == 1
    assert result.failed == 0
    assert result.skipped == 0
    assert result.ok is True
    assert result.reason is None


def test_run_tests_parses_go_style_fail_and_skip_lines(tmp_path: Path) -> None:
    command = _python_command(
        "print('ok  \\tpkg/foo\\t0.005s'); "
        "print('FAIL\\tpkg/bar\\t0.010s'); "
        "print('--- SKIP: TestBaz (0.00s)'); "
        "raise SystemExit(1)"
    )
    result = run_tests(command, None, tmp_path)
    assert result.passed == 1
    assert result.failed == 1
    assert result.skipped == 1
    assert result.ok is False
    assert result.reason == "exit code 1"


def test_run_tests_times_out(tmp_path: Path) -> None:
    command = _python_command("import time; time.sleep(2)")
    result = run_tests(command, None, tmp_path, timeout_s=1)
    assert result.exit_code == -1
    assert result.ok is False
    assert result.reason == "timed out"
    assert result.duration_ms < 3000


def test_run_tests_substitutes_target_placeholder(tmp_path: Path) -> None:
    command_template = (
        _python_command("import sys; print(sys.argv[1]); print('1 passed')") + " {target}"
    )
    result = run_tests(command_template, "tests/test_foo.py", tmp_path)
    assert "tests/test_foo.py" in result.command
    assert "tests/test_foo.py" in result.output
    assert result.passed == 1
    assert result.ok is True


def test_run_tests_quotes_a_target_containing_a_space_on_windows(tmp_path: Path) -> None:
    command_template = (
        _python_command("import sys; print(sys.argv[1]); print('1 passed')") + " {target}"
    )
    result = run_tests(command_template, "my target.py", tmp_path)
    assert '"my target.py"' in result.command
    assert "my target.py" in result.output
    assert result.ok is True


def test_run_tests_caps_output_at_twenty_thousand_characters(tmp_path: Path) -> None:
    command = _python_command("print('x' * 25000); print('1 passed')")
    result = run_tests(command, None, tmp_path)
    assert len(result.output) <= 20000
    assert result.output.rstrip().endswith("1 passed")
    assert result.ok is True


def test_run_tests_drops_the_placeholder_when_no_target_is_given(tmp_path: Path) -> None:
    command_template = _python_command("import sys; print(sys.argv[1:]); print('1 passed')")
    result = run_tests(command_template + " {target}", None, tmp_path)
    assert "{target}" not in result.command
    assert result.ok is True
