from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from . import terminal
from .identity import LedgerError

CLAUDE_VAR = "SENTINEL_SWARM_CLAUDE"
DEV_CHANNELS_VAR = "CLAUDE_DEV_CHANNELS"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BG_LINE = re.compile(r"backgrounded\s+\S+\s+([0-9A-Za-z-]+)")
_LABELS = ("agents", "stop", "--resume", "--bg")
# Not "done": a background session reports state "done" once its turn ends, while its process
# still runs and takes messages. Resuming it with --resume starts a second session.
_DEAD = frozenset({"stopped", "exited", "crashed", "failed", "killed", "dead", "completed"})
_TIMEOUT_S = 60.0
_SPAWN_WAIT_S = 15.0
_POLL_S = 0.5


def claude_binary() -> str:
    raw = os.environ.get(CLAUDE_VAR) or "claude"
    return shutil.which(raw) or raw


def dev_channel_args() -> list[str]:
    # Last on the command line: the flag takes several values and swallows anything after it.
    entries = [e for e in re.split(r"[\s,]+", os.environ.get(DEV_CHANNELS_VAR, "")) if e]
    return ["--dangerously-load-development-channels", *entries] if entries else []


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


def _open(args: list[str], cwd: Path, title: str) -> None:
    terminal.open_session(cwd, title, [claude_binary(), *args])


def spawn(prompt: str, name: str, options: list[str], cwd: Path) -> tuple[str | None, str]:
    # Interactive, not --bg: Claude Code reads --dangerously-load-development-channels only in
    # an interactive session, so a --bg role could never receive a channel event.
    _open([prompt, "--name", name, *options, *dev_channel_args()], cwd, name)
    deadline = time.monotonic() + _SPAWN_WAIT_S
    while True:
        entry = next((s for s in list_sessions() if s.get("name") == name), None)
        if entry is not None and entry.get("sessionId"):
            bg_id = entry.get("id")
            return (str(bg_id) if bg_id else None), str(entry["sessionId"])
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_S)
    raise LedgerError(
        f"session {name} did not appear in claude agents --json within {_SPAWN_WAIT_S:.0f}s; "
        "check its terminal tab"
    )


def stop(bg_id: str) -> None:
    _run(["stop", bg_id])


def _kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.kill(pid, signal.SIGTERM)


def stop_session(session_id: str) -> bool:
    entry = find_session(list_sessions(), session_id)
    if entry is None or not is_running(entry):
        return False
    if entry.get("id"):
        stop(str(entry["id"]))
    else:
        _kill(int(entry["pid"]))
    return True


def resume(
    session_id: str,
    message: str,
    cwd: Path,
    name: str | None = None,
    options: Sequence[str] = (),
) -> None:
    # --resume restores the conversation only: the name and launch flags are passed again, or
    # the session comes back under a generated name without its MCP config.
    named = ["--name", name] if name else []
    args = ["--resume", session_id, message, *named, *options, *dev_channel_args()]
    _open(args, cwd, name or session_id[:8])
