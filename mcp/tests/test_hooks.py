from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger

_KNOWN_EVENTS = {
    "session_start",
    "pre_agent",
    "subagent_start",
    "pre_write",
    "pre_shell",
    "pre_ledger",
    "post_any",
    "post_shell",
    "pre_compact",
    "subagent_stop",
    "stop",
    "session_end",
}


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    text = text.replace("build_command:\n", "build_command: npm run build\n")
    text = text.replace("lint_command:\n", "lint_command: ruff check\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "manager-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("manager-1", "mgr-agent")

    module = ledger.module_add("manager-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "manager-1", "mgr-agent", "lead-1", "lead", "sonnet", "Own module-1.", module_id=module_id
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-1", "lead-agent")

    return {
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _spawn_coder(ledger: Ledger, ctx: dict, coder_name: str, path: str, test_path: str) -> dict:
    claimed = ledger.claim_file("lead-1", ctx["lead"]["agent_id"], path, test_path, coder_name)
    ledger.brief_create(
        "lead-1",
        ctx["lead"]["agent_id"],
        coder_name,
        "coder",
        "sonnet",
        "Implement it.",
        module_id=ctx["module_id"],
        file_id=claimed["file_id"],
    )
    agent_id = f"{coder_name}-agent"
    ledger.agent_register_start(agent_id, "coder", parent_agent_id=ctx["lead"]["agent_id"])
    return ledger.brief_ack(coder_name, agent_id)


def _submit_handoff_row(ledger: Ledger, coder: dict, path: str) -> None:
    file_row = ledger.who_owns(path)["file"]
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO handoffs (file_id, agent_id, state) VALUES (?, ?, 'submitted')",
            (file_row["file_id"], coder["agent_id"]),
        )


# -- non-swarm callers ----------------------------------------------------------------


def test_non_swarm_caller_returns_none_for_every_pre_hook(ledger: Ledger) -> None:
    _bootstrap(ledger)
    assert (
        events.handle_pre_agent(
            ledger, {"agent_id": "ghost", "tool_input": {"subagent_type": "sentinel-swarm:coder"}}
        )
        is None
    )
    assert (
        events.handle_pre_write(
            ledger, {"agent_id": "ghost", "tool_input": {"file_path": "src/a.py"}}
        )
        is None
    )
    assert (
        events.handle_pre_shell(ledger, {"agent_id": "ghost", "tool_input": {"command": "ls"}})
        is None
    )


# -- pre_write --------------------------------------------------------------------------


def test_pre_write_denies_a_lead(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "a.py")},
    }
    result = events.handle_pre_write(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Coder" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_write_denies_a_coder_on_a_foreign_path(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder1 = _spawn_coder(ledger, ctx, "coder-1", "src/mine.py", "tests/test_mine.py")
    _spawn_coder(ledger, ctx, "coder-2", "src/theirs.py", "tests/test_theirs.py")

    data = {
        "agent_id": coder1["agent_id"],
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "theirs.py")},
    }
    result = events.handle_pre_write(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "coder-2" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_write_allows_the_owned_path_and_test_path(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-own", "src/mine.py", "tests/test_mine.py")

    for rel in ("src/mine.py", "tests/test_mine.py"):
        data = {
            "agent_id": coder["agent_id"],
            "tool_input": {"file_path": str(ledger.repo_root / rel)},
        }
        assert events.handle_pre_write(ledger, data) is None


def test_pre_write_denies_the_records_folder_for_everyone(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    for agent_id in (ctx["oracle_id"], ctx["lead"]["agent_id"], "ghost"):
        data = {
            "agent_id": agent_id,
            "tool_input": {"file_path": str(ledger.repo_root / ".sentinel-swarm" / "ledger.db")},
        }
        result = events.handle_pre_write(ledger, data)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "records folder" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_write_allows_after_override_grant_and_consumes_it_once(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "a.py")},
    }

    first = events.handle_pre_write(ledger, data)
    assert first is not None
    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"

    ledger.override_grant("oracle", ctx["oracle_id"], "write", "lead-1", "src/a.py", "urgent fix")

    assert events.handle_pre_write(ledger, data) is None

    third = events.handle_pre_write(ledger, data)
    assert third is not None
    assert third["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- pre_shell --------------------------------------------------------------------------


def test_pre_shell_allows_the_test_command_and_read_only_git(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-sh", "src/mine.py", "tests/test_mine.py")

    ok_test = {
        "agent_id": coder["agent_id"],
        "tool_input": {"command": "pytest -q tests/test_mine.py"},
    }
    assert events.handle_pre_shell(ledger, ok_test) is None

    ok_git = {"agent_id": coder["agent_id"], "tool_input": {"command": "git status"}}
    assert events.handle_pre_shell(ledger, ok_git) is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "pytest -q tests/test_mine.py; rm -rf /",
        "pytest -q tests/test_mine.py && rm -rf /",
        "pytest -q tests/test_mine.py | tee out.txt",
        "pytest -q $(rm -rf /)",
        "pytest -q `rm -rf /`",
        "pytest -q tests/test_mine.py > out.txt",
        "git status; rm -rf /",
        "git push",
        "pytest-evil -q",
        "",
    ],
)
def test_pre_shell_denies_operators_and_foreign_commands(ledger: Ledger, command: str) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-rm", "src/mine.py", "tests/test_mine.py")
    data = {"agent_id": coder["agent_id"], "tool_input": {"command": command}}
    result = events.handle_pre_shell(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_shell_denies_a_lead(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {"agent_id": ctx["lead"]["agent_id"], "tool_input": {"command": "git status"}}
    result = events.handle_pre_shell(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no shell" in result["hookSpecificOutput"]["permissionDecisionReason"]


# -- pre_ledger -------------------------------------------------------------------------


def test_pre_ledger_stamps_and_overwrites_a_forged_agent_id(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-pl", "src/mine.py", "tests/test_mine.py")
    data = {
        "agent_id": coder["agent_id"],
        "tool_name": "mcp__plugin_sentinel-swarm_swarm-ledger__claim_file",
        "tool_input": {"agent_id": "forged", "path": "src/x.py"},
    }
    result = events.handle_pre_ledger(ledger, data)
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert updated["agent_id"] == coder["agent_id"]
    assert updated["path"] == "src/x.py"


def test_pre_ledger_denies_override_grant_for_a_coder(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-og", "src/mine.py", "tests/test_mine.py")
    data = {
        "agent_id": coder["agent_id"],
        "tool_name": "mcp__swarm-ledger__override_grant",
        "tool_input": {},
    }
    result = events.handle_pre_ledger(ledger, data)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- pre_agent --------------------------------------------------------------------------


def test_pre_agent_denies_wrong_child_role(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"subagent_type": "sentinel-swarm:lead", "model": "sonnet"},
    }
    result = events.handle_pre_agent(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_agent_denies_wrong_model(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"subagent_type": "sentinel-swarm:coder", "model": "opus"},
    }
    result = events.handle_pre_agent(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not approved" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_agent_denies_no_brief(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"subagent_type": "sentinel-swarm:coder", "model": "sonnet"},
    }
    result = events.handle_pre_agent(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no unacked brief" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_agent_allows_with_a_brief(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.claim_file("lead-1", ctx["lead"]["agent_id"], "src/a.py", "tests/test_a.py", "coder-a")
    ledger.brief_create(
        "lead-1", ctx["lead"]["agent_id"], "coder-a", "coder", "sonnet", "Implement it."
    )
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"subagent_type": "sentinel-swarm:coder", "model": "sonnet"},
    }
    assert events.handle_pre_agent(ledger, data) is None


# -- post_any ---------------------------------------------------------------------------


def test_post_any_sets_stale_since_after_an_edit(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-stale", "src/mine.py", "tests/test_mine.py")
    data = {
        "agent_id": coder["agent_id"],
        "tool_name": "Edit",
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "mine.py")},
    }
    assert events.handle_post_any(ledger, data) is None

    file_row = ledger.who_owns("src/mine.py")["file"]
    assert file_row["stale_since"] is not None


# -- subagent_stop ------------------------------------------------------------------------


def test_subagent_stop_blocks_a_working_coder_once_and_not_twice(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-block", "src/mine.py", "tests/test_mine.py")
    data = {"agent_id": coder["agent_id"]}

    first = events.handle_subagent_stop(ledger, data)
    assert first == {
        "decision": "block",
        "reason": (
            "Your file has no handoff on record. Call handoff_submit, "
            "or message your Lead with the blocker, then stop."
        ),
    }

    second = events.handle_subagent_stop(ledger, data)
    assert second is None


def test_subagent_stop_sums_tokens_from_a_transcript(ledger: Ledger, tmp_path: Path) -> None:
    ctx = _bootstrap(ledger)
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 3,
                },
                "content": [{"type": "text", "text": "hi"}],
            }
        },
        {
            "message": {
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "text", "text": "ok"},
                ],
            }
        },
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")

    data = {"agent_id": ctx["lead"]["agent_id"], "agent_transcript_path": str(transcript)}
    assert events.handle_subagent_stop(ledger, data) is None

    row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (ctx["lead"]["agent_id"],)
    ).fetchone()
    assert row["input_tokens"] == 150
    assert row["output_tokens"] == 30
    assert row["cache_read_tokens"] == 5
    assert row["cache_write_tokens"] == 3
    assert row["tool_uses"] == 1


# -- stop ---------------------------------------------------------------------------------


def test_stop_blocks_the_oracle_while_a_handoff_is_submitted(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-stop", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"


def test_stop_allows_when_stop_hook_active_is_true(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-stop2", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")

    data = {"agent_id": ctx["oracle_id"], "stop_hook_active": True}
    assert events.handle_stop(ledger, data) is None


# -- session_start ------------------------------------------------------------------------


def test_session_start_reports_an_active_run(ledger: Ledger) -> None:
    _bootstrap(ledger)
    result = events.handle_session_start(ledger, {})
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "active run" in context
    assert "oracle" in context


# -- the __main__ entry point --------------------------------------------------------------


def _subprocess_env(host: Path, repo_root: Path) -> dict:
    env = dict(os.environ)
    env["SENTINEL_SWARM_LEDGER_DB"] = str(host / ".sentinel-swarm" / "ledger.db")
    env["CLAUDE_PROJECT_DIR"] = str(host)
    src_dir = str(repo_root / "mcp" / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else src_dir
    return env


def test_main_pre_write_deny_via_subprocess(ledger: Ledger, host: Path, repo_root: Path) -> None:
    ctx = _bootstrap(ledger)
    ledger.conn.close()

    payload = json.dumps(
        {
            "agent_id": ctx["lead"]["agent_id"],
            "tool_input": {"file_path": str(host / "src" / "a.py")},
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "swarm_ledger.hooks", "pre_write"],
        input=payload,
        capture_output=True,
        text=True,
        env=_subprocess_env(host, repo_root),
        timeout=30,
    )
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_malformed_json_exits_zero_with_empty_stdout(host: Path, repo_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "swarm_ledger.hooks", "pre_write"],
        input="not valid json{",
        capture_output=True,
        text=True,
        env=_subprocess_env(host, repo_root),
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout == ""


# -- hooks.json -------------------------------------------------------------------------


def test_hooks_json_names_only_dispatchable_events(repo_root: Path) -> None:
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    seen = set()
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                event = hook["command"].rsplit(" ", 1)[-1]
                assert event in _KNOWN_EVENTS, hook["command"]
                seen.add(event)
    assert seen == _KNOWN_EVENTS


def test_pre_ledger_does_not_stamp_tools_without_an_agent_id(ledger: Ledger) -> None:
    data = {
        "session_id": "sess-1",
        "tool_name": "mcp__plugin_sentinel-swarm_swarm-ledger__ledger_info",
        "tool_input": {"agent_id": "forged"},
    }
    result = events.handle_pre_ledger(ledger, data)
    assert "agent_id" not in result["hookSpecificOutput"]["updatedInput"]


# -- paused runs, idle tracking, and wake-ups ---------------------------------------------


def _go_idle(ledger: Ledger, agent_id: str) -> None:
    events.handle_subagent_stop(ledger, {"agent_id": agent_id, "stop_hook_active": True})


def _state(ledger: Ledger, agent_id: str) -> str:
    row = ledger.conn.execute("SELECT state FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return row["state"]


def test_gates_still_apply_while_the_run_is_paused(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.run_pause("oracle", ctx["oracle_id"], "waiting on the user")
    data = {
        "agent_id": ctx["lead"]["agent_id"],
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "a.py")},
    }
    result = events.handle_pre_write(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_session_start_reports_a_paused_run_and_its_reason(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.run_pause("oracle", ctx["oracle_id"], "the API key is missing")
    result = events.handle_session_start(ledger, {})
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "paused run" in context
    assert "the API key is missing" in context


def test_subagent_stop_marks_idle_and_post_tool_use_marks_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_id = ctx["lead"]["agent_id"]

    assert events.handle_subagent_stop(ledger, {"agent_id": lead_id}) is None
    assert _state(ledger, lead_id) == "idle"
    assert events.handle_subagent_stop(ledger, {"agent_id": lead_id}) is None
    idle_events = [e for e in ledger.events(agent_id=lead_id) if e["to_state"] == "idle"]
    assert len(idle_events) == 1
    assert idle_events[0]["from_state"] == "working"
    assert idle_events[0]["reason"] == "subagent_stop"

    events.handle_post_any(ledger, {"agent_id": lead_id, "tool_name": "Read", "tool_input": {}})
    assert _state(ledger, lead_id) == "working"
    assert any(
        e["from_state"] == "idle" and e["to_state"] == "working"
        for e in ledger.events(agent_id=lead_id)
    )


def test_subagent_start_on_resume_marks_an_idle_agent_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_id = ctx["lead"]["agent_id"]
    _go_idle(ledger, lead_id)
    events.handle_subagent_start(ledger, {"agent_id": lead_id, "agent_type": "lead"})
    assert _state(ledger, lead_id) == "working"


def test_subagent_stop_blocks_a_coder_before_marking_it_idle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-idle", "src/mine.py", "tests/test_mine.py")
    data = {"agent_id": coder["agent_id"]}

    first = events.handle_subagent_stop(ledger, data)
    assert first is not None
    assert first["decision"] == "block"
    assert _state(ledger, coder["agent_id"]) == "working"

    assert events.handle_subagent_stop(ledger, data) is None
    assert _state(ledger, coder["agent_id"]) == "idle"


def test_stop_allows_when_the_run_is_paused(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-paused", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)
    ledger.run_pause("oracle", ctx["oracle_id"], "waiting on the user")
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None


def test_stop_allows_while_an_agent_is_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-busy", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    _go_idle(ledger, ctx["manager"]["agent_id"])
    assert _state(ledger, ctx["lead"]["agent_id"]) == "working"
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None


def test_stop_names_the_idle_lead_that_owes_a_handoff_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "coder-wait", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"
    reason = result["reason"]
    assert "for src/mine.py waits on lead-1, which is idle" in reason
    assert f"SendMessage(to={ctx['lead']['agent_id']!r})" in reason
    assert "manager-1 is idle" not in reason
    assert reason.endswith(
        "If the run is blocked on something only the user can fix, call run_pause(reason)."
    )


def test_stop_names_an_idle_agent_with_unread_messages(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.message_post("lead-1", ctx["lead"]["agent_id"], "manager-1", "module-1 is done")
    _go_idle(ledger, ctx["lead"]["agent_id"])
    _go_idle(ledger, ctx["manager"]["agent_id"])

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    reason = result["reason"]
    assert "manager-1 has 1 unread message(s)" in reason
    assert f"SendMessage(to={ctx['manager']['agent_id']!r})" in reason


def test_stop_stores_the_oracle_tokens_from_its_transcript(ledger: Ledger, tmp_path: Path) -> None:
    ctx = _bootstrap(ledger)
    transcript = tmp_path / "oracle.jsonl"
    record = {
        "message": {
            "usage": {
                "input_tokens": 40,
                "output_tokens": 8,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 60,
            }
        }
    }
    transcript.write_text(json.dumps(record), encoding="utf-8")

    events.handle_stop(ledger, {"agent_id": ctx["oracle_id"], "transcript_path": str(transcript)})

    row = ledger.conn.execute(
        "SELECT * FROM agents WHERE agent_id = ?", (ctx["oracle_id"],)
    ).fetchone()
    assert row["input_tokens"] == 40
    assert row["cache_read_tokens"] == 900
    assert row["cache_write_tokens"] == 60
    assert row["ended_at"] is None
