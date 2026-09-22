from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from ..db import ensure_git_exclude, write_tx
from ..identity import ROLES, child_role_of
from ..ledger import Ledger

_RECORDS_DIR = ".sentinel-swarm"
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_READONLY_GIT = frozenset({"status", "diff", "log", "show", "ls-files", "branch"})
_POSIX = os.name != "nt"
_UNSTAMPED_TOOLS = frozenset({"ledger_info", "brief_get", "who_owns", "directive_submit", "events"})
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")


# -- Shared helpers -----------------------------------------------------------


def _caller_id(data: dict) -> str | None:
    return data.get("agent_id") or data.get("session_id")


def _active_run_row(ledger: Ledger) -> dict | None:
    row = ledger.conn.execute("SELECT * FROM runs WHERE state = 'active'").fetchone()
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


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# -- 1. SessionStart ------------------------------------------------------------


def handle_session_start(ledger: Ledger, data: dict) -> dict | None:
    del data
    parts: list[str] = []

    run = ledger.conn.execute("SELECT * FROM runs WHERE state = 'active'").fetchone()
    if run is not None:
        oracle = ledger.conn.execute(
            "SELECT name FROM agents WHERE run_id = ? AND role = 'oracle' "
            "ORDER BY started_at DESC LIMIT 1",
            (run["run_id"],),
        ).fetchone()
        oracle_name = oracle["name"] if oracle is not None else "oracle"
        parts.append(
            f"An active run exists (run_id {run['run_id']}, Oracle {oracle_name!r}). "
            "The resume skill continues it."
        )

    if not (ledger.repo_root / "knowledge" / "code_graph.db").is_file():
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
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": " ".join(parts),
        }
    }


# -- 2. PreToolUse: Agent ---------------------------------------------------------


def handle_pre_agent(ledger: Ledger, data: dict) -> dict | None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    tool_input = data.get("tool_input") or {}
    raw_subagent_type = str(tool_input.get("subagent_type") or "")
    requested_role = raw_subagent_type.rsplit(":", 1)[-1]
    expected_child = child_role_of(caller["role"])

    def _deny_or_override(reason: str) -> dict | None:
        if ledger.override_consume("spawn", caller["name"], raw_subagent_type):
            return None
        return _deny(reason)

    if expected_child is None:
        return _deny_or_override(f"role {caller['role']!r} has no child role to spawn")
    if requested_role != expected_child:
        return _deny_or_override(
            f"{caller['name']!r} ({caller['role']}) may spawn only a {expected_child!r}; "
            f"got {raw_subagent_type!r}"
        )

    model = tool_input.get("model")
    if model is not None:
        approved = ledger.settings.models.get(expected_child, [])
        if model not in approved:
            return _deny_or_override(
                f"model {model!r} is not approved for {expected_child!r}; approved: {approved}"
            )

    unacked = ledger.conn.execute(
        "SELECT 1 FROM briefs WHERE parent_agent_id = ? AND child_role = ? AND acked_at IS NULL",
        (caller_id, expected_child),
    ).fetchone()
    if unacked is None:
        return _deny_or_override(
            f"no unacked brief for a {expected_child!r}; call brief_create first"
        )

    cap = ledger.settings.parallelism_cap
    if cap is not None:
        live = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE run_id = ? AND ended_at IS NULL",
            (caller["run_id"],),
        ).fetchone()["n"]
        if live >= cap:
            return _deny_or_override(
                f"parallelism cap of {cap} reached ({live} live agents); wait for one to finish"
            )

    return None


# -- 3. SubagentStart -------------------------------------------------------------


def handle_subagent_start(ledger: Ledger, data: dict) -> None:
    agent_id = data.get("agent_id") or data.get("session_id")
    if agent_id:
        ledger.agent_register_start(agent_id, str(data.get("agent_type") or ""))
    return None


# -- 4. PreToolUse: Write, Edit, MultiEdit, NotebookEdit ------------------------


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
        if ledger.override_consume("write", caller["name"], target):
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


# -- 5. PreToolUse: Bash, PowerShell -----------------------------------------------


def _command_allowed(ledger: Ledger, command: str) -> bool:
    if _SHELL_OPERATORS.search(command):
        return False
    try:
        argv = shlex.split(command, posix=_POSIX)
    except ValueError:
        return False
    if not argv:
        return False
    for template in (
        ledger.settings.test_command,
        ledger.settings.build_command,
        ledger.settings.lint_command,
    ):
        if not template:
            continue
        prefix = shlex.split(template.replace("{target}", ""), posix=_POSIX)
        if prefix and argv[: len(prefix)] == prefix:
            return True
    return argv[0] == "git" and len(argv) > 1 and argv[1] in _READONLY_GIT


def handle_pre_shell(ledger: Ledger, data: dict) -> dict | None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    command = str((data.get("tool_input") or {}).get("command") or "")

    def _deny_or_override(reason: str) -> dict | None:
        if ledger.override_consume("shell", caller["name"], command):
            return None
        return _deny(reason)

    if caller["role"] != "coder":
        return _deny_or_override("this role has no shell; use the ledger tools")
    if _command_allowed(ledger, command):
        return None

    allowed = [
        t
        for t in (
            ledger.settings.test_command,
            ledger.settings.build_command,
            ledger.settings.lint_command,
        )
        if t
    ]
    allowed.append("read-only git (status, diff, log, show, ls-files, branch)")
    return _deny_or_override(f"command not allowed; allowed: {'; '.join(allowed)}")


# -- 6. PreToolUse: the swarm-ledger MCP tools -------------------------------------


def handle_pre_ledger(ledger: Ledger, data: dict) -> dict:
    tool_name = str(data.get("tool_name") or "")
    method = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
    caller_id = _caller_id(data)

    if method == "override_grant":
        caller = _swarm_caller(ledger, caller_id)
        if caller is None or caller["role"] != "oracle":
            return _deny("override_grant is for the Oracle only")

    tool_input = dict(data.get("tool_input") or {})
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


# -- 7. PostToolUse: every tool ----------------------------------------------------


def handle_post_any(ledger: Ledger, data: dict) -> None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    tool_name = str(data.get("tool_name") or "")
    ledger.agent_heartbeat(caller["agent_id"], tool_name)

    if tool_name in _WRITE_TOOLS and caller["role"] == "coder":
        raw_path = str((data.get("tool_input") or {}).get("file_path") or "")
        rel = _repo_relative(ledger, raw_path)
        if rel is not None:
            ledger.mark_stale(caller["agent_id"], rel)
    return None


# -- 8. PostToolUse: Bash, PowerShell ----------------------------------------------


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
            ["git", "status", "--porcelain"],
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

    violations = [
        p
        for p in changed
        if p not in owned
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


# -- 9. PreCompact ------------------------------------------------------------------


def handle_pre_compact(ledger: Ledger, data: dict) -> None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None
    ledger.agent_compacted(caller["agent_id"])
    return None


# -- 10. SubagentStop ---------------------------------------------------------------


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
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                message = record.get("message") or {}
                usage = message.get("usage")
                if usage:
                    totals["input_tokens"] += usage.get("input_tokens") or 0
                    totals["output_tokens"] += usage.get("output_tokens") or 0
                    totals["cache_read_tokens"] += usage.get("cache_read_input_tokens") or 0
                    totals["cache_write_tokens"] += usage.get("cache_creation_input_tokens") or 0
                content = message.get("content")
                if isinstance(content, list):
                    totals["tool_uses"] += sum(
                        1
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )
        return totals
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def handle_subagent_stop(ledger: Ledger, data: dict) -> dict | None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    transcript_path = data.get("agent_transcript_path")
    tokens = _sum_tokens(transcript_path)
    ledger.agent_stop(caller["agent_id"], transcript_path=transcript_path, tokens=tokens)

    if caller["role"] != "coder" or caller["state"] != "working" or data.get("stop_hook_active"):
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
            "VALUES (?, ?, 'stop_blocked', 'subagent_stop')",
            (caller["agent_id"], caller["state"]),
        )
    return {
        "decision": "block",
        "reason": (
            "Your file has no handoff on record. Call handoff_submit, "
            "or message your Lead with the blocker, then stop."
        ),
    }


# -- 11. Stop -------------------------------------------------------------------------


def handle_stop(ledger: Ledger, data: dict) -> dict | None:
    if data.get("stop_hook_active"):
        return None
    run = _active_run_row(ledger)
    if run is None:
        return None
    caller_id = _caller_id(data)

    oracle = ledger.conn.execute(
        "SELECT * FROM agents WHERE run_id = ? AND role = 'oracle' AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (run["run_id"],),
    ).fetchone()
    if oracle is None or oracle["agent_id"] != caller_id:
        return None

    plan_unlocked = ledger.plan_unlocked(oracle["name"], oracle["agent_id"])
    submitted_handoff = ledger.conn.execute(
        "SELECT 1 FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND h.state = 'submitted'",
        (run["run_id"],),
    ).fetchone()
    live_manager = ledger.conn.execute(
        "SELECT 1 FROM agents WHERE run_id = ? AND role = 'manager' AND ended_at IS NULL",
        (run["run_id"],),
    ).fetchone()
    needs_user = ledger.conn.execute(
        "SELECT 1 FROM directives WHERE run_id = ? AND state = 'open' AND outcome = 'needs_user'",
        (run["run_id"],),
    ).fetchone()

    reasons = []
    if plan_unlocked:
        reasons.append(f"{len(plan_unlocked)} unlocked phase(s)")
    if submitted_handoff is not None:
        reasons.append("a handoff awaiting review")
    if live_manager is not None:
        reasons.append("a live Manager")

    if reasons and needs_user is None:
        return {
            "decision": "block",
            "reason": (
                f"The run still has work: {', '.join(reasons)}. Continue, or call run_finish."
            ),
        }
    return None


# -- 12. SessionEnd -----------------------------------------------------------------


def handle_session_end(ledger: Ledger, data: dict) -> None:
    session_id = data.get("session_id")
    if session_id:
        ledger.agent_stop(session_id, end_reason=data.get("reason"))
    return None
