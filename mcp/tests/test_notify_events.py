from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import (
    SWARM_ROLES,
    FakeClaude,
    bootstrap_module,
    insert_passing_test_run,
    ledger_for,
    make_swarm_host,
    review_scores,
    role_template,
    settings_with_events,
    write_settings,
)

from swarm_ledger import frontmatter, notify, repo
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import (
    NOTIFY_EVENT_DEFAULTS,
    Settings,
    channels_for,
    load_settings,
)

EVENT_NAMES = [
    "needs_user",
    "run_stalled",
    "run_paused",
    "pause_reminder",
    "run_resumed",
    "oracle_unreachable",
    "rubric_floor",
    "driver",
    "phase_approved",
    "module_approved",
    "run_finished",
]

_ROLE_FILE = role_template(tools="Read, SendMessage, mcp__swarm-ledger", codebase_kg=False)
lifecycle = pytest.mark.usefixtures("claude_sessions")


def _load(tmp_path: Path, extra: str) -> Settings:
    write_settings(tmp_path, f"---\nbuild_command: npm run build\n{extra}---\n")
    return load_settings(tmp_path)


def _notices(ledger: Ledger) -> list[dict]:
    rows = ledger.conn.execute("SELECT * FROM notifications ORDER BY notification_id")
    return [dict(row) for row in rows]


def _kept(host: Path, **event_values: str) -> Ledger:
    write_settings(host, settings_with_events(event_values))
    return ledger_for(host)


def _push_demanded(ledger: Ledger, agent_id: str) -> bool:
    blocked = events.handle_stop(ledger, {"agent_id": agent_id})
    return blocked is not None and "PushNotification(" in blocked["reason"]


# -- Settings ---------------------------------------------------------------------------------


def test_the_defaults_turn_every_type_on_except_module_approved() -> None:
    assert list(NOTIFY_EVENT_DEFAULTS) == EVENT_NAMES
    assert {name for name, on in NOTIFY_EVENT_DEFAULTS.items() if not on} == {"module_approved"}


def test_a_missing_notify_events_block_uses_the_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path).notify_events == NOTIFY_EVENT_DEFAULTS
    assert _load(tmp_path, "notify_events:\n").notify_events == NOTIFY_EVENT_DEFAULTS
    assert _load(tmp_path, "notify_events: all\n").notify_events == NOTIFY_EVENT_DEFAULTS


def test_notify_events_parse_booleans_and_channel_lists(tmp_path: Path) -> None:
    settings = _load(
        tmp_path,
        "notify_events:\n"
        "  needs_user: false\n"
        "  driver: [os]\n"
        "  run_stalled: [PUSH, pager]\n"
        "  module_approved: true\n"
        "  run_resumed: []\n"
        "  made_up_event: true\n",
    )

    assert settings.notify_events["needs_user"] is False
    assert settings.notify_events["driver"] == ["os"]
    assert settings.notify_events["run_stalled"] == ["push"]
    assert settings.notify_events["module_approved"] is True
    assert settings.notify_events["run_resumed"] == []
    assert settings.notify_events["run_finished"] is True
    assert "made_up_event" not in settings.notify_events
    assert list(settings.notify_events) == EVENT_NAMES


@pytest.mark.parametrize("value", ["maybe", "7", "{os: true}", '""'])
def test_a_value_that_is_not_a_boolean_or_a_list_uses_the_default(
    tmp_path: Path, value: str
) -> None:
    settings = _load(tmp_path, f"notify_events:\n  driver: {value}\n  module_approved: {value}\n")

    assert settings.notify_events["driver"] is NOTIFY_EVENT_DEFAULTS["driver"]
    assert settings.notify_events["module_approved"] is NOTIFY_EVENT_DEFAULTS["module_approved"]


def test_a_single_channel_name_is_a_one_item_list(tmp_path: Path) -> None:
    assert _load(tmp_path, "notify_events:\n  driver: os\n").notify_events["driver"] == ["os"]


def test_the_notify_events_survive_a_snapshot_and_an_old_snapshot_gets_the_defaults(
    tmp_path: Path,
) -> None:
    settings = _load(tmp_path, "notify_events:\n  driver: [os]\n  needs_user: false\n")
    assert Settings.from_snapshot(settings.snapshot()).notify_events == settings.notify_events

    old = json.loads(settings.snapshot())
    del old["notify_events"]
    assert Settings.from_snapshot(json.dumps(old)).notify_events == NOTIFY_EVENT_DEFAULTS


def test_channels_for_resolves_true_false_and_a_list_against_the_default_channels() -> None:
    default = ["os", "push"]

    assert channels_for("driver", default, {"driver": True}) == ["os", "push"]
    assert channels_for("driver", ["os"], {"driver": True}) == ["os"]
    assert channels_for("driver", default, {"driver": False}) == []
    assert channels_for("driver", default, {"driver": ["os"]}) == ["os"]
    assert channels_for("driver", [], {"driver": ["push", "os"]}) == ["os", "push"]
    assert channels_for("driver", default, {"driver": ["pager"]}) == []


def test_channels_for_uses_the_default_for_a_missing_key() -> None:
    assert channels_for("driver", ["os", "push"], {}) == ["os", "push"]
    assert channels_for("module_approved", ["os", "push"], {}) == []
    assert channels_for("module_approved", ["os", "push"], None) == []


def test_settings_channels_for_combines_notify_with_the_event_switch(tmp_path: Path) -> None:
    settings = _load(
        tmp_path, "notify: [os]\nnotify_events:\n  driver: false\n  run_finished: [push]\n"
    )

    assert settings.channels_for("needs_user") == ["os"]
    assert settings.channels_for("driver") == []
    assert settings.channels_for("run_finished") == ["push"]


def test_the_settings_example_lists_every_type_with_a_comment(repo_root: Path) -> None:
    text = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text("utf-8")
    lines = text.splitlines()
    start = lines.index("notify_events:")

    for offset, name in enumerate(EVENT_NAMES, start=1):
        line = lines[start + 2 * offset]
        assert line.startswith(f"  {name}: "), (name, line)
        assert lines[start + 2 * offset - 1].startswith("  # "), name

    head, _ = frontmatter.split_document(text)
    parsed = frontmatter.parse(head)
    assert parsed["notify_events"] == NOTIFY_EVENT_DEFAULTS


# -- record and deliver -----------------------------------------------------------------------


def test_record_returns_the_notice_with_its_channels_and_stores_nothing_without_any(
    ledger: Ledger,
) -> None:
    run_id = ledger.run_start(prd="Build X", session_id="sess-1")["run"]["run_id"]
    with write_tx(ledger.conn) as conn:
        sent = notify.record(conn, run_id, kind="done", event_key="a", message="m", channels=["os"])
        none = notify.record(conn, run_id, kind="done", event_key="b", message="m", channels=[])

    assert sent is not None and sent["channels"] == ["os"] and sent["push_owed"] == 0
    assert none is None
    assert [n["event_key"] for n in _notices(ledger)] == ["a"]


def test_deliver_uses_the_channels_the_notice_carries() -> None:
    shown: list[list[str]] = []

    notify.deliver({"kind": "done", "message": "m", "channels": ["push"]}, run=shown.append)
    assert shown == []

    notify.deliver({"kind": "done", "message": "m", "channels": ["os", "push"]}, run=shown.append)
    assert len(shown) == 1


# -- Driver events through the ledger and the Stop hook ----------------------------------------


@pytest.fixture
def swarm_host(tmp_path: Path) -> Path:
    return make_swarm_host(
        tmp_path / "swarm",
        settings_with_events(),
        _ROLE_FILE,
        roles=(*SWARM_ROLES, "driver"),
        plugins=["cartographer@cartographer", "web-driver@accessibility-tools"],
    )


def _finish_exploration(ledger: Ledger, claude: FakeClaude) -> dict:
    claude.add("sess-oracle", "host-oracle")
    started = ledger.run_start(prd="Build X", session_id="sess-oracle")
    oracle_id = started["oracle"]["agent_id"]
    request = ledger.drive_request("oracle", oracle_id, "cover everything")
    name = f"driver-e{request['ordinal']}"
    ledger.brief_ack(name, request["agent_id"])
    ledger.drive_done(name, request["agent_id"], request["request_id"])
    ledger.watch_armed(1_800_000)
    return {"oracle_id": oracle_id, "run_id": started["run"]["run_id"]}


def test_a_driver_event_that_is_off_sends_neither_os_nor_push(
    swarm_host: Path, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(swarm_host, driver="false")

    ctx = _finish_exploration(ledger, claude)

    assert os_notifications == []
    assert _notices(ledger) == []
    assert notify.owed(ledger.conn, ctx["run_id"]) == []
    assert not _push_demanded(ledger, ctx["oracle_id"])


def test_a_driver_event_limited_to_os_shows_a_toast_and_the_stop_hook_asks_for_no_push(
    swarm_host: Path, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(swarm_host, driver="[os]")

    ctx = _finish_exploration(ledger, claude)

    assert len(os_notifications) == 1
    [notice] = _notices(ledger)
    assert notice["push_owed"] == 0
    assert notify.owed(ledger.conn, ctx["run_id"]) == []
    assert not _push_demanded(ledger, ctx["oracle_id"])


def test_a_driver_event_limited_to_push_owes_a_push_and_shows_no_toast(
    swarm_host: Path, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(swarm_host, driver="[push]")

    ctx = _finish_exploration(ledger, claude)

    assert os_notifications == []
    assert len(notify.owed(ledger.conn, ctx["run_id"])) == 1
    assert _push_demanded(ledger, ctx["oracle_id"])


def test_one_event_off_leaves_the_others_on_the_notify_channels(
    swarm_host: Path, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(swarm_host, needs_user="false")
    ctx = _finish_exploration(ledger, claude)

    assert len(os_notifications) == 1
    assert len(notify.owed(ledger.conn, ctx["run_id"])) == 1


def test_driver_unavailable_notifies_nothing_when_the_driver_switch_is_off(
    swarm_host: Path, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(swarm_host, driver="false")
    claude.add("sess-oracle", "host-oracle")
    started = ledger.run_start(prd="Build X", session_id="sess-oracle")

    result = ledger.drive_unavailable("oracle", started["oracle"]["agent_id"], "no browser")

    assert result["notification"] is None
    assert os_notifications == []


# -- Ledger events: needs_user and pauses ------------------------------------------------------


@lifecycle
def test_needs_user_off_sends_nothing_and_a_pause_still_does(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host, needs_user="false")
    started = ledger.run_start(prd="Build X", session_id="sess-1")

    ledger.user_ask("oracle", started["oracle"]["agent_id"], "Which database?")
    assert os_notifications == []
    assert _notices(ledger) == []

    ledger.run_pause("oracle", started["oracle"]["agent_id"], "the API key is missing")
    assert len(os_notifications) == 1
    assert [n["push_owed"] for n in _notices(ledger)] == [1]


@lifecycle
def test_run_paused_off_covers_the_oracles_pause_and_the_user_pause(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host, run_paused="false")
    started = ledger.run_start(prd="Build X", session_id="sess-1")

    ledger.run_pause("oracle", started["oracle"]["agent_id"], "the API key is missing")

    assert os_notifications == []
    assert _notices(ledger) == []
    assert ledger.conn.execute("SELECT state FROM runs").fetchone()[0] == "paused"


@lifecycle
def test_run_paused_limited_to_os_owes_no_push(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host, run_paused="[os]")
    started = ledger.run_start(prd="Build X", session_id="sess-1")

    ledger.run_pause("oracle", started["oracle"]["agent_id"], "the API key is missing")

    assert len(os_notifications) == 1
    assert [n["push_owed"] for n in _notices(ledger)] == [0]
    assert not _push_demanded(ledger, started["oracle"]["agent_id"])


# -- New types: phase, module, run ------------------------------------------------------------


def _accept_module(ledger: Ledger, ctx: dict) -> dict:
    insert_passing_test_run(ledger, ctx["run_id"], ctx["manager"]["agent_id"], "phase")
    return ledger.module_review(
        "mgr-p1-phase-1",
        ctx["manager"]["agent_id"],
        ctx["module_id"],
        "accepted",
        "looks good",
        scores=review_scores(),
    )


def _approve_phase(ledger: Ledger, ctx: dict) -> dict:
    _accept_module(ledger, ctx)
    ledger.agent_release("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["lead"]["agent_id"])
    ledger.phase_update("mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["phase_id"], "handed_up")
    insert_passing_test_run(ledger, ctx["run_id"], ctx["oracle_id"], "full")
    ledger.phase_review(
        "oracle", ctx["oracle_id"], ctx["phase_id"], "accepted", "ship it", scores=review_scores()
    )
    return ledger.phase_update("oracle", ctx["oracle_id"], ctx["phase_id"], "approved")


def _committed(monkeypatch: pytest.MonkeyPatch, result: dict) -> None:
    monkeypatch.setattr(repo, "commit_paths", lambda *args, **kwargs: result)


@lifecycle
def test_an_approved_phase_notifies_with_its_name_and_commit(
    host: Path, monkeypatch: pytest.MonkeyPatch, os_notifications: list[list[str]]
) -> None:
    _committed(monkeypatch, {"committed": True, "sha": "abc1234", "branch": "run", "paths": []})
    ledger = _kept(host)
    ctx = bootstrap_module(ledger)

    phase = _approve_phase(ledger, ctx)

    [notice] = [n for n in _notices(ledger) if n["event_key"].startswith("phase_approved:")]
    assert notice["event_key"] == f"phase_approved:{ctx['phase_id']}"
    assert notice["kind"] == "done"
    assert (
        notice["message"] == f"Phase {ctx['phase_id']} (p1-phase-1) approved, committed as abc1234"
    )
    assert notice["push_owed"] == 1
    assert phase["commit"]["committed"] is True
    assert len(os_notifications) == 1


@lifecycle
def test_an_approved_phase_that_did_not_commit_says_why(
    host: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _committed(monkeypatch, {"committed": False, "reason": "nothing to commit"})
    ledger = _kept(host)
    ctx = bootstrap_module(ledger)

    _approve_phase(ledger, ctx)

    [notice] = [n for n in _notices(ledger) if n["event_key"].startswith("phase_approved:")]
    assert notice["message"] == (
        f"Phase {ctx['phase_id']} (p1-phase-1) approved, not committed: nothing to commit"
    )
    assert notice["kind"] == "warning"


@lifecycle
def test_an_approved_phase_with_commit_phases_off_names_no_commit(host: Path) -> None:
    write_settings(
        host,
        settings_with_events(swaps=[("commit_phases: true", "commit_phases: false")]),
    )
    ledger = ledger_for(host)
    ctx = bootstrap_module(ledger)

    _approve_phase(ledger, ctx)

    [notice] = [n for n in _notices(ledger) if n["event_key"].startswith("phase_approved:")]
    assert notice["message"] == f"Phase {ctx['phase_id']} (p1-phase-1) approved"
    assert notice["kind"] == "done"


@lifecycle
def test_phase_approved_off_sends_nothing(host: Path, os_notifications: list[list[str]]) -> None:
    ledger = _kept(host, phase_approved="false", run_finished="false")
    ctx = bootstrap_module(ledger)

    _approve_phase(ledger, ctx)

    assert _notices(ledger) == []
    assert os_notifications == []


@lifecycle
def test_phase_approved_limited_to_os_owes_no_push(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host, phase_approved="[os]")
    ctx = bootstrap_module(ledger)

    _approve_phase(ledger, ctx)

    [notice] = [n for n in _notices(ledger) if n["event_key"].startswith("phase_approved:")]
    assert notice["push_owed"] == 0
    assert len(os_notifications) == 1
    assert not _push_demanded(ledger, ctx["oracle_id"])


@lifecycle
def test_an_accepted_module_sends_nothing_by_default(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host)
    ctx = bootstrap_module(ledger)

    _accept_module(ledger, ctx)

    assert _notices(ledger) == []
    assert os_notifications == []


@lifecycle
def test_an_accepted_module_notifies_when_module_approved_is_on(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host, module_approved="true")
    ctx = bootstrap_module(ledger)

    _accept_module(ledger, ctx)

    [notice] = _notices(ledger)
    assert notice["kind"] == "done"
    assert notice["message"] == f"Module {ctx['module_id']} (module-1) accepted by mgr-p1-phase-1"
    assert notice["push_owed"] == 1
    assert len(os_notifications) == 1


@lifecycle
def test_a_returned_module_sends_no_module_approved_notice(host: Path) -> None:
    ledger = _kept(host, module_approved="true")
    ctx = bootstrap_module(ledger)

    ledger.module_review(
        "mgr-p1-phase-1", ctx["manager"]["agent_id"], ctx["module_id"], "returned", "redo it"
    )

    assert _notices(ledger) == []


@lifecycle
def test_run_finish_notifies_the_outcome_and_the_oracle_stop_asks_for_the_push(
    host: Path, monkeypatch: pytest.MonkeyPatch, os_notifications: list[list[str]]
) -> None:
    _committed(monkeypatch, {"committed": False, "reason": "off"})
    ledger = _kept(host, phase_approved="false")
    ctx = bootstrap_module(ledger)
    _approve_phase(ledger, ctx)

    ledger.run_finish("oracle", ctx["oracle_id"], "all phases shipped")

    [notice] = _notices(ledger)
    assert notice["event_key"] == f"run_finished:{ctx['run_id']}"
    assert notice["kind"] == "done"
    assert notice["message"] == "The swarm run finished: all phases shipped"
    assert notice["push_owed"] == 1
    assert len(os_notifications) == 1
    assert _push_demanded(ledger, ctx["oracle_id"])


@lifecycle
def test_run_finished_off_sends_nothing(host: Path, os_notifications: list[list[str]]) -> None:
    ledger = _kept(host, phase_approved="false", run_finished="false")
    ctx = bootstrap_module(ledger)
    _approve_phase(ledger, ctx)

    ledger.run_finish("oracle", ctx["oracle_id"], "success")

    assert _notices(ledger) == []
    assert os_notifications == []
    assert not _push_demanded(ledger, ctx["oracle_id"])


@lifecycle
def test_run_close_notifies_with_the_outcome_and_who_closed_it(
    host: Path, os_notifications: list[list[str]]
) -> None:
    ledger = _kept(host)
    started = ledger.run_start(prd="Build X", session_id="sess-1")

    ledger.run_close("oracle", started["oracle"]["agent_id"], "stopped early")

    [notice] = _notices(ledger)
    assert notice["event_key"] == f"run_finished:{started['run']['run_id']}"
    assert notice["message"] == "The swarm run closed by oracle: stopped early"
    assert len(os_notifications) == 1
