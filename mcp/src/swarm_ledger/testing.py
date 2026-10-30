from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .pool import hidden_console_flags

_MAX_OUTPUT_CHARS = 20000
SUMMARY_CHARS = 4000
_TRACEBACK_CHARS = 2500
_PASS_TAIL_CHARS = 500
_FAILURES_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+$", re.MULTILINE)
_SECTION_RE = re.compile(r"^(_{3,} .* _{3,}|=+ .* =+)$", re.MULTILINE)
_SHORT_SUMMARY_RE = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)

_NO_TESTS_RAN_RE = re.compile(r"no tests ran")
_PASSED_RE = re.compile(r"(\d+)\s+passed\b")
_FAILED_RE = re.compile(r"(\d+)\s+failed\b")
_SKIPPED_RE = re.compile(r"(\d+)\s+skipped\b")
_ERRORS_RE = re.compile(r"(\d+)\s+errors?\b")

_GO_OK_LINE_RE = re.compile(r"^ok\s", re.MULTILINE)
_GO_FAIL_LINE_RE = re.compile(r"^FAIL\b", re.MULTILINE)
_GO_SKIP_LINE_RE = re.compile(r"^\s*--- SKIP\b", re.MULTILINE)

_SKIP_SUMMARY_RE = re.compile(r"^SKIPPED \[(\d+)\] (.+)$", re.MULTILINE)
_SKIP_VERBOSE_RE = re.compile(r"^(\S+::\S+) SKIPPED\b(.*)$", re.MULTILINE)
_PYTEST_WORD_RE = re.compile(r"(^|[\s/\\])(py\.test|pytest)(\.exe)?(\s|$)")
_KILL_WAIT_S = 10


@dataclass
class TestResult:
    command: str
    exit_code: int
    passed: int
    failed: int
    skipped: int
    errors: int
    output: str
    duration_ms: int
    ok: bool
    reason: str | None
    allowed_skipped: int = 0


@dataclass
class Executed:
    exit_code: int
    output: str
    duration_ms: int
    timed_out: bool


@dataclass
class _Counts:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    no_tests_ran: bool = False


def _quote_target(target: str) -> str:
    if os.name == "nt":
        return f'"{target}"' if " " in target else target
    return shlex.quote(target)


def is_pytest(command: str) -> bool:
    return _PYTEST_WORD_RE.search(command) is not None


def build_command(
    command_template: str,
    target: str | None,
    slow_tests: Sequence[str] = (),
    include_slow: bool = False,
) -> str:
    if target is None:
        command = " ".join(command_template.replace("{target}", "").split())
    else:
        command = command_template.replace("{target}", _quote_target(target))
    if include_slow or not slow_tests or not is_pytest(command):
        return command
    return " ".join([command, *(f"--deselect {_quote_target(test)}" for test in slow_tests)])


def collect_only_command(command: str) -> str | None:
    return f"{command} --collect-only -q" if is_pytest(command) else None


def count_allowed_skips(output: str, patterns: Sequence[str], skipped: int) -> int:
    if not patterns or skipped <= 0:
        return 0
    allowed = 0
    summary = _SKIP_SUMMARY_RE.findall(output)
    if summary:
        for count, text in summary:
            if any(pattern in text for pattern in patterns):
                allowed += int(count)
    else:
        for test_id, rest in _SKIP_VERBOSE_RE.findall(output):
            if any(pattern in f"{test_id}{rest}" for pattern in patterns):
                allowed += 1
    return min(allowed, skipped)


def _parse_counts(output: str) -> _Counts:
    if _NO_TESTS_RAN_RE.search(output):
        return _Counts(no_tests_ran=True)

    passed_match = _PASSED_RE.search(output)
    failed_match = _FAILED_RE.search(output)
    skipped_match = _SKIPPED_RE.search(output)
    errors_match = _ERRORS_RE.search(output)
    if passed_match or failed_match or skipped_match or errors_match:
        return _Counts(
            passed=int(passed_match.group(1)) if passed_match else 0,
            failed=int(failed_match.group(1)) if failed_match else 0,
            skipped=int(skipped_match.group(1)) if skipped_match else 0,
            errors=int(errors_match.group(1)) if errors_match else 0,
        )

    go_passed = len(_GO_OK_LINE_RE.findall(output))
    go_failed = len(_GO_FAIL_LINE_RE.findall(output))
    go_skipped = len(_GO_SKIP_LINE_RE.findall(output))
    if go_passed or go_failed or go_skipped:
        return _Counts(passed=go_passed, failed=go_failed, skipped=go_skipped)

    return _Counts()


def _evaluate(exit_code: int, counts: _Counts, allowed_skipped: int = 0) -> tuple[bool, str | None]:
    unexplained = counts.skipped - allowed_skipped
    ok = (
        exit_code == 0
        and counts.failed == 0
        and counts.errors == 0
        and unexplained == 0
        and counts.passed >= 1
    )
    if ok:
        return True, None
    if counts.no_tests_ran:
        return False, "no tests ran"
    if exit_code != 0:
        return False, f"exit code {exit_code}"
    if unexplained:
        return False, f"{unexplained} skipped"
    if counts.passed < 1:
        return False, "no tests ran"
    return False, f"exit code {exit_code}"


def _cap_output(output: str) -> str:
    return output[-_MAX_OUTPUT_CHARS:]


def summarize_output(output: str, ok: bool) -> str:
    if ok:
        return output[-_PASS_TAIL_CHARS:]
    parts: list[str] = []
    failures = _FAILURES_RE.search(output)
    if failures:
        rest = output[failures.end() :]
        first = _SECTION_RE.search(rest)
        if first:
            after = rest[first.end() :]
            end = _SECTION_RE.search(after)
            block = rest[first.start() : first.end() + (end.start() if end else len(after))]
            parts.append(block.strip()[:_TRACEBACK_CHARS])
    summary = _SHORT_SUMMARY_RE.search(output)
    if summary:
        parts.append(output[summary.start() :].strip())
    if not parts:
        return output[-SUMMARY_CHARS:]
    return "\n...\n".join(parts)[-SUMMARY_CHARS:]


def _git(repo_root: Path, *args: str, stdin: str | None = None) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def tree_fingerprint(repo_root: Path, claimed: list[str]) -> str | None:
    head = _git(repo_root, "rev-parse", "HEAD")
    tracked = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=no")
    if head is None or tracked is None:
        return None
    digest = hashlib.sha256()
    for part in (head, tracked):
        digest.update(part.encode("utf-8", errors="replace"))
    for path in sorted(set(claimed)):
        target = repo_root / path
        content = target.read_bytes() if target.is_file() else b"<missing>"
        digest.update(path.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=hidden_console_flags(),
                timeout=_KILL_WAIT_S,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def execute(command: str, cwd: Path, timeout_s: int, log_path: Path | None = None) -> Executed:
    if log_path is None:
        handle, name = tempfile.mkstemp(prefix="sentinel-swarm-test-", suffix=".log")
        os.close(handle)
        path, temporary = Path(name), True
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        path, temporary = log_path, False
    start = time.monotonic()
    timed_out = False
    try:
        with path.open("wb") as log:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=hidden_console_flags(new_process_group=True),
                start_new_session=sys.platform != "win32",
            )
            try:
                exit_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = -1
            duration_ms = int((time.monotonic() - start) * 1000)
            if timed_out:
                _kill_tree(proc)
        output = path.read_bytes().decode("utf-8", errors="replace")
    finally:
        if temporary:
            path.unlink(missing_ok=True)
    return Executed(
        exit_code=exit_code,
        output=output,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )


def result_of(command: str, executed: Executed, allowed_skips: Sequence[str] = ()) -> TestResult:
    if executed.timed_out:
        return TestResult(
            command=command,
            exit_code=-1,
            passed=0,
            failed=0,
            skipped=0,
            errors=0,
            output=_cap_output(executed.output),
            duration_ms=executed.duration_ms,
            ok=False,
            reason="timed out",
        )
    counts = _parse_counts(executed.output)
    allowed = count_allowed_skips(executed.output, allowed_skips, counts.skipped)
    ok, reason = _evaluate(executed.exit_code, counts, allowed)
    return TestResult(
        command=command,
        exit_code=executed.exit_code,
        passed=counts.passed,
        failed=counts.failed,
        skipped=counts.skipped,
        errors=counts.errors,
        output=_cap_output(executed.output),
        duration_ms=executed.duration_ms,
        ok=ok,
        reason=reason,
        allowed_skipped=allowed,
    )


def run_tests(
    command_template: str,
    target: str | None,
    cwd: Path,
    timeout_s: int = 600,
    allowed_skips: Sequence[str] = (),
) -> TestResult:
    command = build_command(command_template, target)
    return result_of(command, execute(command, cwd, timeout_s), allowed_skips)
