from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import timedelta
from pathlib import Path

from .. import sessions
from ..db import ensure_git_exclude, write_tx
from ..identity import ROLES, LedgerError
from ..ledger import Ledger
from ..watchdog import MONITOR_CALL, WATCH_COMMAND, parse_stamp, utcnow

_RECORDS_DIR = ".sentinel-swarm"
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_READONLY_GIT = frozenset({"status", "diff", "log", "show", "ls-files", "branch"})
_POSIX = os.name != "nt"
_UNSTAMPED_TOOLS = frozenset({"ledger_info", "brief_get", "who_owns", "directive_submit", "events"})
_SHELL_OPERATORS = re.compile(r"[;&|<>`\n]|\$\(")
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
    caller = _swarm_caller(ledger, data.get("session_id"))
    if caller is not None:
        if data.get("transcript_path"):
            ledger.agent_transcript(caller["agent_id"], str(data["transcript_path"]))
        if caller["state"] == "idle":
            ledger.agent_active(caller["agent_id"], "session_start")
        return None

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
    if _swarm_caller(ledger, _caller_id(data)) is None:
        return None
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


# -- 4. PreToolUse: Bash, PowerShell -----------------------------------------------


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


# -- 5. PreToolUse: the swarm-ledger MCP tools -------------------------------------


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


# -- 6. PostToolUse: every tool ----------------------------------------------------


def handle_post_any(ledger: Ledger, data: dict) -> None:
    if _active_run_row(ledger) is None:
        return None
    caller_id = _caller_id(data)
    caller = _swarm_caller(ledger, caller_id)
    if caller is None:
        return None

    tool_name = str(data.get("tool_name") or "")
    ledger.agent_heartbeat(caller["agent_id"], tool_name)
    if caller["state"] == "idle":
        ledger.agent_active(caller["agent_id"], "post_tool_use")
    if data.get("transcript_path") and not caller["transcript_path"]:
        ledger.agent_transcript(caller["agent_id"], str(data["transcript_path"]))

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


def _wake_hint(agent: dict, live_ids: set[str] | None) -> str:
    resume = f"agent_resume(target_name={json.dumps(agent['name'])})"
    if not agent["session_name"]:
        return f"resume it with {resume}"
    send = f"SendMessage(to={json.dumps(agent['session_name'])})"
    if live_ids is None:
        return f"wake it with {send}, or {resume} if that session is not running"
    if agent["agent_id"] in live_ids:
        return f"wake it with {send}"
    return f"its session is not running; resume it with {resume}"


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
            f"{reviewer['name']}, which is {reviewer['state']}; {_wake_hint(reviewer, live_ids)}"
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
                f"{agent['name']} has {unread} unread message(s); {_wake_hint(agent, live_ids)}"
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
            f"which is idle; {_wake_hint(recipient, live_ids)}"
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
            f"{agent['name']} to verify it, which is idle; {_wake_hint(agent, live_ids)}"
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
            f"{_wake_hint(agent, live_ids)}"
        )
        covered |= ancestors(agent["agent_id"])
    return lines


def handle_stop(ledger: Ledger, data: dict) -> dict | None:
    run = _active_run_row(ledger)
    caller_id = _caller_id(data)
    if run is None:
        if not data.get("stop_hook_active"):
            _refresh_finished_report(ledger, caller_id, data.get("transcript_path"))
        return None

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

    if run["state"] == "paused":
        return None

    blocked = _oracle_work_block(ledger, run, dict(oracle))
    if not _watch_unarmed(run):
        return blocked
    if blocked is None:
        return {"decision": "block", "reason": _ARM_WATCHDOG}
    return blocked | {"reason": f"{blocked['reason']}\n{_ARM_WATCHDOG}"}


def _watch_unarmed(run: dict) -> bool:
    now = utcnow()
    heartbeat = parse_stamp(run.get("watch_heartbeat_at"))
    if heartbeat is None or now - heartbeat > _WATCH_STALE:
        return True
    expires = parse_stamp(run.get("watch_expires_at"))
    return expires is not None and expires - now <= _WATCH_RENEW


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
    if any(a["state"] == "working" for a in live):
        return None

    plan_unlocked = ledger.plan_unlocked(oracle["name"], oracle["agent_id"])
    submitted_handoffs = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM handoffs h "
        "JOIN files f ON f.file_id = h.file_id "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND h.state = 'submitted'",
        (run["run_id"],),
    ).fetchone()["n"]
    live_claims = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM files f "
        "JOIN modules m ON m.module_id = f.module_id "
        "JOIN phases p ON p.phase_id = m.phase_id "
        "WHERE p.run_id = ? AND f.released_at IS NULL",
        (run["run_id"],),
    ).fetchone()["n"]
    pending_phase_reviews = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM phases p WHERE p.run_id = ? AND p.state = 'handed_up' "
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
        listed = "; ".join(f"{a['name']} ({a['state']}): {_wake_hint(a, live_ids)}" for a in live)
        lines = [f"Live agents: {listed}. Wake the one with pending work, or call run_pause."]
    else:
        lines = ["Continue the plan, or call run_finish."]

    return {
        "decision": "block",
        "reason": "\n".join(
            [f"The run still has work: {', '.join(reasons)}.", *lines, _PAUSE_HINT]
        ),
    }


def _member_stop(ledger: Ledger, caller: dict, data: dict) -> dict | None:
    transcript_path = data.get("transcript_path")
    ledger.agent_stop(
        caller["agent_id"], transcript_path=transcript_path, tokens=_sum_tokens(transcript_path)
    )

    if not data.get("stop_hook_active"):
        owed = ledger.owed_wakeups(caller["agent_id"])
        if owed:
            steps = [f"- {ledger.next_step(wakeup)}" for wakeup in owed]
            return {
                "decision": "block",
                "reason": "\n".join(
                    ["You still owe a wake-up. Make each call below, then stop:", *steps]
                ),
            }

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
