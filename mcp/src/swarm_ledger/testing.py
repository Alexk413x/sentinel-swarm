from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

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


def build_command(command_template: str, target: str | None) -> str:
    if target is None:
        return " ".join(command_template.replace("{target}", "").split())
    return command_template.replace("{target}", _quote_target(target))


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


def _evaluate(exit_code: int, counts: _Counts) -> tuple[bool, str | None]:
    ok = (
        exit_code == 0
        and counts.failed == 0
        and counts.errors == 0
        and counts.skipped == 0
        and counts.passed >= 1
    )
    if ok:
        return True, None
    if counts.no_tests_ran:
        return False, "no tests ran"
    if exit_code != 0:
        return False, f"exit code {exit_code}"
    if counts.skipped:
        return False, f"{counts.skipped} skipped"
    if counts.passed < 1:
        return False, "no tests ran"
    return False, f"exit code {exit_code}"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    stdout_text = _as_text(stdout)
    stderr_text = _as_text(stderr)
    if stdout_text and stderr_text:
        return f"{stdout_text}\n{stderr_text}"
    return stdout_text or stderr_text


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


def run_tests(
    command_template: str, target: str | None, cwd: Path, timeout_s: int = 600
) -> TestResult:
    command = build_command(command_template, target)
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        output = _cap_output(_combine_output(exc.stdout, exc.stderr))
        return TestResult(
            command=command,
            exit_code=-1,
            passed=0,
            failed=0,
            skipped=0,
            errors=0,
            output=output,
            duration_ms=duration_ms,
            ok=False,
            reason="timed out",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    output = _combine_output(completed.stdout, completed.stderr)
    counts = _parse_counts(output)
    ok, reason = _evaluate(completed.returncode, counts)
    return TestResult(
        command=command,
        exit_code=completed.returncode,
        passed=counts.passed,
        failed=counts.failed,
        skipped=counts.skipped,
        errors=counts.errors,
        output=_cap_output(output),
        duration_ms=duration_ms,
        ok=ok,
        reason=reason,
    )
