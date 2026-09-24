from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .identity import LedgerError

CLAUDE_VAR = "SENTINEL_SWARM_CLAUDE"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BG_LINE = re.compile(r"backgrounded\s+\S+\s+([0-9A-Za-z-]+)")
_LABELS = ("agents", "stop", "--resume", "--bg")
_DEAD = frozenset({"stopped", "exited", "crashed", "failed", "killed", "dead", "done", "completed"})
_TIMEOUT_S = 60.0
_SPAWN_WAIT_S = 15.0
_POLL_S = 0.5


def claude_binary() -> str:
    raw = os.environ.get(CLAUDE_VAR) or "claude"
    return shutil.which(raw) or raw


def _run(args: list[str], cwd: Path | None = None) -> str:
    binary = claude_binary()
    # Files, not pipes: a backgrounded session can inherit the handles and hold a pipe open.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(
                [binary, *args],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise LedgerError(
                f"{binary} {_describe(args)} timed out after {_TIMEOUT_S:.0f}s"
            ) from exc
        except OSError as exc:
            raise LedgerError(f"cannot run {binary!r}: {exc}") from exc
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", errors="replace")
        stderr = err.read().decode("utf-8", errors="replace")
    if result.returncode != 0:
        detail = _strip_ansi(stderr).strip() or _strip_ansi(stdout).strip()
        raise LedgerError(
            f"{binary} {_describe(args)} failed with exit code {result.returncode}: {detail}"
        )
    return stdout


def _describe(args: list[str]) -> str:
    return next((a for a in args if a in _LABELS), "")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_bg_id(output: str) -> str | None:
    match = _BG_LINE.search(_strip_ansi(output))
    return match.group(1) if match else None


def list_sessions() -> list[dict]:
    raw = _run(["agents", "--json"])
    try:
        data = json.loads(_strip_ansi(raw) or "[]")
    except json.JSONDecodeError as exc:
        raise LedgerError(f"claude agents --json printed invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise LedgerError("claude agents --json did not print a list")
    return [entry for entry in data if isinstance(entry, dict)]


def is_running(entry: dict) -> bool:
    states = {str(entry.get(key) or "").lower() for key in ("status", "state")}
    return bool(entry.get("pid")) and not (states & _DEAD)


def find_session(sessions: list[dict], session_id: str) -> dict | None:
    return next((s for s in sessions if s.get("sessionId") == session_id), None)


def is_live(session_id: str) -> bool:
    entry = find_session(list_sessions(), session_id)
    return entry is not None and is_running(entry)


def live_names(sessions: list[dict]) -> set[str]:
    return {str(s["name"]) for s in sessions if s.get("name") and is_running(s)}


def spawn(prompt: str, name: str, options: list[str], cwd: Path) -> tuple[str, str]:
    output = _run([prompt, "--bg", "--name", name, *options], cwd=cwd)
    bg_id = parse_bg_id(output)
    if bg_id is None:
        raise LedgerError(f"claude --bg printed no background id: {_strip_ansi(output).strip()!r}")
    deadline = time.monotonic() + _SPAWN_WAIT_S
    while True:
        entry = next((s for s in list_sessions() if s.get("id") == bg_id), None)
        if entry is not None and entry.get("sessionId"):
            return bg_id, str(entry["sessionId"])
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_S)
    try:
        stop(bg_id)
    except LedgerError:
        pass
    raise LedgerError(
        f"session {name} ({bg_id}) did not appear in claude agents --json "
        f"within {_SPAWN_WAIT_S:.0f}s, so it was stopped"
    )


def stop(bg_id: str) -> None:
    _run(["stop", bg_id])


def resume(session_id: str, message: str, cwd: Path | None = None) -> str | None:
    return parse_bg_id(_run(["--resume", session_id, "--bg", message], cwd=cwd))
