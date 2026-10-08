from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from .. import __version__, pricing, sessions
from ..agentfiles import plugin_installed
from ..db import ensure_git_exclude, write_tx
from ..identity import ROLES, LedgerError, caller_of
from ..ledger import Ledger
from ..watchdog import MONITOR_CALL, REGISTER_GRACE, WATCH_COMMAND, parse_stamp, utcnow

_CODEBASE_KG_PLUGIN = "codebase-kg@codebase-kg"

_RECORDS_DIR = ".sentinel-swarm"
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
# Must equal the synchronous post_any matcher in templates/agents/*.md: the Stop hook and
# handoff_submit read what post_any records for these tools, so it cannot run async.
SYNC_POST_TOOLS = ("SendMessage", "PushNotification", "Monitor", *_WRITE_TOOLS)
FIRED_AT_KEY = "sentinel_swarm_fired_at"
_READONLY_GIT = frozenset({"status", "diff", "log", "show", "ls-files", "branch"})
_POSIX = os.name != "nt"
_UNSTAMPED_TOOLS = frozenset({"ledger_info", "brief_get", "who_owns", "directive_submit", "events"})
_PRE_BIND_TOOLS = _UNSTAMPED_TOOLS | {"brief_ack"}
_RUN_STATUS_ROLES = frozenset({"manager", "lead"})
# Claude Code saves additionalContext over 10,000 characters to a file and shows the model
# only a 2,000-character preview, so the start calls must fit under the cap.
_START_CONTEXT_CAP = 9_500
# The Driver is the one role with an Agent tool, and only for cartographer's own
# subagents: it never runs a subagent of its own or any other plugin's.
_DRIVER_SUBAGENTS = frozenset(
    {"map-driver", "map-reviewer", "cartographer:map-driver", "cartographer:map-reviewer"}
)
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")
_SEARCH_TOOLS = frozenset({"Grep", "Glob"})
_GAP_PATHS = 20
_CONTENT_LINE = re.compile(r"^((?:[A-Za-z]:)?[^:]+):\d+[:-]")
_LIVE_RUN = "state IN ('active', 'paused')"
_PAUSE_HINT = "If the run is blocked on something only the user can fix, call run_pause(reason)."
_WATCH_STALE = timedelta(seconds=60)
_WATCH_RENEW = timedelta(seconds=30)
_ARM_WATCHDOG = (
    f"Arm the watchdog before you stop: {MONITOR_CALL}. Re-arm it with the same call "
    "whenever it expires."
)


# -- Shared helpers -----------------------------------------------------------


def _caller_id(data: dict) -> str | None:
    return data.get("agent_id") or data.get("session_id")


def _active_run_row(ledger: Ledger) -> dict | None:
    row = ledger.conn.execute(f"SELECT * FROM runs WHERE {_LIVE_RUN}").fetchone()
    return dict(row) if row is not None else None


def _swarm_caller(ledger: Ledger, caller_id: str | None) -> dict | None:
    if not caller_id:
        return None
    row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ? AND ended_at IS NULL", (caller_id,)
    ).fetchone()
    if row is None or row["role"] not in ROLES:
        return None
    return dict(row)


def _repo_relative(ledger: Ledger, raw: str) -> str | None:
    if not raw:
        return None
    path = Path(raw)
    try:
        if not path.is_absolute():
            path = ledger.repo_root / path
        rel = path.resolve().relative_to(ledger.repo_root.resolve())
    except (OSError, ValueError):
        return None
    return rel.as_posix()


def _oracle_run_id(ledger: Ledger, caller_id: str | None) -> int | None:
    if not caller_id:
        return None
    row = ledger.conn.execute(
        "SELECT run_id FROM agents WHERE agent_id = ? AND role = 'oracle'", (caller_id,)
    ).fetchone()
    return row["run_id"] if row is not None else None


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# -- 1. SessionStart ------------------------------------------------------------


def _session_context(text: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _start_calls(ledger: Ledger, caller: dict) -> list[tuple[str, Callable[[], object]]]:
    name, agent_id = caller["name"], caller["agent_id"]
    info = {
        "name": "swarm-ledger",
        "version": __version__,
        "status": "ready",
        "repo_root": str(ledger.repo_root),
    }
    calls: list[tuple[str, Callable[[], object]]] = [
        ("ledger_info()", lambda: info),
        (
            f"brief_get(caller_name={name!r}, child_name={name!r})",
            lambda: ledger.brief_get(name, name),
        ),
        ("guidelines_get()", lambda: ledger.guidelines_get(name, agent_id)),
    ]
    if caller["role"] in _RUN_STATUS_ROLES:
        calls.append(("run_status()", lambda: ledger.run_status(name, agent_id)))
    return calls


def _start_context(ledger: Ledger, caller: dict) -> str:
    name, agent_id = caller["name"], caller["agent_id"]
    names = ", ".join(label.split("(")[0] for label, _ in _start_calls(ledger, caller))
    if caller["state"] == "registered":
        try:
            ledger.brief_ack(name, agent_id)
        except LedgerError as exc:
            return (
                f"The SessionStart hook called brief_ack(caller={name!r}) for you, and the "
                f"ledger refused it: {exc}. No other ledger tool works until brief_ack "
                f"succeeds. Call brief_ack(caller={name!r}) yourself once the cause is fixed, "
                f"then {names}."
            )
        head = f"The SessionStart hook bound you to the ledger with brief_ack(caller={name!r})."
    else:
        head = f"You are {name}, already bound to the ledger. Do not call brief_ack again."
    parts = [
        f"{head} It also made your start calls ({names}); their results follow. Do not "
        "repeat them now. Call brief_get again whenever your template says to re-read "
        "your brief."
    ]
    size = len(parts[0])
    brief_in = False
    for label, call in _start_calls(ledger, caller):
        try:
            line = f"{label} returned: {_compact_json(call())}"
            answered = True
        except LedgerError as exc:
            line = f"{label} was refused: {exc}"
            answered = False
        if size + len(line) + 1 > _START_CONTEXT_CAP:
            line = (
                f"{label} is left out: its result is {len(line)} characters, over the room "
                "left in this hook's context. Call it yourself."
            )
        elif answered and label.startswith("brief_get"):
            brief_in = True
        parts.append(line)
        size += len(line) + 1
    if brief_in:
        ledger.brief_read(agent_id, name)
    return "\n".join(parts)


def handle_session_start(ledger: Ledger, data: dict) -> dict | None:
    caller = _swarm_caller(ledger, data.get("session_id"))
    if caller is not None:
        if data.get("transcript_path"):
            ledger.agent_transcript(caller["agent_id"], str(data["transcript_path"]))
        if caller["state"] == "idle":
            ledger.agent_active(caller["agent_id"], "session_start")
        if caller["role"] == "oracle":
            return None
        return _session_context(_start_context(ledger, caller))

    parts: list[str] = []

    run = _active_run_row(ledger)
    if run is not None:
        oracle = ledger.conn.execute(
            "SELECT name FROM agents WHERE run_id = ? AND role = 'oracle' "
            "ORDER BY started_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        oracle_name = oracle["name"] if oracle is not None else "oracle"
        if run["state"] == "paused":
            reason = ledger.pause_reason(run["run_id"])
            parts.append(
                f"A paused run exists (run_id {run['run_id']}, Oracle {oracle_name!r}), "
                f"paused because: {reason}. The resume skill continues it."
            )
        else:
            parts.append(
                f"An active run exists (run_id {run['run_id']}, Oracle {oracle_name!r}). "
                "The resume skill continues it."
            )

    if not plugin_installed(ledger.repo_root, _CODEBASE_KG_PLUGIN):
        parts.append(
            "codebase-kg is not installed for this repo; install it, then run "
            "/sentinel-swarm:setup."
        )
    elif not (ledger.repo_root / "knowledge" / "code_graph.db").is_file():
        parts.append("knowledge/code_graph.db is missing; codebase-kg has not mapped this repo.")

    git_dir = ledger.repo_root / ".git"
    if git_dir.exists():
        exclude_path = git_dir / "info" / "exclude"
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
        if f"{_RECORDS_DIR}/" not in existing.splitlines():
            ensure_git_exclude(ledger.repo_root)
            parts.append(f"Added {_RECORDS_DIR}/ to .git/info/exclude.")

    if not parts:
        return None
    return _session_context(" ".join(parts))


# -- 2. PreToolUse: Agent ---------------------------------------------------------


def handle_pre_agent(ledger: Ledger, data: dict) -> dict | None:
    caller = _swarm_caller(ledger, _caller_id(data))
    if caller is None:
        return None
    if caller["role"] == "driver":
        subagent_type = str((data.get("tool_input") or {}).get("subagent_type") or "")
        if subagent_type in _DRIVER_SUBAGENTS:
            return None
        return _deny(
            "the Driver's Agent tool is for cartographer's map-driver and map-reviewer "
            f"subagents only, not {subagent_type!r}"
        )
    return _deny(
        "roles start children with agent_spawn: write the brief with brief_create, "
        "then call agent_spawn(child_name)"
    )


# -- 3. PreToolUse: Write, Edit, MultiEdit, NotebookEdit ------------------------


def handle_pre_write(ledger: Ledger, data: dict) -> dict | None:
    if _active_run_row(ledger) is None:
        return None

    tool_input = data.get("tool_input") or {}
    raw_path = str(tool_input.get("file_path") or "")
    rel = _repo_relative(ledger, raw_path)

    if rel is not None and (rel == _RECORDS_DIR or rel.startswith(f"{_RECORDS_DIR}/")):
        return _deny("the records folder is written by the ledger only")

    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    target = rel if rel is not None else raw_path

    def _deny_or_override(reason: str) -> dict | None:
        if ledger.override_consume(caller["run_id"], "write", caller["name"], target):
            return None
        return _deny(reason)

    if caller["role"] != "coder":
        return _deny_or_override(
            "only a Coder writes project files; file a change request with the owner"
        )
    if rel is None:
        return _deny_or_override(f"{raw_path} is outside the repo root")

    claim = ledger.conn.execute(
        "SELECT path, test_path FROM files WHERE file_id = ? AND released_at IS NULL",
        (caller["file_id"],),
    ).fetchone()
    owned = {claim["path"], claim["test_path"]} if claim is not None else set()
    if rel not in owned:
        owner = ledger.who_owns(rel)["owner"] or "nobody"
        mine = (
            " and ".join(p for p in (claim["path"], claim["test_path"]) if p)
            if claim is not None
            else "nothing"
        )
        return _deny_or_override(f"{rel} is owned by {owner}; you own {mine}")

    return None


# -- 4. PreToolUse: Bash, PowerShell -----------------------------------------------


def _allowed_templates(ledger: Ledger, role: str) -> list[str]:
    if role == "driver":
        return [t for t in (ledger.settings.build_command,) if t]
    templates = (
        ledger.settings.test_command,
        ledger.settings.build_command,
        ledger.settings.lint_command,
    )
    return [t for t in templates if t]


def _command_allowed(ledger: Ledger, command: str, role: str) -> bool:
    if _SHELL_OPERATORS.search(command):
        return False
    try:
        argv = shlex.split(command, posix=_POSIX)
    except ValueError:
        return False
    if not argv:
        return False
    for template in _allowed_templates(ledger, role):
        prefix = shlex.split(template.replace("{target}", ""), posix=_POSIX)
        if prefix and argv[: len(prefix)] == prefix:
            return True
    return role == "coder" and argv[0] == "git" and len(argv) > 1 and argv[1] in _READONLY_GIT


def handle_pre_shell(ledger: Ledger, data: dict) -> dict | None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    command = str((data.get("tool_input") or {}).get("command") or "")

    def _deny_or_override(reason: str) -> dict | None:
        if ledger.override_consume(caller["run_id"], "shell", caller["name"], command):
            return None
        return _deny(reason)

    if caller["role"] not in ("coder", "driver"):
        return _deny_or_override("this role has no shell; use the ledger tools")
    if _command_allowed(ledger, command, caller["role"]):
        return None

    allowed = list(_allowed_templates(ledger, caller["role"]))
    if caller["role"] == "coder":
        allowed.append("read-only git (status, diff, log, show, ls-files, branch)")
    return _deny_or_override(f"command not allowed; allowed: {'; '.join(allowed)}")


def _role_of(ledger: Ledger, data: dict) -> str | None:
    caller = _swarm_caller(ledger, _caller_id(data))
    if caller is not None:
        return caller["role"]
    agent_type = str(data.get("agent_type") or "")
    role = agent_type.removeprefix("swarm-")
    return role if agent_type.startswith("swarm-") and role in ROLES else None


def handle_pre_monitor(ledger: Ledger, data: dict) -> dict | None:
    role = _role_of(ledger, data)
    if role is None:
        return None
    tool_input = data.get("tool_input") or {}
    command = " ".join(str(tool_input.get("command") or "").split())
    if role == "oracle" and tool_input.get("ws") is None and command == WATCH_COMMAND:
        return None
    return _deny(
        "a swarm role runs no Monitor of its own. The one Monitor call allowed is the "
        f"Oracle's watchdog: {MONITOR_CALL}"
    )


# -- 4b. PreToolUse: SendMessage ----------------------------------------------------


def handle_pre_send_message(ledger: Ledger, data: dict) -> dict | None:
    caller = _swarm_caller(ledger, _caller_id(data))
    if caller is None or caller["run_id"] is None:
        return None
    to = str((data.get("tool_input") or {}).get("to") or "")
    names = {
        row["session_name"]
        for row in ledger.conn.execute(
            "SELECT DISTINCT session_name FROM agents WHERE run_id = ? "
            "AND session_name IS NOT NULL",
            (caller["run_id"],),
        )
    }
    if to not in names:
        return _deny(
            "SendMessage may target only a session of this run; valid session names: "
            f"{sorted(names)}"
        )
    allowed = {
        row["session_name"]
        for row in ledger.message_peers(ledger.conn, caller_of(caller))
        if row["session_name"]
    }
    allowed |= {
        row["to_session_name"]
        for row in ledger.conn.execute(
            "SELECT to_session_name FROM wakeups WHERE from_agent_id = ? AND sent_at IS NULL",
            (caller["agent_id"],),
        )
    }
    if caller["role"] == "oracle":
        # The Oracle's Stop hook names any waiting agent of a stalled run to wake.
        allowed |= {
            row["session_name"]
            for row in ledger.conn.execute(
                "SELECT session_name FROM agents WHERE run_id = ? AND ended_at IS NULL "
                "AND state != 'working' AND session_name IS NOT NULL",
                (caller["run_id"],),
            )
        }
    if to in allowed:
        return None
    return _deny(
        f"SendMessage from {caller['name']} goes to its parent, its children, its siblings, or "
        f"a session it owes a wake-up: {sorted(allowed)}. Reach anyone else through that chain"
    )


# -- 4c. PreToolUse: Skill -----------------------------------------------------------


def handle_pre_skill(ledger: Ledger, data: dict) -> dict | None:
    caller = _swarm_caller(ledger, _caller_id(data))
    if caller is None or caller["role"] != "driver":
        return None
    tool_input = data.get("tool_input") or {}
    skill = str(tool_input.get("skill") or tool_input.get("command") or "")
    reason = ledger.drive_skill(caller["agent_id"], skill)
    return _deny(reason) if reason is not None else None


# -- 5. PreToolUse: the swarm-ledger MCP tools -------------------------------------


def handle_pre_ledger(ledger: Ledger, data: dict) -> dict:
    tool_name = str(data.get("tool_name") or "")
    method = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
    caller_id = _caller_id(data)

    caller = _swarm_caller(ledger, caller_id)
    if method == "override_grant" and (caller is None or caller["role"] != "oracle"):
        return _deny("override_grant is for the Oracle only")
    if caller is not None and caller["state"] == "registered" and method not in _PRE_BIND_TOOLS:
        return _deny(
            f"you are not bound to the ledger yet: call brief_ack(caller={caller['name']!r}). "
            "No other ledger tool works until it succeeds"
        )

    tool_input = dict(data.get("tool_input") or {})
    if method == "brief_get" and caller_id:
        ledger.brief_read(caller_id, str(tool_input.get("child_name") or ""))
    if method in _UNSTAMPED_TOOLS:
        tool_input.pop("agent_id", None)
    else:
        tool_input["agent_id"] = caller_id
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": tool_input,
        }
    }


# -- 6. PostToolUse: post_any and post_activity ----------------------------------------------------


def _idle_since(ledger: Ledger, agent_id: str, fired_at: datetime | None) -> bool:
    if fired_at is None:
        return False
    row = ledger.conn.execute(
        "SELECT MAX(at) AS at FROM agent_events WHERE agent_id = ? AND to_state = 'idle'",
        (agent_id,),
    ).fetchone()
    idle_at = parse_stamp(row["at"]) if row is not None else None
    return idle_at is not None and idle_at >= fired_at


def _record_activity(
    ledger: Ledger, caller: dict, data: dict, tool_name: str, fired_at: datetime | None = None
) -> None:
    ledger.agent_heartbeat(caller["agent_id"], tool_name)
    # An async post_activity can land after the Stop hook set the agent idle; the tool call it
    # reports then came before the stop and must not wake the agent.
    if caller["state"] == "idle" and not _idle_since(ledger, caller["agent_id"], fired_at):
        ledger.agent_active(caller["agent_id"], "post_tool_use")
    if data.get("transcript_path") and not caller["transcript_path"]:
        ledger.agent_transcript(caller["agent_id"], str(data["transcript_path"]))


def handle_post_any(ledger: Ledger, data: dict) -> None:
    caller_id = _caller_id(data)
    if data.get("tool_name") == "PushNotification":
        run_id = _oracle_run_id(ledger, caller_id)
        if run_id is not None:
            from .. import notify

            message = str((data.get("tool_input") or {}).get("message") or "")
            notify.mark_sent(ledger.conn, run_id, message)
    if _active_run_row(ledger) is None:
        return None
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    tool_name = str(data.get("tool_name") or "")
    _record_activity(ledger, caller, data, tool_name)

    if tool_name == "Monitor" and caller["role"] == "oracle":
        tool_input = data.get("tool_input") or {}
        if " ".join(str(tool_input.get("command") or "").split()) == WATCH_COMMAND:
            timeout_ms = tool_input.get("timeout_ms")
            ledger.watch_armed(timeout_ms if isinstance(timeout_ms, int) else None)

    if tool_name == "SendMessage":
        to = str((data.get("tool_input") or {}).get("to") or "")
        if to:
            ledger.wakeups_sent(caller["agent_id"], to)

    if tool_name in _WRITE_TOOLS and caller["role"] == "coder":
        raw_path = str((data.get("tool_input") or {}).get("file_path") or "")
        rel = _repo_relative(ledger, raw_path)
        if rel is not None:
            ledger.mark_stale(caller["agent_id"], rel)
    return None


def handle_post_activity(ledger: Ledger, data: dict) -> None:
    tool_name = str(data.get("tool_name") or "")
    if tool_name in SYNC_POST_TOOLS or _active_run_row(ledger) is None:
        return None
    caller = _swarm_caller(ledger, _caller_id(data))
    if caller is None:
        return None
    _record_activity(ledger, caller, data, tool_name, parse_stamp(data.get(FIRED_AT_KEY)))
    if tool_name in _SEARCH_TOOLS:
        tool_input = data.get("tool_input") or {}
        raw_path = str(tool_input.get("path") or "")
        ledger.graph_gap(
            caller["agent_id"],
            tool_name,
            str(tool_input.get("pattern") or "") or None,
            (_repo_relative(ledger, raw_path) or raw_path) if raw_path else None,
            _found_paths(ledger, data.get("tool_response")),
        )
    return None


def _found_paths(ledger: Ledger, response: object) -> list[str]:
    if isinstance(response, dict):
        filenames = response.get("filenames")
        if isinstance(filenames, list):
            raw = [str(name) for name in filenames]
        else:
            content = response.get("content")
            raw = _content_paths(content) if isinstance(content, str) else []
    elif isinstance(response, str):
        raw = _content_paths(response)
    else:
        raw = []
    found: list[str] = []
    for name in raw:
        path = _repo_relative(ledger, name) or name
        if path not in found:
            found.append(path)
        if len(found) == _GAP_PATHS:
            break
    return found


def _content_paths(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        match = _CONTENT_LINE.match(line)
        paths.append(match.group(1) if match else line.strip())
    return [p for p in paths if p]


# -- 7. PostToolUse: Bash, PowerShell ----------------------------------------------


def _parse_porcelain(output: str) -> list[str]:
    paths = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        rest = line[3:].strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1].strip()
        paths.append(rest.strip('"'))
    return paths


def handle_post_shell(ledger: Ledger, data: dict) -> None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None or caller["role"] != "coder":
        return None

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ledger.repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    changed = _parse_porcelain(result.stdout)
    if not changed:
        return None

    claim = ledger.conn.execute(
        "SELECT path, test_path FROM files WHERE file_id = ? AND released_at IS NULL",
        (caller["file_id"],),
    ).fetchone()
    owned = {claim["path"], claim["test_path"]} if claim is not None else set()
    others = {
        p
        for row in ledger.conn.execute(
            "SELECT f.path, f.test_path FROM files f JOIN modules m ON m.module_id = f.module_id "
            "JOIN phases ph ON ph.phase_id = m.phase_id WHERE ph.run_id = ?",
            (caller["run_id"],),
        )
        for p in (row["path"], row["test_path"])
        if p
    }

    violations = [
        p
        for p in changed
        if p not in owned
        and p not in others
        and not p.startswith(f"{_RECORDS_DIR}/")
        and not p.startswith("knowledge/")
    ]
    if not violations:
        return None

    reason = f"changed files outside its claim: {', '.join(violations)}"
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES (?, ?, 'violation', ?)",
            (caller["agent_id"], caller["state"], reason),
        )

    parent = ledger.conn.execute(
        "SELECT name FROM agents WHERE agent_id = ?", (caller["parent_agent_id"],)
    ).fetchone()
    if parent is not None:
        ledger.message_post(
            caller["name"], caller["agent_id"], parent["name"], f"Violation: {reason}"
        )
    return None


# -- 8. PreCompact ------------------------------------------------------------------


def handle_pre_compact(ledger: Ledger, data: dict) -> None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None
    ledger.agent_compacted(caller["agent_id"])
    return None


# -- 9. Stop -----------------------------------------------------------------------


def _sum_tokens(transcript_path: str | None) -> dict | None:
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return None
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "tool_uses": 0,
        }
        responses: dict[object, tuple[str | None, dict]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                message = record.get("message") or {}
                usage = message.get("usage")
                if usage:
                    # One response is written as one line per content block, each repeating
                    # its usage, so only the last line of each response id counts.
                    key = message.get("id") or record.get("uuid") or number
                    responses[key] = (message.get("model"), usage)
                content = message.get("content")
                if isinstance(content, list):
                    totals["tool_uses"] += sum(
                        1
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )
        cost: float | None = 0.0
        for model, usage in responses.values():
            totals["input_tokens"] += usage.get("input_tokens") or 0
            totals["output_tokens"] += usage.get("output_tokens") or 0
            totals["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
            totals["cache_write_tokens"] += usage.get("cache_creation_input_tokens") or 0
            priced = pricing.response_cost(model, usage)
            if priced is None and not any(
                usage.get(k)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            ):
                continue
            cost = None if cost is None or priced is None else cost + priced
        return totals | {"cost_usd": cost}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def _block_coder_stop_once(ledger: Ledger, caller: dict, data: dict) -> dict | None:
    if caller["role"] != "coder" or data.get("stop_hook_active"):
        return None

    already_blocked = ledger.conn.execute(
        "SELECT 1 FROM agent_events WHERE agent_id = ? AND to_state = 'stop_blocked'",
        (caller["agent_id"],),
    ).fetchone()
    if already_blocked is not None:
        return None

    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agent_events (agent_id, from_state, to_state, reason) "
            "VALUES (?, ?, 'stop_blocked', 'stop')",
            (caller["agent_id"], caller["state"]),
        )
    lead = ledger.conn.execute(
        "SELECT name FROM agents WHERE agent_id = ?", (caller["parent_agent_id"],)
    ).fetchone()
    lead_name = json.dumps(lead["name"] if lead is not None else "<your Lead>")
    return {
        "decision": "block",
        "reason": (
            "Your file has no handoff on record. Call handoff_submit. If a blocker stops you, "
            f"call message_post(to_name={lead_name}, body=<the blocker>) and make the "
            "SendMessage call its next field names, then stop."
        ),
    }


def _live_session_ids() -> set[str] | None:
    try:
        return {
            str(entry.get("sessionId"))
            for entry in sessions.list_sessions()
            if sessions.is_running(entry)
        }
    except LedgerError:
        return None


def _wake_hint(ledger: Ledger, agent: dict, live_ids: set[str] | None) -> str:
    from .. import wake

    delivery = wake.route_wakeup(
        {
            "to_agent_id": agent["agent_id"],
            "to_name": agent["name"],
            "to_session_name": agent["session_name"],
        },
        transport=ledger.settings.wake_transport,
        live=None if live_ids is None else agent["agent_id"] in live_ids,
        channel=agent.get("channel") or "none",
        hub=ledger.hub,
    )
    if delivery.send is None:
        return f"resume it with {delivery.resume}"
    if delivery.kind == "resume":
        return f"its session is not running; resume it with {delivery.resume}"
    return f"wake it with {delivery.next}"


def _wake_lines(ledger: Ledger, run_id: int, live: list[dict]) -> list[str]:
    by_id = {a["agent_id"]: a for a in live}
    waiting = {a["agent_id"]: a for a in live if a["state"] != "working"}
    live_ids = _live_session_ids()
    lines: list[str] = []
    named: set[str] = set()

    handoffs = ledger.conn.execute(
        "SELECT h.handoff_id, f.path, coder.parent_agent_id AS reviewer_id FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "LEFT JOIN agents coder ON coder.agent_id = h.agent_id "
        "WHERE p.run_id = ? AND h.state = 'submitted' ORDER BY h.handoff_id",
        (run_id,),
    ).fetchall()
    for handoff in handoffs:
        reviewer = waiting.get(handoff["reviewer_id"])
        if reviewer is None:
            continue
        lines.append(
            f"handoff {handoff['handoff_id']} for {handoff['path']} waits on "
            f"{reviewer['name']}, which is {reviewer['state']}; "
            f"{_wake_hint(ledger, reviewer, live_ids)}"
        )
        named.add(reviewer["agent_id"])

    for agent in waiting.values():
        unread = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE run_id = ? AND to_name = ? "
            "AND read_at IS NULL",
            (run_id, agent["name"]),
        ).fetchone()["n"]
        if unread:
            lines.append(
                f"{agent['name']} has {unread} unread message(s); "
                f"{_wake_hint(ledger, agent, live_ids)}"
            )
            named.add(agent["agent_id"])

    open_crs = ledger.conn.execute(
        "SELECT cr_id, path, to_agent_id FROM change_requests WHERE run_id = ? AND state = 'open'",
        (run_id,),
    ).fetchall()
    for cr in open_crs:
        recipient = waiting.get(cr["to_agent_id"])
        if recipient is None or recipient["state"] != "idle":
            continue
        lines.append(
            f"change request {cr['cr_id']} for {cr['path']} waits on {recipient['name']}, "
            f"which is idle; {_wake_hint(ledger, recipient, live_ids)}"
        )
        named.add(recipient["agent_id"])

    completed_crs = ledger.conn.execute(
        "SELECT cr_id, path, from_agent_id FROM change_requests WHERE run_id = ? "
        "AND state = 'completed'",
        (run_id,),
    ).fetchall()
    for cr in completed_crs:
        verifier = ledger.nearest_live_agent(ledger.conn, cr["from_agent_id"])
        if verifier is None:
            continue
        agent = waiting.get(verifier["agent_id"])
        if agent is None or agent["state"] != "idle":
            continue
        lines.append(
            f"change request {cr['cr_id']} for {cr['path']} is completed and waits on "
            f"{agent['name']} to verify it, which is idle; {_wake_hint(ledger, agent, live_ids)}"
        )
        named.add(agent["agent_id"])

    def ancestors(agent_id: str) -> set[str]:
        found: set[str] = set()
        parent_id = by_id[agent_id]["parent_agent_id"]
        while parent_id in by_id and parent_id not in found:
            found.add(parent_id)
            parent_id = by_id[parent_id]["parent_agent_id"]
        return found

    covered: set[str] = set()
    for agent_id in named:
        covered |= ancestors(agent_id)
    idle = [a for a in waiting.values() if a["state"] == "idle" and a["agent_id"] not in named]
    for agent in sorted(idle, key=lambda a: len(ancestors(a["agent_id"])), reverse=True):
        if agent["agent_id"] in covered:
            continue
        lines.append(
            f"{agent['name']} is idle and none of its children is working; "
            f"{_wake_hint(ledger, agent, live_ids)}"
        )
        covered |= ancestors(agent["agent_id"])
    return lines


def _push_reason(ledger: Ledger, run_id: int | None) -> str | None:
    from .. import notify

    owed = notify.owed(ledger.conn, run_id) if run_id is not None else []
    if not owed:
        return None
    return "\n".join(
        [
            "You owe the user a notification. Make each call below, then stop. A result that "
            "says the notification was not sent still counts; do not repeat the call:",
            *(f"- {notify.push_call(n['message'])}" for n in owed),
        ]
    )


def _block(*reasons: str | None) -> dict | None:
    lines = [reason for reason in reasons if reason]
    return {"decision": "block", "reason": "\n".join(lines)} if lines else None


def handle_stop(ledger: Ledger, data: dict) -> dict | None:
    run = _active_run_row(ledger)
    caller_id = _caller_id(data)
    if run is None:
        if data.get("stop_hook_active"):
            return None
        _refresh_finished_report(ledger, caller_id, data.get("transcript_path"))
        return _block(_push_reason(ledger, _oracle_run_id(ledger, caller_id)))

    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None
    if caller["role"] != "oracle":
        return _member_stop(ledger, caller, data)
    if data.get("stop_hook_active"):
        return None

    oracle = ledger.conn.execute(
        "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (run["run_id"],),
    ).fetchone()
    if oracle is None or oracle["agent_id"] != caller_id:
        return None

    transcript_path = data.get("transcript_path")
    if transcript_path:
        ledger.agent_stop(
            oracle["agent_id"],
            transcript_path=transcript_path,
            tokens=_sum_tokens(transcript_path),
        )

    push = _push_reason(ledger, run["run_id"])
    if run["state"] == "paused":
        return _block(push)

    blocked = _oracle_work_block(ledger, run, dict(oracle))
    return _block(
        push,
        blocked["reason"] if blocked is not None else None,
        _ARM_WATCHDOG if _watch_unarmed(run) else None,
    )


def _watch_unarmed(run: dict) -> bool:
    now = utcnow()
    heartbeat = parse_stamp(run.get("watch_heartbeat_at"))
    if heartbeat is None or now - heartbeat > _WATCH_STALE:
        return True
    expires = parse_stamp(run.get("watch_expires_at"))
    return expires is not None and expires - now <= _WATCH_RENEW


def _child_starting(live: list[dict]) -> bool:
    now = utcnow()
    recent = [
        a
        for a in live
        if a["state"] == "registered"
        and (started := parse_stamp(a.get("started_at"))) is not None
        and now - started <= REGISTER_GRACE
    ]
    if not recent:
        return False
    live_ids = _live_session_ids()
    return live_ids is None or any(a["agent_id"] in live_ids for a in recent)


def _oracle_work_block(ledger: Ledger, run: dict, oracle: dict) -> dict | None:
    needs_user = ledger.conn.execute(
        "SELECT 1 FROM directives WHERE run_id = ? AND state = 'open' AND outcome = 'needs_user'",
        (run["run_id"],),
    ).fetchone()
    if needs_user is not None:
        return None

    live = [
        dict(row)
        for row in ledger.conn.execute(
            "SELECT * FROM agents WHERE run_id = ? AND ended_at IS NULL AND state != 'released' "
            "AND role IN ('manager', 'lead', 'coder') ORDER BY started_at",
            (run["run_id"],),
        )
    ]
    if any(a["state"] == "working" for a in live) or _child_starting(live):
        return None

    plan_unlocked = ledger.plan_unlocked(oracle["name"], oracle["agent_id"])
    submitted_handoffs = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND h.state = 'submitted' AND p.paused_at IS NULL",
        (run["run_id"],),
    ).fetchone()["n"]
    live_claims = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM files f "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND f.released_at IS NULL AND p.paused_at IS NULL",
        (run["run_id"],),
    ).fetchone()["n"]
    pending_phase_reviews = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM phases p WHERE p.run_id = ? AND p.state = 'handed_up' "
        "AND p.paused_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.phase_id = p.phase_id "
        "AND r.kind = 'oracle' AND r.outcome = 'accepted' AND r.created_at >= p.handed_up_at)",
        (run["run_id"],),
    ).fetchone()["n"]

    reasons = []
    if plan_unlocked:
        reasons.append(f"{len(plan_unlocked)} unlocked phase(s)")
    if submitted_handoffs:
        reasons.append(f"{submitted_handoffs} handoff(s) awaiting review")
    if live_claims:
        reasons.append(f"{live_claims} live file claim(s)")
    if pending_phase_reviews:
        reasons.append(f"{pending_phase_reviews} handed-up phase(s) awaiting an Oracle review")
    if live:
        reasons.append(f"{len(live)} live agent(s), none working")
    if not reasons:
        return None

    lines = _wake_lines(ledger, run["run_id"], live)
    if lines:
        lines.insert(0, "No agent is working, so no child will wake you.")
    elif live:
        live_ids = _live_session_ids()
        listed = "; ".join(
            f"{a['name']} ({a['state']}): {_wake_hint(ledger, a, live_ids)}" for a in live
        )
        lines = [f"Live agents: {listed}. Wake the one with pending work, or call run_pause."]
    else:
        lines = ["Continue the plan, or call run_finish."]

    return {
        "decision": "block",
        "reason": "\n".join(
            [f"The run still has work: {', '.join(reasons)}.", *lines, _PAUSE_HINT]
        ),
    }


def _owed_steps(ledger: Ledger, agent_id: str) -> list[str]:
    from .. import wake

    owed = ledger.owed_wakeups(agent_id)
    pushed = [w for w in owed if w["pushed_at"] is not None]
    unconfirmed = {w["wakeup_id"] for w in wake.await_confirmation(ledger.conn, pushed)}
    return [
        ledger.fallback_step(w) if w["pushed_at"] is not None else ledger.wake_step(w)
        for w in owed
        if w["pushed_at"] is None or w["wakeup_id"] in unconfirmed
    ]


def _unread_count(ledger: Ledger, caller: dict) -> int:
    return ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE run_id = ? AND to_name = ? AND read_at IS NULL",
        (caller["run_id"], caller["name"]),
    ).fetchone()["n"]


def _member_stop(ledger: Ledger, caller: dict, data: dict) -> dict | None:
    transcript_path = data.get("transcript_path")
    ledger.agent_stop(
        caller["agent_id"], transcript_path=transcript_path, tokens=_sum_tokens(transcript_path)
    )

    if not data.get("stop_hook_active"):
        steps = [f"- {step}" for step in _owed_steps(ledger, caller["agent_id"])]
        unread = _unread_count(ledger, caller)
        if steps or unread:
            lines = []
            if steps:
                lines += ["You still owe a wake-up. Make each call below, then stop:", *steps]
            if unread:
                lines.append(
                    f"You have {unread} unread message(s). Call message_inbox, act on them, "
                    "then stop."
                )
            return {"decision": "block", "reason": "\n".join(lines)}

    if caller["role"] == "driver" and ledger.release_closed_driver(caller["agent_id"]):
        return None
    if caller["state"] != "working":
        return None
    blocked = _block_coder_stop_once(ledger, caller, data)
    if blocked is not None:
        return blocked
    ledger.agent_idle(caller["agent_id"], "stop")
    return None


# -- 10. SessionEnd ----------------------------------------------------------------


def handle_session_end(ledger: Ledger, data: dict) -> None:
    session_id = data.get("session_id")
    if session_id:
        transcript_path = data.get("transcript_path")
        ledger.agent_stop(
            session_id,
            transcript_path=transcript_path,
            tokens=_sum_tokens(transcript_path),
            end_reason=data.get("reason"),
        )
        if _active_run_row(ledger) is None:
            _refresh_finished_report(ledger, session_id, None)
    return None


def _refresh_finished_report(
    ledger: Ledger, caller_id: str | None, transcript_path: str | None
) -> None:
    run = ledger.conn.execute(
        "SELECT * FROM runs WHERE state = 'finished' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if run is None or not caller_id:
        return
    oracle = ledger.conn.execute(
        "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' AND agent_id = ?",
        (run["run_id"], caller_id),
    ).fetchone()
    if oracle is None:
        return
    if transcript_path:
        ledger.agent_stop(
            caller_id, transcript_path=transcript_path, tokens=_sum_tokens(transcript_path)
        )
    ledger.write_report(run["run_id"])
