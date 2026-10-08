from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from swarm_ledger.clock import stamp, utcnow
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import HANDLERS as _HANDLERS
from swarm_ledger.hooks import events, run_event
from swarm_ledger.ledger import Ledger

pytestmark = pytest.mark.usefixtures("claude_sessions")


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
    ledger.watch_armed(1_800_000)

    phase = ledger.phase_add("oracle", oracle_id, "phase-1")
    phase_id = phase["phase_id"]
    ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")

    ledger.brief_create(
        "oracle", oracle_id, "mgr-p1-phase-1", "manager", "opus", "Own phase-1.", phase_id=phase_id
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-1", "mgr-agent")

    module = ledger.module_add("mgr-p1-phase-1", "mgr-agent", phase_id, "module-1")
    module_id = module["module_id"]

    ledger.brief_create(
        "mgr-p1-phase-1",
        "mgr-agent",
        "lead-p1-module-1",
        "lead",
        "sonnet",
        "Own module-1.",
        module_id=module_id,
    )
    ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
    lead = ledger.brief_ack("lead-p1-module-1", "lead-agent")

    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_id": phase_id,
        "module_id": module_id,
        "manager": manager,
        "lead": lead,
    }


def _insert_passing_test_run(ledger: Ledger, run_id: int, agent_id: str, scope: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, ?, 'x', 'x', 0, 1, 0, 0, '')",
            (run_id, agent_id, scope),
        )


def _review_scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10},
        {"dimension": "integration", "value": 10},
        {"dimension": "open_items", "value": 10},
    ]


def _accept_module_and_phase(ledger: Ledger, ctx: dict) -> None:
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=_review_scores(),
    )
    ledger.phase_update("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")
    _insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=_review_scores()
    )


def _spawn_coder(ledger: Ledger, ctx: dict, slug: str, path: str, test_path: str) -> dict:
    coder_name = f"coder-p1-module-1-{slug}"
    claimed = ledger.claim_file(
        "lead-p1-module-1", ctx["lead"]["agent_id"], path, test_path, coder_name
    )
    ledger.brief_create(
        "lead-p1-module-1",
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
    coder1 = _spawn_coder(ledger, ctx, "1", "src/mine.py", "tests/test_mine.py")
    _spawn_coder(ledger, ctx, "2", "src/theirs.py", "tests/test_theirs.py")

    data = {
        "agent_id": coder1["agent_id"],
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "theirs.py")},
    }
    result = events.handle_pre_write(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "coder-p1-module-1-2" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_write_allows_the_owned_path_and_test_path(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "own", "src/mine.py", "tests/test_mine.py")

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

    ledger.override_grant(
        "oracle", ctx["oracle_id"], "write", "lead-p1-module-1", "src/a.py", "urgent fix"
    )

    assert events.handle_pre_write(ledger, data) is None

    third = events.handle_pre_write(ledger, data)
    assert third is not None
    assert third["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- pre_shell --------------------------------------------------------------------------


def test_pre_shell_allows_the_test_command_and_read_only_git(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "sh", "src/mine.py", "tests/test_mine.py")

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
    coder = _spawn_coder(ledger, ctx, "rm", "src/mine.py", "tests/test_mine.py")
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
    coder = _spawn_coder(ledger, ctx, "pl", "src/mine.py", "tests/test_mine.py")
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
    coder = _spawn_coder(ledger, ctx, "og", "src/mine.py", "tests/test_mine.py")
    data = {
        "agent_id": coder["agent_id"],
        "tool_name": "mcp__swarm-ledger__override_grant",
        "tool_input": {},
    }
    result = events.handle_pre_ledger(ledger, data)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- pre_agent --------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["oracle", "manager", "lead", "coder"])
def test_pre_agent_denies_every_agent_call_from_a_swarm_session(ledger: Ledger, who: str) -> None:
    ctx = _bootstrap(ledger)
    ids = {
        "oracle": ctx["oracle_id"],
        "manager": ctx["manager"]["agent_id"],
        "lead": ctx["lead"]["agent_id"],
        "coder": _spawn_coder(ledger, ctx, "ag", "src/a.py", "tests/test_a.py")["agent_id"],
    }
    data = {"session_id": ids[who], "tool_input": {"subagent_type": "general-purpose"}}
    result = events.handle_pre_agent(ledger, data)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "agent_spawn" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pre_agent_allows_a_session_outside_the_swarm(ledger: Ledger) -> None:
    _bootstrap(ledger)
    data = {"session_id": "someone-else", "tool_input": {"subagent_type": "general-purpose"}}
    assert events.handle_pre_agent(ledger, data) is None


# -- post_any ---------------------------------------------------------------------------


def test_post_any_sets_stale_since_after_an_edit(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "stale", "src/mine.py", "tests/test_mine.py")
    data = {
        "agent_id": coder["agent_id"],
        "tool_name": "Edit",
        "tool_input": {"file_path": str(ledger.repo_root / "src" / "mine.py")},
    }
    assert events.handle_post_any(ledger, data) is None

    file_row = ledger.who_owns("src/mine.py")["file"]
    assert file_row["stale_since"] is not None


def _agent_row(ledger: Ledger, agent_id: str) -> dict:
    row = ledger.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return dict(row)


def test_post_activity_skips_the_tools_the_synchronous_post_any_covers(ledger: Ledger) -> None:
    lead_id = _bootstrap(ledger)["lead"]["agent_id"]
    ledger.agent_idle(lead_id, "stop")
    for tool in events.SYNC_POST_TOOLS:
        assert events.handle_post_activity(ledger, {"agent_id": lead_id, "tool_name": tool}) is None
    row = _agent_row(ledger, lead_id)
    assert row["state"] == "idle"
    assert row["current_activity"] not in events.SYNC_POST_TOOLS


def test_post_activity_records_the_heartbeat_and_wakes_an_idle_agent(ledger: Ledger) -> None:
    lead_id = _bootstrap(ledger)["lead"]["agent_id"]
    ledger.agent_idle(lead_id, "stop")
    data = {"agent_id": lead_id, "tool_name": "Read", "transcript_path": "lead.jsonl"}
    assert events.handle_post_activity(ledger, data) is None
    row = _agent_row(ledger, lead_id)
    assert row["state"] == "working"
    assert row["current_activity"] == "Read"
    assert row["last_heartbeat_at"] is not None
    assert row["transcript_path"] == "lead.jsonl"


def test_post_activity_fired_before_the_stop_does_not_wake_the_agent(ledger: Ledger) -> None:
    lead_id = _bootstrap(ledger)["lead"]["agent_id"]
    ledger.agent_idle(lead_id, "stop")
    early = {
        "agent_id": lead_id,
        "tool_name": "Grep",
        events.FIRED_AT_KEY: "2000-01-01T00:00:00.000Z",
    }
    events.handle_post_activity(ledger, early)
    row = _agent_row(ledger, lead_id)
    assert row["state"] == "idle"
    assert row["current_activity"] == "Grep"

    later = {**early, events.FIRED_AT_KEY: stamp(utcnow().replace(year=utcnow().year + 1))}
    events.handle_post_activity(ledger, later)
    assert _agent_row(ledger, lead_id)["state"] == "working"


# -- stop for a Manager, Lead, or Coder ---------------------------------------------------


def test_stop_blocks_a_working_coder_once_and_not_twice(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "block", "src/mine.py", "tests/test_mine.py")
    data = {"session_id": coder["agent_id"]}

    first = events.handle_stop(ledger, data)
    assert first == {
        "decision": "block",
        "reason": (
            "Your file has no handoff on record. Call handoff_submit. If a blocker stops you, "
            'call message_post(to_name="lead-p1-module-1", body=<the blocker>) and make the '
            "SendMessage call its next field names, then stop."
        ),
    }

    second = events.handle_stop(ledger, data)
    assert second is None


def test_stop_sums_a_members_tokens_from_its_transcript(ledger: Ledger, tmp_path: Path) -> None:
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

    data = {"session_id": ctx["lead"]["agent_id"], "transcript_path": str(transcript)}
    assert events.handle_stop(ledger, data) is None

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
    coder = _spawn_coder(ledger, ctx, "stop", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"


def test_stop_allows_when_stop_hook_active_is_true(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "stop2", "src/mine.py", "tests/test_mine.py")
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


def test_run_event_prints_what_the_hook_subprocess_prints(
    ledger: Ledger, host: Path, repo_root: Path
) -> None:
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
    stdout, stderr = run_event("pre_write", payload, host)
    assert (stdout, stderr) == (result.stdout, result.stderr)
    assert json.loads(stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_event_reports_an_unknown_event_and_a_failure_on_stderr(host: Path) -> None:
    assert run_event("nope", "{}", host) == ("", "swarm_ledger.hooks: unknown event 'nope'\n")
    stdout, stderr = run_event("pre_write", "not valid json{", host)
    assert stdout == ""
    assert stderr.startswith("swarm_ledger.hooks pre_write: ")


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


def _hook_commands(hooks: dict) -> list[str]:
    return [hook["command"] for entries in hooks.values() for e in entries for hook in e["hooks"]]


def test_hooks_json_names_only_dispatchable_events(repo_root: Path) -> None:
    data = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for command in _hook_commands(data.get("hooks") or {}):
        assert command.rsplit(" ", 1)[-1] in _HANDLERS, command


def test_role_templates_name_only_dispatchable_events(repo_root: Path) -> None:
    templates = sorted((repo_root / "templates" / "agents").glob("*.md"))
    for template in templates:
        lines = template.read_text(encoding="utf-8").splitlines()
        end = lines.index("---", 1)
        frontmatter = yaml.safe_load("\n".join(lines[1:end]))
        for command in _hook_commands(frontmatter.get("hooks") or {}):
            for part in command.split("||"):
                event = part.split(" hook ", 1)[1].split()[0]
                assert event in _HANDLERS, (template.name, command)


def test_subagent_events_are_gone_from_the_dispatch_table() -> None:
    assert "subagent_start" not in _HANDLERS
    assert "subagent_stop" not in _HANDLERS


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
    events.handle_stop(ledger, {"session_id": agent_id, "stop_hook_active": True})


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


def test_stop_marks_idle_and_post_tool_use_marks_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_id = ctx["lead"]["agent_id"]

    assert events.handle_stop(ledger, {"session_id": lead_id}) is None
    assert _state(ledger, lead_id) == "idle"
    assert events.handle_stop(ledger, {"session_id": lead_id}) is None
    idle_events = [e for e in ledger.events(agent_id=lead_id) if e["to_state"] == "idle"]
    assert len(idle_events) == 1
    assert idle_events[0]["from_state"] == "working"
    assert idle_events[0]["reason"] == "stop"

    events.handle_post_any(ledger, {"session_id": lead_id, "tool_name": "Read", "tool_input": {}})
    assert _state(ledger, lead_id) == "working"
    assert any(
        e["from_state"] == "idle" and e["to_state"] == "working"
        for e in ledger.events(agent_id=lead_id)
    )


def test_session_start_marks_a_resumed_idle_session_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    lead_id = ctx["lead"]["agent_id"]
    _go_idle(ledger, lead_id)
    result = events.handle_session_start(ledger, {"session_id": lead_id, "source": "resume"})
    assert result is not None
    assert "already bound" in result["hookSpecificOutput"]["additionalContext"]
    assert _state(ledger, lead_id) == "working"


def _registered(ledger: Ledger, ctx: dict, role: str) -> tuple[str, str]:
    run_id, oracle_id = ctx["run_id"], ctx["oracle_id"]
    parent = {"manager": oracle_id, "lead": "mgr-agent", "coder": "lead-agent"}.get(role, oracle_id)
    if role == "manager":
        phase = ledger.phase_add("oracle", oracle_id, "phase-2")
        ledger.phase_update("oracle", oracle_id, phase["phase_id"], "unlocked")
        name = "mgr-p2-phase-2"
        ledger.brief_create(
            "oracle", oracle_id, name, "manager", "opus", "Own phase-2.", phase_id=phase["phase_id"]
        )
    elif role == "lead":
        module = ledger.module_add("mgr-p1-phase-1", "mgr-agent", ctx["phase_id"], "module-2")
        name = "lead-p1-module-2"
        ledger.brief_create(
            "mgr-p1-phase-1",
            "mgr-agent",
            name,
            "lead",
            "sonnet",
            "Own module-2.",
            module_id=module["module_id"],
        )
    elif role == "coder":
        name = "coder-p1-module-1-new"
        claimed = ledger.claim_file(
            "lead-p1-module-1", "lead-agent", "src/new.py", "tests/test_new.py", name
        )
        ledger.brief_create(
            "lead-p1-module-1",
            "lead-agent",
            name,
            "coder",
            "sonnet",
            "Implement new.py.",
            module_id=ctx["module_id"],
            file_id=claimed["file_id"],
        )
    else:
        name = "driver-e1"
        with write_tx(ledger.conn) as conn:
            conn.execute(
                "INSERT INTO briefs (run_id, parent_agent_id, child_name, child_role, model, "
                "body) VALUES (?, ?, ?, 'driver', 'sonnet', 'Explore the login screen.')",
                (run_id, oracle_id, name),
            )
    agent_id = f"sess-{name}"
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, parent_agent_id, run_id, state, "
            "session_name, bg_id) VALUES (?, ?, ?, ?, ?, 'registered', ?, 'bg')",
            (agent_id, name, role, parent, run_id, f"host-r1-{name}"),
        )
    return name, agent_id


def _start_context(ledger: Ledger, agent_id: str, source: str = "startup") -> str:
    result = events.handle_session_start(ledger, {"session_id": agent_id, "source": source})
    assert result is not None
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("role", ["manager", "lead", "coder", "driver"])
def test_session_start_binds_a_spawned_role_and_returns_its_start_calls(
    ledger: Ledger, role: str
) -> None:
    ctx = _bootstrap(ledger)
    ledger.guidelines_set("oracle", ctx["oracle_id"], "Use stdlib only.")
    name, agent_id = _registered(ledger, ctx, role)

    context = _start_context(ledger, agent_id)

    assert _state(ledger, agent_id) == "working"
    assert f"brief_ack(caller={name!r})" in context
    assert "ledger_info() returned" in context
    assert f'"repo_root":{json.dumps(str(ledger.repo_root))}' in context
    assert f"brief_get(caller_name={name!r}, child_name={name!r}) returned" in context
    assert f'"child_name":"{name}"' in context
    assert "guidelines_get() returned" in context
    assert "Use stdlib only." in context
    assert ("run_status() returned" in context) == (role in ("manager", "lead"))
    assert len(context) <= 10_000


def test_session_start_records_the_brief_the_hook_hands_over_as_read(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    name, agent_id = _registered(ledger, ctx, "coder")
    _start_context(ledger, agent_id)
    row = ledger.conn.execute(
        "SELECT last_read_by_child_at FROM briefs WHERE child_name = ?", (name,)
    ).fetchone()
    assert row["last_read_by_child_at"] is not None


def test_session_start_on_resume_returns_the_context_without_binding_again(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    name, agent_id = _registered(ledger, ctx, "coder")
    _start_context(ledger, agent_id)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE briefs SET last_read_by_child_at = NULL WHERE child_name = ?", (name,))

    context = _start_context(ledger, agent_id, source="resume")

    assert "already bound" in context
    assert f'"child_name":"{name}"' in context
    acks = [e for e in ledger.events(agent_id=agent_id) if e["reason"] == "brief_ack"]
    assert len(acks) == 1
    row = ledger.conn.execute(
        "SELECT last_read_by_child_at FROM briefs WHERE child_name = ?", (name,)
    ).fetchone()
    assert row["last_read_by_child_at"] is not None


def test_session_start_returns_a_refused_bind_and_the_ledger_waits_for_brief_ack(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    name, agent_id = _registered(ledger, ctx, "lead")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE briefs SET acked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE child_name = ?",
            (name,),
        )

    context = _start_context(ledger, agent_id)

    assert "refused" in context
    assert "no unacked brief" in context
    assert _state(ledger, agent_id) == "registered"

    def pre_ledger(tool: str) -> str:
        data = {"session_id": agent_id, "tool_name": f"mcp__swarm-ledger__{tool}"}
        return events.handle_pre_ledger(ledger, data)["hookSpecificOutput"]["permissionDecision"]

    assert pre_ledger("run_status") == "deny"
    assert pre_ledger("brief_get") == "allow"
    assert pre_ledger("brief_ack") == "allow"

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE briefs SET acked_at = NULL WHERE child_name = ?", (name,))
    assert ledger.brief_ack(name, agent_id)["state"] == "working"
    assert pre_ledger("run_status") == "allow"


def test_session_start_leaves_out_a_start_call_over_the_context_cap(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.guidelines_set("oracle", ctx["oracle_id"], "x" * 12_000)
    name, agent_id = _registered(ledger, ctx, "coder")

    context = _start_context(ledger, agent_id)

    assert len(context) <= 10_000
    assert "guidelines_get() is left out" in context
    assert f'"child_name":"{name}"' in context


def test_session_start_makes_no_start_calls_for_the_oracle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    assert events.handle_session_start(ledger, {"session_id": ctx["oracle_id"]}) is None


def test_stop_blocks_a_coder_before_marking_it_idle(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "idle", "src/mine.py", "tests/test_mine.py")
    data = {"session_id": coder["agent_id"]}

    first = events.handle_stop(ledger, data)
    assert first is not None
    assert first["decision"] == "block"
    assert _state(ledger, coder["agent_id"]) == "working"

    assert events.handle_stop(ledger, data) is None
    assert _state(ledger, coder["agent_id"]) == "idle"


def test_stop_allows_when_the_run_is_paused(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "paused", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)
    ledger.run_pause("oracle", ctx["oracle_id"], "waiting on the user")
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None


def test_stop_allows_while_a_directive_waits_on_the_user_until_the_reply(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _go_idle(ledger, ctx["lead"]["agent_id"])
    _go_idle(ledger, ctx["manager"]["agent_id"])
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is not None

    asked = ledger.directive_submit("user-chat", "alex", "Drop the CLI?")
    ledger.directive_resolve(
        "oracle", ctx["oracle_id"], asked["directive_id"], "needs_user", "Keep the flags?"
    )
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None

    ledger.directive_submit("user-chat", "alex", "Keep them.", reply_to=asked["directive_id"])
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is not None


def test_stop_allows_while_an_agent_is_working(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "busy", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    _go_idle(ledger, ctx["manager"]["agent_id"])
    assert _state(ledger, ctx["lead"]["agent_id"]) == "working"
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None


def test_stop_names_the_idle_lead_that_owes_a_handoff_review(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "wait", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    assert result["decision"] == "block"
    reason = result["reason"]
    assert "for src/mine.py waits on lead-p1-module-1, which is idle" in reason
    assert 'resume it with agent_resume(target_name="lead-p1-module-1")' in reason
    assert "mgr-p1-phase-1 is idle" not in reason
    assert reason.endswith(
        "If the run is blocked on something only the user can fix, call run_pause(reason)."
    )


def test_stop_names_an_idle_agent_with_unread_messages(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    ledger.message_post(
        "lead-p1-module-1", ctx["lead"]["agent_id"], "mgr-p1-phase-1", "module-1 is done"
    )
    _go_idle(ledger, ctx["lead"]["agent_id"])
    _go_idle(ledger, ctx["manager"]["agent_id"])

    result = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert result is not None
    reason = result["reason"]
    assert "mgr-p1-phase-1 has 1 unread message(s)" in reason
    assert 'agent_resume(target_name="mgr-p1-phase-1")' in reason


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


def test_stop_after_run_finish_records_the_oracle_tokens_and_rebuilds_the_report(
    ledger: Ledger, host: Path, tmp_path: Path
) -> None:
    ctx = _bootstrap(ledger)
    ledger.agent_release("mgr-p1-phase-1", "mgr-agent", ctx["lead"]["agent_id"])
    _accept_module_and_phase(ledger, ctx)
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    ledger.run_finish("oracle", ctx["oracle_id"], "success")
    transcript = tmp_path / "oracle.jsonl"
    usage = {"input_tokens": 7, "output_tokens": 3}
    transcript.write_text(json.dumps({"message": {"usage": usage}}), encoding="utf-8")

    events.handle_stop(ledger, {"agent_id": ctx["oracle_id"], "transcript_path": str(transcript)})

    report = (host / ".sentinel-swarm" / "report.md").read_text(encoding="utf-8")
    assert "- oracle (oracle, opus): tokens in=7 out=3" in report


# -- sessions: owed wake-ups and wake hints ------------------------------------------------


def _as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def _running(listing: list[dict], agent_id: str, session_name: str) -> None:
    listing.append({"pid": 7, "sessionId": agent_id, "name": session_name, "status": "idle"})


def test_stop_blocks_a_member_until_it_sends_the_owed_wake_up(
    ledger: Ledger, claude_sessions: list[dict]
) -> None:
    ctx = _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    _running(claude_sessions, "mgr-agent", "host-r1-manager-1")
    posted = ledger.message_post(
        "lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "module-1 is done"
    )
    pointer = f"Message {posted['message_id']} from lead-p1-module-1 is waiting in the ledger; "
    assert posted["next"].startswith(f'SendMessage(to="host-r1-manager-1", message="{pointer}')

    blocked = events.handle_stop(ledger, {"session_id": ctx["lead"]["agent_id"]})
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert f"- {posted['next']}" in blocked["reason"]
    assert _state(ledger, "lead-agent") == "working"

    events.handle_post_any(
        ledger,
        {
            "session_id": "lead-agent",
            "tool_name": "SendMessage",
            "tool_input": {"to": "host-r1-manager-1", "message": "see the ledger"},
        },
    )
    assert ledger.owed_wakeups("lead-agent") == []
    assert events.handle_stop(ledger, {"session_id": "lead-agent"}) is None
    assert _state(ledger, "lead-agent") == "idle"


def test_stop_names_agent_resume_when_the_recipient_session_is_not_running(
    ledger: Ledger,
) -> None:
    ctx = _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "module-1 is done")

    blocked = events.handle_stop(ledger, {"session_id": ctx["lead"]["agent_id"]})
    assert blocked is not None
    assert '- agent_resume(target_name="mgr-p1-phase-1")' in blocked["reason"]


def test_stop_allows_an_owing_member_when_stop_hook_active(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "module-1 is done")
    data = {"session_id": "lead-agent", "stop_hook_active": True}
    assert events.handle_stop(ledger, data) is None
    assert _state(ledger, "lead-agent") == "idle"


def test_stop_blocks_a_member_with_unread_mail_once(ledger: Ledger) -> None:
    _bootstrap(ledger)
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "Start on module-1.")

    blocked = events.handle_stop(ledger, {"session_id": "lead-agent"})
    assert blocked == {
        "decision": "block",
        "reason": "You have 1 unread message(s). Call message_inbox, act on them, then stop.",
    }
    assert _state(ledger, "lead-agent") == "working"

    data = {"session_id": "lead-agent", "stop_hook_active": True}
    assert events.handle_stop(ledger, data) is None
    assert _state(ledger, "lead-agent") == "idle"


def test_stop_names_the_owed_wake_up_before_unread_mail(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.message_post("mgr-p1-phase-1", "mgr-agent", "lead-p1-module-1", "Start on module-1.")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "module-1 is done")

    blocked = events.handle_stop(ledger, {"session_id": "lead-agent"})
    assert blocked is not None
    lines = blocked["reason"].splitlines()
    assert lines[0] == "You still owe a wake-up. Make each call below, then stop:"
    assert lines[1] == '- agent_resume(target_name="mgr-p1-phase-1")'
    assert lines[-1].startswith("You have 1 unread message(s). Call message_inbox")


def test_stop_ignores_unread_mail_from_an_earlier_run(ledger: Ledger) -> None:
    _bootstrap(ledger)
    with write_tx(ledger.conn) as conn:
        conn.execute("INSERT INTO runs (run_id, prd, state) VALUES (9, 'old', 'finished')")
        conn.execute(
            "INSERT INTO messages (run_id, from_name, to_name, body) "
            "VALUES (9, 'mgr-p1-phase-1', 'lead-p1-module-1', 'Old mail.')"
        )
    assert events.handle_stop(ledger, {"session_id": "lead-agent"}) is None


def test_send_message_to_another_session_keeps_the_debt(ledger: Ledger) -> None:
    _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.message_post("lead-p1-module-1", "lead-agent", "mgr-p1-phase-1", "module-1 is done")
    events.handle_post_any(
        ledger,
        {"session_id": "lead-agent", "tool_name": "SendMessage", "tool_input": {"to": "other"}},
    )
    assert len(ledger.owed_wakeups("lead-agent")) == 1


def test_stop_tells_the_oracle_to_send_message_to_a_live_idle_session(
    ledger: Ledger, claude_sessions: list[dict]
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "live", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    _as_session(ledger, "lead-agent", "host-r1-lead-1")
    _running(claude_sessions, "lead-agent", "host-r1-lead-1")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"session_id": ctx["oracle_id"]})
    assert result is not None
    assert (
        'waits on lead-p1-module-1, which is idle; wake it with SendMessage(to="host-r1-lead-1")'
        in (result["reason"])
    )


def test_stop_tells_the_oracle_to_resume_a_stopped_session(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "dead", "src/mine.py", "tests/test_mine.py")
    _submit_handoff_row(ledger, coder, "src/mine.py")
    _as_session(ledger, "lead-agent", "host-r1-lead-1")
    for agent_id in (coder["agent_id"], ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)

    result = events.handle_stop(ledger, {"session_id": ctx["oracle_id"]})
    assert result is not None
    assert (
        'its session is not running; resume it with agent_resume(target_name="lead-p1-module-1")'
        in result["reason"]
    )


def test_stop_does_not_block_the_oracle_for_its_own_debts(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    ledger.agent_release("mgr-p1-phase-1", "mgr-agent", ctx["lead"]["agent_id"])
    _accept_module_and_phase(ledger, ctx)
    ledger.message_post("oracle", ctx["oracle_id"], "mgr-p1-phase-1", "Change of plan.")
    ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")
    assert events.handle_stop(ledger, {"session_id": ctx["oracle_id"]}) is None


def test_post_shell_flags_only_paths_no_claim_in_the_run_covers(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger)
    coder = _spawn_coder(ledger, ctx, "a", "src/a.py", "tests/test_a.py")
    ledger.claim_file(
        "lead-p1-module-1",
        ctx["lead"]["agent_id"],
        "src/b.py",
        "tests/test_b.py",
        "coder-p1-module-1-b",
    )
    porcelain = "?? src/a.py\n?? src/b.py\n?? tests/test_b.py\n?? stray.txt\n"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert "--untracked-files=all" in args
        return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")

    monkeypatch.setattr(events.subprocess, "run", fake_run)
    events.handle_post_shell(ledger, {"session_id": coder["agent_id"]})

    reasons = [
        row["reason"]
        for row in ledger.conn.execute(
            "SELECT reason FROM agent_events WHERE agent_id = ? AND to_state = 'violation'",
            (coder["agent_id"],),
        )
    ]
    assert reasons == ["changed files outside its claim: stray.txt"]


def test_token_totals_count_each_response_once_and_price_its_model(tmp_path: Path) -> None:
    usage = {
        "input_tokens": 10,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 0,
    }
    lines = [
        {
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-5",
                "usage": usage,
                "content": [{"type": "text", "text": "Reading."}],
            }
        },
        {
            "message": {
                "id": "msg-1",
                "model": "claude-sonnet-5",
                "usage": usage,
                "content": [{"type": "tool_use", "name": "Read", "input": {}}],
            }
        },
        {
            "message": {
                "id": "msg-2",
                "model": "<synthetic>",
                "usage": {"input_tokens": 0},
                "content": [],
            }
        },
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")

    totals = events._sum_tokens(str(path))

    assert totals is not None
    assert totals["output_tokens"] == 1_000_000
    assert totals["input_tokens"] == 10
    assert totals["tool_uses"] == 1
    assert totals["cost_usd"] == pytest.approx((10 * 2 + 1_000_000 * 10 + 100 * 0.2) / 1e6)


def _registered_child(ledger: Ledger, ctx: dict, agent_id: str, age_seconds: int) -> None:
    ledger.agent_register_start(agent_id, "manager", parent_agent_id=ctx["oracle_id"])
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET run_id = ?, "
            "started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE agent_id = ?",
            (ctx["run_id"], f"-{age_seconds} seconds", agent_id),
        )


def test_stop_lets_the_oracle_stop_while_a_new_child_starts(
    ledger: Ledger, claude_sessions: list[dict]
) -> None:
    ctx = _bootstrap(ledger)
    for agent_id in (ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)
    _registered_child(ledger, ctx, "mgr-new", age_seconds=10)
    _running(claude_sessions, "mgr-new", "host-r1-manager-2")

    assert events.handle_stop(ledger, {"session_id": ctx["oracle_id"]}) is None


def test_stop_blocks_the_oracle_when_a_new_child_is_not_running_or_is_late(
    ledger: Ledger, claude_sessions: list[dict]
) -> None:
    ctx = _bootstrap(ledger)
    for agent_id in (ctx["lead"]["agent_id"], ctx["manager"]["agent_id"]):
        _go_idle(ledger, agent_id)
    _registered_child(ledger, ctx, "mgr-dead", age_seconds=10)
    _registered_child(ledger, ctx, "mgr-late", age_seconds=300)
    _running(claude_sessions, "mgr-late", "host-r1-manager-3")

    blocked = events.handle_stop(ledger, {"session_id": ctx["oracle_id"]})
    assert blocked is not None
    assert "none working" in blocked["reason"]


def test_session_start_reports_codebase_kg_not_installed(host: Path, ledger: Ledger) -> None:
    result = events.handle_session_start(ledger, {})
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "codebase-kg is not installed" in context


def test_session_start_reports_a_missing_graph_once_the_plugin_is_installed(
    host: Path, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "swarm_ledger.hooks.events.plugin_installed", lambda repo_root, plugin_id: True
    )
    result = events.handle_session_start(ledger, {})
    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "codebase-kg is not installed" not in context
    assert "knowledge/code_graph.db is missing" in context


def test_session_start_reports_neither_once_the_graph_exists(
    host: Path, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "swarm_ledger.hooks.events.plugin_installed", lambda repo_root, plugin_id: True
    )
    (host / "knowledge").mkdir()
    (host / "knowledge" / "code_graph.db").write_text("", encoding="utf-8")
    result = events.handle_session_start(ledger, {})
    if result is not None:
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "codebase-kg" not in context


def test_pre_send_message_is_registered_and_a_gating_event(repo_root: Path) -> None:
    assert _HANDLERS["pre_send_message"] is events.handle_pre_send_message
    shim_text = (repo_root / "templates" / "hook_shim.py").read_text(encoding="utf-8")
    assert '"pre_send_message"' in shim_text
