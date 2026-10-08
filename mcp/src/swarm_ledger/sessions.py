from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .identity import LedgerError

CLAUDE_VAR = "SENTINEL_SWARM_CLAUDE"
DEV_CHANNELS_VAR = "CLAUDE_DEV_CHANNELS"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BG_LINE = re.compile(r"backgrounded\s+\S+\s+([0-9A-Za-z-]+)")
_PLUGIN_LINE = re.compile(r"^\s*❯\s+(\S+)")
_STATUS_LINE = re.compile(r"^\s*Status:\s*(.+?)\s*$")
# A parent Claude Code session sets these for its own child processes. A session started
# with them, CLAUDE_CODE_CHILD_SESSION above all, never registers: `claude agents` does not
# list it and a send to its session id finds no live session.
PARENT_SESSION_VARS = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_JOB_DIR",
        "CLAUDE_EFFORT",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
    }
)
_LABELS = ("agents", "plugin", "stop", "--resume", "--bg")
# Not "done": a background session reports state "done" once its turn ends, while its process
# still runs and takes messages. Resuming it with --resume starts a second session.
_DEAD = frozenset({"stopped", "exited", "crashed", "failed", "killed", "dead", "completed"})
_TIMEOUT_S = 60.0
_SPAWN_WAIT_S = 15.0
_POLL_S = 0.5


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if key not in PARENT_SESSION_VARS}


def claude_binary() -> str:
    raw = os.environ.get(CLAUDE_VAR) or "claude"
    return shutil.which(raw) or raw


def dev_channel_args() -> list[str]:
    # Last on the command line: the flag takes several values and swallows anything after it.
    raw = [e for e in re.split(r"[\s,]+", os.environ.get(DEV_CHANNELS_VAR, "")) if e]
    entries = list(dict.fromkeys(raw))
    return ["--dangerously-load-development-channels", *entries] if entries else []


def _run(args: list[str], cwd: Path | None = None) -> str:
    binary = claude_binary()
    # Files, not pipes: a backgrounded session can inherit the handles and hold a pipe open.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            result = subprocess.run(
                [binary, *args],
                cwd=cwd,
                env=child_env(),
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


def plugin_load_problem(repo: Path, name: str = "sentinel-swarm") -> str | None:
    try:
        output = _strip_ansi(_run(["plugin", "list"], cwd=repo))
    except LedgerError as exc:
        return str(exc)
    statuses: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        plugin = _PLUGIN_LINE.match(line)
        if plugin is not None:
            current = plugin.group(1)
            continue
        status = _STATUS_LINE.match(line)
        if status is not None and current is not None and current.split("@")[0] == name:
            statuses.append(status.group(1))
    if any(status.endswith(" enabled") and "✔" in status for status in statuses):
        return None
    if not statuses:
        return f"claude plugin list shows no {name} plugin for this repo"
    return f"claude plugin list shows {name}: {'; '.join(statuses)}"


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
    # Claude Code 2.1.284 prints no pid for a background session; the plain listing holds only
    # active ones, so a background entry without a dead state is running.
    present = bool(entry.get("pid")) or entry.get("kind") == "background"
    return present and not (states & _DEAD)


def find_session(sessions: list[dict], session_id: str) -> dict | None:
    return next((s for s in sessions if s.get("sessionId") == session_id), None)


def is_live(session_id: str) -> bool:
    entry = find_session(list_sessions(), session_id)
    return entry is not None and is_running(entry)


def live_names(sessions: list[dict]) -> set[str]:
    return {str(s["name"]) for s in sessions if s.get("name") and is_running(s)}


def spawn(prompt: str, name: str, options: list[str], cwd: Path) -> tuple[str, str]:
    output = _run([prompt, "--bg", "--name", name, *options, *dev_channel_args()], cwd=cwd)
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


def resume(
    session_id: str,
    message: str,
    cwd: Path | None = None,
    name: str | None = None,
    options: Sequence[str] = (),
) -> str | None:
    # --resume restores the conversation only: the name and launch flags are passed again, or
    # the session comes back under a generated name without its MCP config.
    named = ["--name", name] if name else []
    args = ["--resume", session_id, "--bg", message, *named, *options, *dev_channel_args()]
    return parse_bg_id(_run(args, cwd=cwd))
