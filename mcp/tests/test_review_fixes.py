from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from helpers import make_host, settings_text
from test_memory import MovingClaude, _bootstrap, _brief_second_lead, _row, claude, host
from test_watchdog import Exits, FakeClaude, _agent, _dog, _run_id, _wakes
from test_watchdog import claude as dog_claude
from test_watchdog import ledger as dog_ledger
from test_watchdog import now as dog_now

from swarm_ledger import graph, memory, pricing, repo, sessions, watchdog
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import Settings, load_settings

__all__ = ["claude", "dog_claude", "dog_ledger", "dog_now", "host"]


# -- capacity ------------------------------------------------------------------------------------


def test_one_resume_pass_does_not_resume_every_parked_agent_when_memory_is_short(
    ledger: Ledger, claude: MovingClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _bootstrap(ledger, claude)
    second = _brief_second_lead(ledger, ctx)
    lead2 = ledger.agent_spawn(*ctx.manager, second)
    ledger.brief_ack(second, lead2["agent_id"])
    for agent_id in (ctx.manager[1], ctx.lead[1], lead2["agent_id"]):
        claude.stop_session(agent_id)
        ledger.agent_idle(agent_id, "stop")
        ledger._park(agent_id, "retired")
    ledger.message_post(*ctx.oracle, ctx.manager[0], "one")
    ledger.message_post(*ctx.manager, ctx.lead[0], "two")
    ledger.message_post(*ctx.manager, second, "three")
    monkeypatch.setattr(memory, "free_mb", lambda: 1_000)

    resumed = ledger.resume_parked(list(claude.listing))

    assert len(resumed) == 1


def test_a_wake_up_owed_to_an_agent_whose_park_is_claimed_waits_for_its_resume(
    ledger: Ledger, claude: MovingClaude
) -> None:
    ctx = _bootstrap(ledger, claude)
    ledger.mod_session(ctx.lead[1])
    ledger._park(ctx.manager[1], "claimed, `claude stop` pending")
    ledger.message_post(*ctx.lead, "mgr-p1-phase-1", "module-1 is done")

    answer = events.handle_owed(ledger, {"session_id": ctx.lead[1]})

    assert answer == {"wakeups": []}
    assert claude.resumed == []
    assert _row(ledger, ctx.manager[1])["parked_at"] is not None


# -- watchdog ------------------------------------------------------------------------------------


def test_the_oracle_resume_is_claimed_before_claude_resume_runs(
    dog_ledger: Ledger,
    dog_claude: FakeClaude,
    dog_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_id(dog_ledger)
    _agent(dog_ledger, "sess-coder-1", "coder", "working", started=dog_now)
    dog_claude.run("sess-oracle", status="stopped")
    claimed: list[object] = []

    def run(args: list[str], cwd: Path | None = None) -> str:
        if args[0] == "--resume":
            row = dog_ledger.conn.execute(
                "SELECT resume_started_at FROM agents WHERE agent_id = 'sess-oracle'"
            ).fetchone()
            claimed.append(row[0])
        return dog_claude(args, cwd)

    monkeypatch.setattr(sessions, "_run", run)
    dog = _dog(dog_ledger, Exits())
    dog.tick(dog_now)
    dog.tick(dog_now + timedelta(seconds=30))

    assert len(claimed) == 1 and claimed[0] is not None


def test_a_second_oracle_resume_is_skipped_while_one_is_in_flight(
    dog_ledger: Ledger, dog_claude: FakeClaude, dog_now: datetime
) -> None:
    _run_id(dog_ledger)
    _agent(dog_ledger, "sess-coder-1", "coder", "working", started=dog_now)
    dog_claude.run("sess-oracle", status="stopped")
    with write_tx(dog_ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET resume_started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE agent_id = 'sess-oracle'"
        )
    dog = _dog(dog_ledger, Exits())
    dog.tick(dog_now)
    dog.tick(dog_now + timedelta(seconds=30))

    assert dog_claude.resumed == []
    assert "already running" in _wakes(dog_ledger)[0]


def test_a_failed_oracle_resume_releases_its_claim(
    dog_ledger: Ledger, dog_claude: FakeClaude, dog_now: datetime
) -> None:
    _run_id(dog_ledger)
    _agent(dog_ledger, "sess-coder-1", "coder", "working", started=dog_now)
    dog_claude.fail_resume = True
    dog = _dog(dog_ledger, Exits())
    dog.tick(dog_now)
    dog.tick(dog_now + timedelta(minutes=1))

    row = dog_ledger.conn.execute(
        "SELECT resume_started_at FROM agents WHERE agent_id = 'sess-oracle'"
    ).fetchone()
    assert row[0] is None


def test_a_failed_listing_does_not_count_as_no_session_running(
    dog_ledger: Ledger, dog_claude: FakeClaude, dog_now: datetime
) -> None:
    _run_id(dog_ledger)
    dog_ledger.run_pause("oracle", "sess-oracle", "waiting on the user")
    dog_claude.fail_list = True
    exits = Exits()
    dog = _dog(dog_ledger, exits)

    for minutes in (0, 20, 40, 60):
        dog.tick(dog_now + timedelta(minutes=minutes))

    assert exits.count == 0


def test_context_windows_follow_the_settings_by_family() -> None:
    windows = {"haiku": 500_000, "default": 2_000_000, "fable": 3_000_000}
    assert watchdog._context_window("claude-haiku-4-5") == 200_000
    assert watchdog._context_window("sonnet") == 1_000_000
    assert watchdog._context_window("claude-haiku-4-5", windows) == 500_000
    assert watchdog._context_window("sonnet", windows) == 2_000_000
    assert watchdog._context_window("fable-1", windows) == 3_000_000


# -- settings: pricing and context windows ------------------------------------------------------


def _load(tmp_path: Path, frontmatter: str) -> Settings:
    text = settings_text().replace("max_workers: 8\n", f"max_workers: 8\n{frontmatter}\n")
    return load_settings(make_host(tmp_path / "h", text))


def test_pricing_defaults_ship_unchanged(tmp_path: Path) -> None:
    settings = _load(tmp_path, "")
    assert settings.pricing == {}
    assert settings.watchdog.context_windows == {}
    assert pricing.PRICES == pricing.DEFAULT_PRICES
    assert pricing.DEFAULT_PRICES["sonnet"] == (2.0, 10.0, 0.10, 2.5, 4.0)


def test_a_pricing_override_replaces_only_the_rates_it_names(tmp_path: Path) -> None:
    settings = _load(tmp_path, "pricing:\n  sonnet:\n    input: 3\n    output: 15\n")
    assert pricing.PRICES["sonnet"] == (3.0, 15.0, 0.10, 2.5, 4.0)
    assert pricing.PRICES["opus"] == pricing.DEFAULT_PRICES["opus"]
    again = Settings.from_snapshot(settings.snapshot())
    assert again.pricing == {"sonnet": {"input": 3.0, "output": 15.0}}
    assert again == settings


def test_a_new_family_needs_all_five_rates_and_beats_a_shorter_match(tmp_path: Path) -> None:
    _load(
        tmp_path,
        "pricing:\n  opus-6:\n    input: 7\n    output: 35\n    cache_read: 1\n"
        "    cache_write_5m: 8\n    cache_write_1h: 9\n  partial:\n    input: 1\n",
    )
    assert "partial" not in pricing.PRICES
    assert pricing.estimate("claude-opus-6", {"input_tokens": 1_000_000}) == 7.0
    assert pricing.estimate("claude-opus-5-5", {"input_tokens": 1_000_000}) == 4.0


def test_bad_pricing_and_window_values_are_ignored(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "pricing:\n  sonnet:\n    input: cheap\n    output: -1\n  haiku: 3\nwatchdog_note: x\n",
    )
    assert settings.pricing == {}
    assert pricing.PRICES == pricing.DEFAULT_PRICES


def test_context_windows_load_from_the_watchdog_section(tmp_path: Path) -> None:
    text = settings_text().replace(
        "watchdog:\n", "watchdog:\n  context_windows:\n    haiku: 300000\n    bad: lots\n", 1
    )
    settings = load_settings(make_host(tmp_path / "h", text))
    assert settings.watchdog.context_windows == {"haiku": 300_000}
    assert Settings.from_snapshot(settings.snapshot()) == settings


def test_an_old_snapshot_without_the_new_keys_still_loads(tmp_path: Path) -> None:
    settings = _load(tmp_path, "")
    data = json.loads(settings.snapshot())
    del data["pricing"]
    del data["watchdog"]["context_windows"]
    restored = Settings.from_snapshot(json.dumps(data))
    assert restored.pricing == {} and restored.watchdog.context_windows == {}


# -- subprocess hygiene --------------------------------------------------------------------------


def test_graph_upsert_reports_a_timeout_as_a_ledger_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_graph import _graph_host, _kg

    host_root = _graph_host(tmp_path, Path(__file__).resolve().parents[2])

    def hang(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(command, 120)

    monkeypatch.setattr(graph, "codebase_kg_root", lambda _root: _kg(tmp_path))
    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(LedgerError, match="timed out"):
        graph.graph_upsert(host_root, [{"id": "n", "description": "d"}])


@pytest.mark.parametrize("module", [graph, repo])
def test_git_and_uv_output_decodes_as_utf8_with_replacement(
    module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if module is repo:
        repo._run_git(["status"], tmp_path)
    else:
        from test_graph import _graph_host, _kg

        monkeypatch.setattr(graph, "codebase_kg_root", lambda _root: _kg(tmp_path))
        graph.graph_upsert(
            _graph_host(tmp_path, Path(__file__).resolve().parents[2]),
            [{"id": "n", "description": "d"}],
        )
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert "text" not in seen


@pytest.mark.skipif(sys.platform != "win32", reason="Windows memory API")
def test_windows_working_set_is_read_with_typed_handles() -> None:
    assert (memory._windows_rss_mb(os.getpid()) or 0) > 0


# -- the claude launcher -------------------------------------------------------------------------


def test_a_native_binary_carries_a_message_unchanged() -> None:
    text = 'a "quoted" & %PATH% message'
    assert sessions.launcher_text(text, "/usr/bin/claude", windows=True) == text
    assert sessions.launcher_text(text, "C:/x/claude.exe", windows=True) == text
    assert sessions.launcher_text(text, "C:/x/claude.cmd", windows=False) == text


def test_a_cmd_shim_carries_a_message_as_plain_text(tmp_path: Path) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    sent = sessions.launcher_text(
        'coverage is 100%: say "hi" & calc | x <in >out ^ now!\nnext line', str(shim), windows=True
    )
    assert sent == "coverage is 100 percent: say 'hi' and calc / x (in )out now. next line"
    assert not sessions._CMD_META.search(sent)
    plain = "a plain message, with spaces"
    assert sessions.launcher_text(plain, str(shim), windows=True) == plain


def test_a_cmd_shim_gets_its_json_flags_as_files_and_no_cmd_characters(tmp_path: Path) -> None:
    config = '{"mcpServers": {"swarm-ledger": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}}'
    args = ["a plain prompt", "--bg", "--name", "host-lead", "--mcp-config", config]
    args += ["--settings", '{"worktree":{"bgIsolation":"none"}}', "--model", "sonnet"]

    sent = sessions.cmd_args(args, tmp_path)

    assert not any(sessions._CMD_META.search(arg) for arg in sent)
    written = Path(sent[5])
    assert written.parent == tmp_path / ".sentinel-swarm" / "launch"
    assert written.read_text(encoding="utf-8") == config
    assert Path(sent[7]).read_text(encoding="utf-8") == '{"worktree":{"bgIsolation":"none"}}'
    assert sessions.cmd_args(args, tmp_path) == sent
    with pytest.raises(LedgerError, match="cmd.exe"):
        sessions.cmd_args(["prompt", "--model", "sonnet & calc"], tmp_path)
    with pytest.raises(LedgerError, match="repo folder"):
        sessions.cmd_args(["--mcp-config", config], None)


def test_a_cmd_shim_runs_the_native_binary_npm_put_beside_it(tmp_path: Path) -> None:
    native = tmp_path.joinpath(*sessions._NPM_NATIVE)
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    shim = tmp_path / "claude.cmd"
    shim.write_text("", encoding="utf-8")
    assert sessions._launcher(str(shim), windows=True) == (str(native), False)
    assert sessions.launcher_text('evil" & calc', str(shim), windows=True) == 'evil" & calc'
