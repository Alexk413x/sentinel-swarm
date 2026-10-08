from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from swarm_ledger import auth, sessions
from swarm_ledger.db import write_tx
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger, repo_slug, run_stamp, session_name_for

_ORACLE_SESSION = "sess-oracle-0000"


class FakeClaude:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.listing: list[dict] = []
        self.fail: dict[str, str] = {}
        self.appear = True
        self._count = 0

    def __call__(self, args: list[str], cwd: Path | None = None) -> str:
        self.calls.append((list(args), cwd))
        if args[:2] == ["agents", "--json"]:
            if "agents" in self.fail:
                raise LedgerError(self.fail["agents"])
            return json.dumps(self.listing)
        if args[0] == "stop":
            if "stop" in self.fail:
                raise LedgerError(self.fail["stop"])
            for entry in self.listing:
                if entry.get("id") == args[1]:
                    entry["status"] = "stopped"
            return ""
        if args[0] == "--resume":
            entry = self.entry(args[1])
            if entry is not None:
                entry["status"] = "busy"
            return f"\x1b[2mbackgrounded · {args[1][:8]} · resumed\x1b[0m\n"
        name = args[args.index("--name") + 1]
        self._count += 1
        session_id = f"{self._count:08x}-aaaa-bbbb-cccc-dddddddddddd"
        if self.appear:
            self.add(session_id, name, bg_id=session_id[:8])
        return f"backgrounded · {session_id[:8]} · {name}\n"

    def add(self, session_id: str, name: str, bg_id: str | None = None) -> dict:
        entry = {"pid": 100 + len(self.listing), "sessionId": session_id, "name": name}
        entry |= {"status": "busy", "state": "working"}
        if bg_id:
            entry["id"] = bg_id
        self.listing.append(entry)
        return entry

    def entry(self, session_id: str) -> dict | None:
        return next((e for e in self.listing if e["sessionId"] == session_id), None)

    def stop_session(self, session_id: str) -> None:
        entry = self.entry(session_id)
        assert entry is not None
        entry["status"] = "stopped"

    def commands(self, first: str) -> list[list[str]]:
        return [args for args, _ in self.calls if args and args[0] == first]


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(sessions, "_run", fake)
    return fake


_ROLE_FILE = """---
name: swarm-{role}
description: The {role}.
model: sonnet
permissionMode: acceptEdits
tools: Read, SendMessage, mcp__swarm-ledger
mcpServers:
  - codebase-kg:
      command: python
      args: [".sentinel-swarm/hook.py", "mcp", "codebase-kg@codebase-kg", "codebase-kg"]
---

You are a {role}.
"""


@pytest.fixture
def host(tmp_path: Path, repo_root: Path) -> Path:
    root = tmp_path / "My Host"
    (root / ".git").mkdir(parents=True)
    claude_dir = root / ".claude"
    (claude_dir / "agents").mkdir(parents=True)
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    (claude_dir / "sentinel-swarm.local.md").write_text(template, encoding="utf-8")
    for role in ("oracle", "manager", "lead", "coder"):
        (claude_dir / "agents" / f"swarm-{role}.md").write_text(
            _ROLE_FILE.format(role=role), encoding="utf-8"
        )
    records = root / ".sentinel-swarm"
    records.mkdir()
    (records / "server.json").write_text(
        json.dumps({"url": "http://127.0.0.1:4321/mcp", "port": 4321, "pid": 1}), encoding="utf-8"
    )
    auth.ensure_token(root)
    return root


@pytest.fixture
def ledger(host: Path) -> Ledger:
    return Ledger(host, db_path=host / ".sentinel-swarm" / "ledger.db")


def _spawn(ledger: Ledger, parent: tuple[str, str], name: str, role: str, model: str, **ids):
    ledger.brief_create(parent[0], parent[1], name, role, model, f"Own {name}.", **ids)
    spawned = ledger.agent_spawn(parent[0], parent[1], name)
    return ledger.brief_ack(name, spawned["agent_id"])


@dataclass
class Ctx:
    oracle: tuple[str, str]
    manager: tuple[str, str]
    lead: tuple[str, str]
    phase_id: int
    module_id: int
    run_id: int


def _bootstrap(ledger: Ledger, claude: FakeClaude) -> Ctx:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    started = ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    oracle = ("oracle", str(started["oracle"]["agent_id"]))
    ledger.repo_check(*oracle)
    phase = ledger.phase_add(*oracle, "phase-1")
    ledger.phase_update(*oracle, phase["phase_id"], "unlocked")
    manager = _spawn(
        ledger, oracle, "mgr-p1-phase-1", "manager", "opus", phase_id=phase["phase_id"]
    )
    mgr = ("mgr-p1-phase-1", str(manager["agent_id"]))
    module = ledger.module_add(*mgr, phase["phase_id"], "module-1")
    lead = _spawn(ledger, mgr, "lead-p1-module-1", "lead", "sonnet", module_id=module["module_id"])
    return Ctx(
        oracle=oracle,
        manager=mgr,
        lead=("lead-p1-module-1", str(lead["agent_id"])),
        phase_id=phase["phase_id"],
        module_id=module["module_id"],
        run_id=started["run"]["run_id"],
    )


def _claim_coder(ledger: Ledger, ctx: Ctx, slug: str) -> tuple[str, int]:
    name = f"coder-p1-module-1-{slug}"
    claimed = ledger.claim_file(*ctx.lead, f"src/{slug}.py", f"tests/test_{slug}.py", name)
    return name, claimed["file_id"]


def _brief_coder(ledger: Ledger, ctx: Ctx, slug: str, model: str = "haiku", **options) -> str:
    name, file_id = _claim_coder(ledger, ctx, slug)
    ledger.brief_create(
        *ctx.lead, name, "coder", model, f"Write {slug}.", file_id=file_id, **options
    )
    return name


def _brief_second_lead(ledger: Ledger, ctx: Ctx, number: int = 2) -> str:
    module = ledger.module_add(*ctx.manager, ctx.phase_id, f"module-{number}")
    name = f"lead-p1-module-{number}"
    ledger.brief_create(
        *ctx.manager, name, "lead", "sonnet", "Own it.", module_id=module["module_id"]
    )
    return name


def _spawn_coder(ledger: Ledger, ctx: Ctx, slug: str = "c1") -> tuple[str, str]:
    name, file_id = _claim_coder(ledger, ctx, slug)
    coder = _spawn(ledger, ctx.lead, name, "coder", "sonnet", file_id=file_id)
    return (name, str(coder["agent_id"]))


def _row(ledger: Ledger, agent_id: str) -> dict:
    return dict(
        ledger.conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    )


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


def _accept_module(ledger: Ledger, ctx: Ctx) -> dict:
    _insert_passing_test_run(ledger, ctx.run_id, ctx.manager[1], "phase")
    return ledger.module_review(
        *ctx.manager, ctx.module_id, "accepted", "looks good", scores=_review_scores()
    )


def _hand_up_and_accept_phase(ledger: Ledger, ctx: Ctx) -> dict:
    ledger.phase_update(*ctx.manager, ctx.phase_id, "handed_up")
    _insert_passing_test_run(ledger, ctx.run_id, ctx.oracle[1], "full")
    return ledger.phase_review(
        *ctx.oracle, ctx.phase_id, "accepted", "ship it", scores=_review_scores()
    )


# -- the CLI wrapper ------------------------------------------------------------------------


def _untimed(text: str) -> str:
    assert re.search(r" elapsed \d+s(?: / \d+s)?", text), text
    return re.sub(r" elapsed \d+s(?: / \d+s)?", "", text)


def test_the_binary_comes_from_sentinel_swarm_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sessions.CLAUDE_VAR, sys.executable)
    assert sessions.claude_binary() == sys.executable
    monkeypatch.delenv(sessions.CLAUDE_VAR)
    assert Path(sessions.claude_binary()).stem.lower() == "claude"


def test_run_returns_stdout_and_raises_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sessions.CLAUDE_VAR, sys.executable)
    assert sessions._run(["-c", "print('[]')"]).strip() == "[]"
    code = "import sys; sys.stderr.write('no such session'); sys.exit(3)"
    with pytest.raises(LedgerError, match="exit code 3: no such session"):
        sessions._run(["-c", code])


def test_run_raises_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv(sessions.CLAUDE_VAR, str(tmp_path / "no-claude-here"))
    with pytest.raises(LedgerError, match="cannot run"):
        sessions.list_sessions()


@pytest.mark.parametrize(
    "output",
    [
        "backgrounded · 3378dc08 · my-host-r1-lead-p1-module-1\n",
        "\x1b[32mbackgrounded\x1b[0m · 3378dc08 · x\n",
        "starting...\nbackgrounded Â· 3378dc08 Â· x\n",
    ],
)
def test_parse_bg_id_reads_the_backgrounded_line(output: str) -> None:
    assert sessions.parse_bg_id(output) == "3378dc08"


def test_is_live_reads_status_and_pid(claude: FakeClaude) -> None:
    claude.add("s-running", "a")
    claude.listing.append({"pid": 5, "sessionId": "s-stopped", "status": "stopped"})
    claude.listing.append({"sessionId": "s-no-pid", "status": "idle"})
    claude.listing.append({"pid": 6, "sessionId": "s-turn-done", "status": "idle", "state": "done"})
    assert sessions.is_live("s-running") is True
    assert sessions.is_live("s-turn-done") is True
    assert sessions.is_live("s-stopped") is False
    assert sessions.is_live("s-no-pid") is False
    assert sessions.is_live("s-unknown") is False
    assert sessions.live_names(claude.listing) == {"a"}


def test_spawn_puts_the_prompt_first_and_returns_both_ids(claude: FakeClaude, tmp_path: Path):
    bg_id, session_id = sessions.spawn("You are x.", "n-1", ["--agent", "swarm-lead"], tmp_path)
    args, cwd = claude.calls[0]
    assert args == ["You are x.", "--bg", "--name", "n-1", "--agent", "swarm-lead"]
    assert cwd == tmp_path
    assert session_id.startswith(bg_id)
    assert claude.entry(session_id) is not None


def test_spawn_stops_a_session_that_never_appears(
    claude: FakeClaude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "_SPAWN_WAIT_S", 0.0)
    claude.appear = False
    with pytest.raises(LedgerError, match="did not appear"):
        sessions.spawn("You are x.", "n-1", [], tmp_path)
    assert claude.commands("stop") == [["stop", "00000001"]]


def test_stop_and_resume_run_the_documented_commands(claude: FakeClaude, tmp_path: Path) -> None:
    sessions.stop("3378dc08")
    assert sessions.resume("3378dc08-full-id", "Re-read.", cwd=tmp_path) == "3378dc08"
    assert [args for args, _ in claude.calls] == [
        ["stop", "3378dc08"],
        ["--resume", "3378dc08-full-id", "--bg", "Re-read."],
    ]
    assert claude.calls[1][1] == tmp_path


# -- agent_spawn ------------------------------------------------------------------------------


def test_agent_spawn_starts_the_session_with_the_role_files_flags(
    ledger: Ledger, claude: FakeClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    spawn_calls = [(args, cwd) for args, cwd in claude.calls if "--bg" in args]
    args, cwd = spawn_calls[-1]
    assert cwd == host
    assert args[:4] == [
        "You are lead-p1-module-1. Read your brief from the swarm ledger and follow it.",
        "--bg",
        "--name",
        ledger.session_name(ctx.run_id, "lead-p1-module-1"),
    ]
    options = args[4:]
    assert options[:10] == [
        "--agent",
        "swarm-lead",
        "--model",
        "sonnet",
        "--effort",
        "medium",
        "--permission-mode",
        "acceptEdits",
        "--strict-mcp-config",
        "--mcp-config",
    ]
    config = json.loads(options[10])
    assert config["mcpServers"]["swarm-ledger"] == {
        "type": "http",
        "url": "http://127.0.0.1:4321/mcp",
        "headers": {"Authorization": f"Bearer {auth.read_token(host)}"},
    }
    assert config["mcpServers"]["codebase-kg"]["command"] == "python"
    assert options[11:] == [
        "--allowedTools",
        "Read,SendMessage,mcp__swarm-ledger",
        "--settings",
        '{"worktree":{"bgIsolation":"none"}}',
    ]


def test_agent_spawn_registers_the_row_and_brief_ack_binds_it(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _brief_coder(ledger, ctx, "a")
    spawned = ledger.agent_spawn(*ctx.lead, coder)

    entry = claude.listing[-1]
    assert spawned["agent_id"] == entry["sessionId"]
    assert spawned["bg_id"] == entry["id"]
    assert spawned["session_name"] == ledger.session_name(ctx.run_id, coder)
    assert spawned["state"] == "registered"
    assert spawned["role"] == "coder"
    assert spawned["model"] == "haiku"
    assert spawned["parent_agent_id"] == ctx.lead[1]
    assert spawned["module_id"] == ctx.module_id
    assert any(e["reason"].startswith("agent_spawn: ") for e in ledger.events(spawned["agent_id"]))

    bound = ledger.brief_ack(coder, spawned["agent_id"])
    assert bound["state"] == "working"
    assert bound["runtime"] == "session"
    assert bound["session_name"] == spawned["session_name"]


def test_brief_ack_refuses_a_spawned_session_under_another_name(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _brief_second_lead(ledger, ctx)
    _brief_second_lead(ledger, ctx, 3)
    spawned = ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")
    with pytest.raises(LedgerError, match="was spawned as 'lead-p1-module-2'"):
        ledger.brief_ack("lead-p1-module-3", spawned["agent_id"])


def test_agent_spawn_refuses_a_caller_that_is_not_the_briefs_parent(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _brief_second_lead(ledger, ctx)
    with pytest.raises(LedgerError, match="not the parent"):
        ledger.agent_spawn(*ctx.oracle, "lead-p1-module-2")
    with pytest.raises(LedgerError, match="no unacknowledged brief"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-9")


def test_agent_spawn_refuses_twice_for_the_same_child(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    _brief_second_lead(ledger, ctx)
    ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")
    with pytest.raises(LedgerError, match="already runs as session"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")


def test_agent_spawn_refuses_a_name_a_live_session_holds(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.add("someone-else", ledger.session_name(ctx.run_id, "lead-p1-module-2"))
    _brief_second_lead(ledger, ctx)
    with pytest.raises(LedgerError, match="already has the name"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")


def test_agent_spawn_applies_the_parallelism_cap_only_when_set(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    _brief_second_lead(ledger, ctx)
    ledger.settings.parallelism_cap = 3
    with pytest.raises(LedgerError, match="parallelism cap of 3 is reached"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")
    ledger.settings.parallelism_cap = None
    assert ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")["state"] == "registered"


def test_agent_spawn_names_setup_when_the_role_file_is_missing(
    ledger: Ledger, claude: FakeClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    (host / ".claude" / "agents" / "swarm-lead.md").unlink()
    _brief_second_lead(ledger, ctx)
    with pytest.raises(LedgerError, match="run /sentinel-swarm:setup"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")


def test_agent_spawn_needs_the_server_url(ledger: Ledger, claude: FakeClaude, host: Path) -> None:
    ctx = _bootstrap(ledger, claude)
    (host / ".sentinel-swarm" / "server.json").unlink()
    _brief_second_lead(ledger, ctx)
    with pytest.raises(LedgerError, match="no ledger server"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")


def test_agent_spawn_refuses_a_recorded_url_off_loopback(
    ledger: Ledger, claude: FakeClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    (host / ".sentinel-swarm" / "server.json").write_text(
        json.dumps({"url": "http://10.0.0.5:4321/mcp", "port": 4321, "pid": 1}), encoding="utf-8"
    )
    _brief_second_lead(ledger, ctx)
    calls = len(claude.calls)
    with pytest.raises(LedgerError, match=r"is not http://127\.0\.0\.1:<port>/mcp"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")
    assert not any("--bg" in args for args, _ in claude.calls[calls:])


def test_agent_spawn_refuses_without_the_ledger_token(
    ledger: Ledger, claude: FakeClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    auth.token_path(host).unlink()
    _brief_second_lead(ledger, ctx)
    with pytest.raises(LedgerError, match="no ledger token"):
        ledger.agent_spawn(*ctx.manager, "lead-p1-module-2")


def test_repo_slug_lowercases_and_replaces_non_alphanumerics(tmp_path: Path) -> None:
    root = tmp_path / "Sentinel_Swarm.v2"
    root.mkdir()
    assert repo_slug(root) == "sentinel-swarm-v2"
    assert session_name_for(root, 7, "coder-a") == "sentinel-swarm-v2-r7-coder-a"
    stamp = run_stamp("2026-09-26T21:39:40.123Z")
    assert session_name_for(root, 1, "lead-1", stamp) == "sentinel-swarm-v2-r1-09262139-lead-1"


# -- agent_resume -------------------------------------------------------------------------------


def test_agent_resume_refuses_a_running_session(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="still running"):
        ledger.agent_resume(*ctx.manager, "lead-p1-module-1")
    assert claude.commands("--resume") == []


def test_agent_resume_continues_a_stopped_session_with_the_owed_pointer(
    ledger: Ledger, claude: FakeClaude, host: Path
) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.stop_session(ctx.manager[1])
    posted = ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")
    assert posted["next"] == 'agent_resume(target_name="mgr-p1-phase-1")'

    resumed = ledger.agent_resume(*ctx.lead, "mgr-p1-phase-1")
    pointer = f"Message {posted['message_id']} from lead-p1-module-1 is waiting in the ledger; "
    assert resumed["message"].startswith(pointer)
    assert resumed["wakeups_sent"] == 1
    args, cwd = [(a, c) for a, c in claude.calls if a[0] == "--resume"][0]
    assert args[:5] == ["--resume", ctx.manager[1], "--bg", resumed["message"], "--name"]
    assert args[5] == ledger.session_name(ctx.run_id, "mgr-p1-phase-1")
    assert cwd == host
    assert ledger.owed_wakeups(ctx.lead[1]) == []


def test_agent_resume_without_a_debt_sends_the_generic_pointer(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.stop_session(ctx.lead[1])
    resumed = ledger.agent_resume(*ctx.oracle, "lead-p1-module-1")
    assert resumed["message"] == "Re-read your brief and your inbox in the ledger."


def test_agent_resume_refuses_an_unknown_name(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    with pytest.raises(LedgerError, match="no live agent named 'nobody'"):
        ledger.agent_resume(*ctx.oracle, "nobody")


# -- owed wake-ups ---------------------------------------------------------------------------------


def test_message_post_returns_a_send_message_to_a_running_recipient(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    posted = ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")
    session = ledger.session_name(ctx.run_id, "mgr-p1-phase-1")
    pointer = (
        f"Message {posted['message_id']} from lead-p1-module-1 is waiting in the ledger; "
        "read it with message_inbox."
    )
    assert _untimed(posted["next"]) == f'SendMessage(to="{session}", message="{pointer}")'
    owed = ledger.owed_wakeups(ctx.lead[1])
    assert [(w["to_name"], w["to_session_name"], w["reason"]) for w in owed] == [
        ("mgr-p1-phase-1", session, "message_post")
    ]
    assert ledger.wakeups_sent(ctx.lead[1], session) == 1
    assert ledger.owed_wakeups(ctx.lead[1]) == []


def test_next_names_both_calls_when_the_session_list_fails(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.fail["agents"] = "boom"
    posted = ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "hi")
    assert posted["next"].endswith(
        ', or agent_resume(target_name="mgr-p1-phase-1") if that session is not running'
    )


def test_phase_update_handed_up_owes_the_oracle_a_wake_up(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.agent_release(*ctx.manager, ctx.lead[1])
    _accept_module(ledger, ctx)
    phase = ledger.phase_update(*ctx.manager, ctx.phase_id, "handed_up")
    assert _untimed(phase["next"]) == (
        'SendMessage(to="my-host-oracle", '
        f'message="Phase {ctx.phase_id} (p1-phase-1) is handed up and waiting in the ledger.")'
    )
    assert "next" not in ledger.phase_update(*ctx.oracle, ctx.phase_id, "working")


def test_return_work_owes_the_coder_a_wake_up(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _spawn_coder(ledger, ctx)
    file_id = _row(ledger, coder[1])["file_id"]
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO handoffs (file_id, agent_id, state) VALUES (?, ?, 'submitted')",
            (file_id, coder[1]),
        )
        handoff_id = int(cur.lastrowid or 0)
    attempt = ledger.return_work(*ctx.lead, handoff_id, ["slow"], ["performance"])
    assert attempt["next"] == (
        f'SendMessage(to="{ledger.session_name(ctx.run_id, coder[0])}", '
        f'message="Handoff {handoff_id} '
        'for src/c1.py is returned; read its issues with message_inbox and fix them.")'
    )
    inbox = ledger.message_inbox(*coder)["messages"]
    assert len(inbox) == 1
    assert inbox[0]["from_name"] == "lead-p1-module-1"
    assert "- slow" in inbox[0]["body"]
    assert "Dimensions to move: performance" in inbox[0]["body"]


def test_a_recipient_without_a_session_owes_nothing(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET session_name = NULL WHERE name = 'mgr-p1-phase-1'")
    posted = ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "hi")
    assert posted["next"] is None
    assert ledger.owed_wakeups(ctx.lead[1]) == []


def test_a_debt_to_a_released_agent_is_no_longer_owed(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.message_post(*ctx.manager, "lead-p1-module-1", "wrap up")
    ledger.agent_release(*ctx.manager, ctx.lead[1])
    assert ledger.owed_wakeups(ctx.manager[1]) == []


# -- release stops the session ---------------------------------------------------------------------


def test_agent_release_stops_the_childs_session(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    bg_id = _row(ledger, ctx.lead[1])["bg_id"]
    released = ledger.agent_release(*ctx.manager, ctx.lead[1])
    assert released["state"] == "released"
    assert claude.commands("stop") == [["stop", bg_id]]
    assert any(e["reason"] == f"session {bg_id} stopped" for e in ledger.events(ctx.lead[1]))


def test_a_failed_stop_does_not_fail_the_release(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _bootstrap(ledger, claude)
    claude.fail["stop"] = "no session 1234"
    released = ledger.agent_release(*ctx.manager, ctx.lead[1])
    assert released["state"] == "released"
    reasons = [e["reason"] for e in ledger.events(ctx.lead[1])]
    assert any("was not stopped: no session 1234" in r for r in reasons)


def test_phase_approval_and_run_finish_stop_every_session_but_the_oracles(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE agents SET bg_id = 'oraclebg' WHERE agent_id = ?", (ctx.oracle[1],))
    _accept_module(ledger, ctx)
    ledger.agent_release(*ctx.manager, ctx.lead[1])
    _hand_up_and_accept_phase(ledger, ctx)
    ledger.phase_update(*ctx.oracle, ctx.phase_id, "approved")
    stopped = {args[1] for args in claude.commands("stop")}
    assert stopped == {
        _row(ledger, ctx.manager[1])["bg_id"],
        _row(ledger, ctx.lead[1])["bg_id"],
    }

    ledger.run_finish(*ctx.oracle, "success")
    assert "oraclebg" not in {args[1] for args in claude.commands("stop")}


# -- run_start and the one-swarm-per-repo rule --------------------------------------------------


def test_run_start_records_the_oracles_session_name(ledger: Ledger, claude: FakeClaude) -> None:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    started = ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    assert started["oracle"]["session_name"] == "my-host-oracle"


def test_run_start_leaves_the_session_name_empty_when_the_list_fails(
    ledger: Ledger, claude: FakeClaude
) -> None:
    claude.fail["agents"] = "claude is not installed"
    started = ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    assert started["oracle"]["session_name"] is None


def test_run_start_refuses_while_another_oracle_session_is_running(
    ledger: Ledger, claude: FakeClaude
) -> None:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    with pytest.raises(LedgerError, match="held by the Oracle session 'my-host-oracle'"):
        ledger.run_start(prd="Build Y", session_id="sess-intruder")
    live = ledger.conn.execute(
        "SELECT agent_id FROM agents WHERE role = 'oracle' AND ended_at IS NULL"
    ).fetchall()
    assert [r["agent_id"] for r in live] == [_ORACLE_SESSION]


def test_run_start_resumes_a_run_whose_oracle_session_is_dead(
    ledger: Ledger, claude: FakeClaude
) -> None:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    first = ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    claude.stop_session(_ORACLE_SESSION)
    claude.add("sess-new", "my-host-oracle")
    resumed = ledger.run_start(prd="ignored", session_id="sess-new")
    assert resumed["resumed"] is True
    assert resumed["run"]["run_id"] == first["run"]["run_id"]
    assert resumed["oracle"]["session_name"] == "my-host-oracle"


def test_run_start_refuses_when_it_cannot_list_sessions_to_check(
    ledger: Ledger, claude: FakeClaude
) -> None:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    claude.fail["agents"] = "claude is not installed"
    with pytest.raises(LedgerError, match="cannot tell whether"):
        ledger.run_start(prd="Build Y", session_id="sess-other")


def test_the_same_oracle_session_calling_run_start_again_resumes(
    ledger: Ledger, claude: FakeClaude
) -> None:
    claude.add(_ORACLE_SESSION, "my-host-oracle")
    ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    again = ledger.run_start(prd="Build X", session_id=_ORACLE_SESSION)
    assert again["resumed"] is True
    assert again["oracle"]["agent_id"] == _ORACLE_SESSION


# -- CLAUDE_DEV_CHANNELS --------------------------------------------------------------------


def test_dev_channels_are_empty_without_the_variable() -> None:
    assert sessions.dev_channel_args() == []


def test_dev_channels_split_on_spaces_and_commas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_DEV_CHANNELS", " plugin:q@m, server:x  server:y,")
    assert sessions.dev_channel_args() == [
        "--dangerously-load-development-channels",
        "plugin:q@m",
        "server:x",
        "server:y",
    ]


def test_dev_channels_go_last_on_spawn_and_resume(
    claude: FakeClaude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_DEV_CHANNELS", "plugin:q@m")
    sessions.spawn("You are x.", "n-1", ["--agent", "swarm-lead"], tmp_path)
    sessions.resume("3378dc08-full-id", "Re-read.", cwd=tmp_path)
    tail = ["--dangerously-load-development-channels", "plugin:q@m"]
    assert claude.calls[0][0][-2:] == tail
    assert claude.calls[-1][0] == ["--resume", "3378dc08-full-id", "--bg", "Re-read.", *tail]


def test_a_background_entry_without_a_pid_counts_as_running() -> None:
    entry = {"id": "fda406cd", "kind": "background", "sessionId": "s-1", "state": "blocked"}
    assert sessions.is_running(entry)
    assert not sessions.is_running(entry | {"state": "stopped"})
    assert not sessions.is_running({"kind": "interactive", "sessionId": "s-2", "status": "idle"})


def test_a_briefs_effort_overrides_the_settings_and_is_recorded(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _brief_coder(ledger, ctx, "a", effort="high")
    spawned = ledger.agent_spawn(*ctx.lead, coder)

    args = next(args for args, _ in reversed(claude.calls) if "--bg" in args)
    assert args[args.index("--effort") + 1] == "high"
    assert spawned["effort"] == "high"
    with pytest.raises(LedgerError, match="unknown effort"):
        ledger.brief_create(
            *ctx.lead, "coder-p1-module-1-b", "coder", "haiku", "Write b.", effort="huge"
        )


def test_agent_spawn_records_the_settings_effort_without_a_brief_effort(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    coder = _brief_coder(ledger, ctx, "a")

    assert ledger.agent_spawn(*ctx.lead, coder)["effort"] == ledger.settings.effort["coder"]
