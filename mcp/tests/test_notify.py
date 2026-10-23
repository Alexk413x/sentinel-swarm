from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from helpers import (
    SWARM_ROLES,
    FakeClaude,
    ledger_for,
    make_swarm_host,
    role_template,
    write_settings,
)

from swarm_ledger import notify, wake, watch, watchdog
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger
from swarm_ledger.settings import WatchdogSettings, load_settings

_ROLE_FILE = role_template(
    tools="Read, SendMessage, mcp__swarm-ledger", permission_mode="default", codebase_kg=False
)

_FINDING = {
    "fingerprint": "contrast:login-button:2.1",
    "title": "Login button fails contrast",
    "severity": "major",
    "area": "login screen",
}


def _settings(extra: str = "") -> str:
    return f"---\nbuild_command: npm run build\n{extra}---\n"


def _write_settings(root: Path, extra: str = "") -> None:
    write_settings(root, _settings(extra))


@pytest.fixture
def host(tmp_path: Path) -> Path:
    return make_swarm_host(
        tmp_path / "host",
        _settings(),
        _ROLE_FILE,
        roles=(*SWARM_ROLES, "driver"),
        plugins=["cartographer@cartographer", "web-driver@accessibility-tools"],
    )


def _open_request(ledger: Ledger, claude: FakeClaude) -> dict:
    claude.add("sess-oracle", "host-oracle")
    started = ledger.run_start(prd="Build X", session_id="sess-oracle")
    oracle_id = started["oracle"]["agent_id"]
    request = ledger.drive_request("oracle", oracle_id, "cover everything")
    ledger.brief_ack(f"driver-e{request['ordinal']}", request["agent_id"])
    return {
        "oracle_id": oracle_id,
        "run_id": started["run"]["run_id"],
        "request_id": request["request_id"],
        "driver_id": request["agent_id"],
        "driver": f"driver-e{request['ordinal']}",
    }


def _seed_exploration(ledger: Ledger, run_id: int, ordinal: int, fingerprint: str) -> None:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, focus, state, done_at) "
            "VALUES (?, ?, 'focus', 'done', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (run_id, ordinal),
        )
        conn.execute(
            "INSERT INTO drive_findings (request_id, run_id, fingerprint, title, severity, area) "
            "VALUES (?, ?, ?, ?, 'major', 'screen-a')",
            (cur.lastrowid, run_id, fingerprint, fingerprint),
        )


def _notifications(ledger: Ledger) -> list[dict]:
    rows = ledger.conn.execute("SELECT * FROM notifications ORDER BY notification_id")
    return [dict(row) for row in rows]


# -- Settings ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("", ["os", "push"]),
        ("notify:\n", ["os", "push"]),
        ("notify: []\n", []),
        ('notify: ""\n', []),
        ("notify: [os]\n", ["os"]),
        ("notify: push\n", ["push"]),
        ("notify: [PUSH, os, pager]\n", ["os", "push"]),
    ],
)
def test_notify_setting(tmp_path: Path, line: str, expected: list[str]) -> None:
    (tmp_path / ".claude").mkdir()
    _write_settings(tmp_path, line)
    assert load_settings(tmp_path).notify == expected


def test_the_settings_example_turns_both_paths_on(repo_root: Path) -> None:
    text = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text("utf-8")
    assert "\nnotify: [os, push]\n" in text


# -- Message shape and OS commands ------------------------------------------------------------


def test_one_line_flattens_strips_markdown_and_caps_the_length() -> None:
    assert notify.one_line("Driver  done:\n`x` **bold**") == "Driver done: x bold"
    long = notify.one_line("word " * 100)
    assert len(long) <= notify.MAX_CHARS
    assert long.endswith("...")


def test_push_call_quotes_the_message() -> None:
    assert notify.push_call('Say "hi" — now') == (
        'PushNotification(message="Say \\"hi\\" — now", status="proactive")'
    )


def _toast(message: str, **kwargs: object) -> str:
    argv = notify.os_command(message, platform="win32", **kwargs)  # type: ignore[arg-type]
    assert argv is not None
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    return base64.b64decode(argv[4]).decode("utf-16-le")


def test_windows_command_is_an_encoded_toast_under_the_swarm_app_id(tmp_path: Path) -> None:
    icon, logo = tmp_path / "icon.png", tmp_path / "icon-error.png"
    script = _toast("Driver <done> & 'clean'", icon=icon, logo=logo)
    assert "ToastNotificationManager" in script
    assert rf"'HKCU:\Software\Classes\AppUserModelId\{notify.APP_ID}'" in script
    assert "-Name DisplayName -Value 'Sentinel Swarm'" in script
    assert f"-Name IconUri -Value '{icon}'" in script
    assert f"CreateToastNotifier('{notify.APP_ID}')" in script
    assert f'placement="appLogoOverride" src="{logo.as_uri()}"' in script
    assert "Driver &lt;done&gt; &amp; &apos;clean&apos;" in script
    assert "'clean'" not in script


@pytest.mark.parametrize("level", ["success", "warning", "error"])
def test_each_level_shows_its_own_logo(level: str) -> None:
    script = _toast("hi", level=level)
    assert f"icon-{level}.png" in script
    assert f"IconUri -Value '{notify.ASSETS / 'icon.png'}'" in script


@pytest.mark.parametrize(
    "kind, level", [("done", "success"), ("warning", "warning"), ("error", "error")]
)
def test_deliver_shows_the_level_for_the_notice_kind(
    kind: str, level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake_command(message: str, *, level: str | None = None) -> list[str]:
        seen.append(level)
        return ["notifier", message]

    monkeypatch.setattr(notify, "os_command", fake_command)
    notify.deliver({"kind": kind, "message": "m"}, ["os"], run=lambda argv: None)
    assert seen == [level]


def test_windows_toast_without_icon_files_sets_no_icon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notify, "ASSETS", tmp_path)
    script = _toast("hi", level="error")
    assert "IconUri" not in script
    assert "appLogoOverride" not in script


def test_the_plugin_ships_its_icons() -> None:
    for name in ("icon", "icon-success", "icon-warning", "icon-error"):
        assert (notify.ASSETS / f"{name}.png").is_file()


def test_macos_command_escapes_the_applescript_string() -> None:
    argv = notify.os_command('Driver "done"', platform="darwin")
    assert argv == [
        "osascript",
        "-e",
        'display notification "Driver \\"done\\"" with title "Sentinel Swarm"',
    ]


def test_linux_command_names_the_app_and_needs_notify_send() -> None:
    icon = Path("/opt/swarm/assets/icon-success.png")
    found = notify.os_command(
        "hi", platform="linux", which=lambda _: "/usr/bin/notify-send", logo=icon
    )
    assert found == [
        "notify-send",
        "--app-name",
        "Sentinel Swarm",
        "--icon",
        str(icon),
        "Sentinel Swarm",
        "hi",
    ]
    assert notify.os_command("hi", platform="linux", which=lambda _: None) is None


def test_show_logs_a_runner_failure_and_never_raises() -> None:
    logs: list[str] = []

    def broken(argv: list[str]) -> None:
        raise OSError("no shell")

    notify.show("hi", run=broken, log=logs.append)
    assert logs and "no shell" in logs[0]


def test_run_logs_a_missing_program_and_a_failed_exit() -> None:
    logs: list[str] = []
    notify._run(["sentinel-swarm-no-such-program"], log=logs.append)
    notify._run([sys.executable, "-c", "import sys; sys.exit(3)"], log=logs.append)
    assert len(logs) == 2
    assert "exit code 3" in logs[1]


def test_the_default_runner_is_replaced_in_tests(os_notifications: list[list[str]]) -> None:
    notify.show("hi")
    assert len(os_notifications) == 1


# -- Recording and dedupe ---------------------------------------------------------------------


def test_record_keeps_one_row_per_event(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    with write_tx(ledger.conn) as conn:
        first = notify.record(
            conn, ctx["run_id"], kind="done", event_key="k", message="a", channels=["push"]
        )
        again = notify.record(
            conn, ctx["run_id"], kind="done", event_key="k", message="b", channels=["push"]
        )
    assert first is not None and first["push_owed"] == 1
    assert again is None


# -- Driver events -----------------------------------------------------------------------------


def test_a_clean_exploration_notifies_done_once(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ctx = _open_request(ledger, claude)
    result = ledger.drive_done(ctx["driver"], ctx["driver_id"], ctx["request_id"])

    assert result["notification"]["message"] == "Driver done: exploration 1 clean, 0 open bugs"
    assert [(n["kind"], n["push_owed"]) for n in _notifications(ledger)] == [("done", 1)]
    assert len(os_notifications) == 1
    assert notify.owed(ledger.conn, ctx["run_id"])[0]["event_key"] == (
        f"drive_done:{ctx['request_id']}"
    )


def test_an_exploration_with_findings_and_no_stop_rule_notifies_nothing(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_issue(ctx["driver"], ctx["driver_id"], ctx["request_id"], _FINDING)
    result = ledger.drive_done(ctx["driver"], ctx["driver_id"], ctx["request_id"])

    assert result["notification"] is None
    assert _notifications(ledger) == []
    assert os_notifications == []


def test_a_stalled_loop_notifies_a_warning_with_what_is_left(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    claude.add("sess-oracle", "host-oracle")
    started = ledger.run_start(prd="Build X", session_id="sess-oracle")
    run_id, oracle_id = started["run"]["run_id"], started["oracle"]["agent_id"]
    for ordinal in (1, 2, 3):
        _seed_exploration(ledger, run_id, ordinal, "F1")
    request = ledger.drive_request("oracle", oracle_id, "recheck F1")
    ledger.brief_ack("driver-e4", request["agent_id"])
    ledger.drive_issue(
        "driver-e4", request["agent_id"], request["request_id"], {**_FINDING, "fingerprint": "F1"}
    )
    ledger.drive_done("driver-e4", request["agent_id"], request["request_id"])

    [notice] = _notifications(ledger)
    assert notice["kind"] == "warning"
    assert notice["message"] == (
        "Driver loop ended: 3 explorations in a row fixed nothing; 1 left: Login button fails "
        "contrast (+1 more)"
    )
    assert len(os_notifications) == 1


def test_blocked_needs_a_recorded_finding(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    with pytest.raises(LedgerError, match="drive_issue first"):
        ledger.drive_done(ctx["driver"], ctx["driver_id"], ctx["request_id"], blocked="no build")


def test_blocked_notifies_an_error(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ctx = _open_request(ledger, claude)
    ledger.drive_issue(ctx["driver"], ctx["driver_id"], ctx["request_id"], _FINDING)
    ledger.drive_done(
        ctx["driver"], ctx["driver_id"], ctx["request_id"], blocked="  the build failed "
    )

    [notice] = _notifications(ledger)
    assert (notice["kind"], notice["message"]) == (
        "error",
        "Driver blocked: exploration 1 cannot continue: the build failed",
    )
    assert len(os_notifications) == 1


@pytest.mark.parametrize(
    "line, shown, push_owed",
    [("notify: []\n", 0, 0), ("notify: [push]\n", 0, 1), ("notify: [os]\n", 1, 0)],
)
def test_the_notify_setting_picks_the_paths(
    host: Path,
    claude: FakeClaude,
    os_notifications: list[list[str]],
    line: str,
    shown: int,
    push_owed: int,
) -> None:
    _write_settings(host, line)
    ledger = ledger_for(host)
    ctx = _open_request(ledger, claude)
    ledger.drive_done(ctx["driver"], ctx["driver_id"], ctx["request_id"])

    assert len(os_notifications) == shown
    assert [n["push_owed"] for n in _notifications(ledger)] == [push_owed]
    assert len(notify.owed(ledger.conn, ctx["run_id"])) == push_owed


# -- Watchdog events -----------------------------------------------------------------------------


def _dog(ledger: Ledger, logs: list[str]) -> watchdog.Watchdog:
    return watchdog.Watchdog(
        ledger.repo_root,
        ledger.conn,
        WatchdogSettings(),
        exit_server=lambda: None,
        activity=lambda: 0,
        log=logs.append,
    )


def test_a_crashed_driver_notifies_once(
    ledger: Ledger, claude: FakeClaude, os_notifications: list[list[str]]
) -> None:
    ctx = _open_request(ledger, claude)
    claude.listing[:] = [e for e in claude.listing if e["sessionId"] != ctx["driver_id"]]
    dog = _dog(ledger, [])

    dog.tick(watchdog.utcnow())
    dog.tick(watchdog.utcnow())

    [notice] = _notifications(ledger)
    directive_id = ledger.conn.execute(
        "SELECT directive_id FROM watchdog_findings WHERE agent_id = ? AND kind = 'crashed'",
        (ctx["driver_id"],),
    ).fetchone()["directive_id"]
    assert notice["event_key"] == f"directive:{directive_id}"
    assert notice["message"].startswith(f"Driver crashed: {ctx['driver']}, the ledger says")
    assert len(os_notifications) == 1


def test_a_watchdog_report_on_another_role_notifies_nothing(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _open_request(ledger, claude)
    reported = [
        {"agent_id": ctx["oracle_id"], "kind": "crashed", "detail": "x", "directive_id": 1},
        {"agent_id": ctx["driver_id"], "kind": "context_high", "detail": "x", "directive_id": 2},
    ]
    assert watchdog.driver_notices(ledger.conn, ctx["run_id"], reported, ["os", "push"]) == []


# -- The Oracle's push debt ------------------------------------------------------------------------


def _owe_push(ledger: Ledger, claude: FakeClaude) -> dict:
    ctx = _open_request(ledger, claude)
    ledger.drive_done(ctx["driver"], ctx["driver_id"], ctx["request_id"])
    ledger.watch_armed(1_800_000)
    return ctx


def test_the_oracle_stop_blocks_until_a_push_call(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _owe_push(ledger, claude)
    call = notify.push_call("Driver done: exploration 1 clean, 0 open bugs")

    blocked = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert blocked is not None and call in blocked["reason"]

    events.handle_post_any(
        ledger,
        {
            "agent_id": ctx["oracle_id"],
            "tool_name": "PushNotification",
            "tool_input": {"message": "reworded by the Oracle", "status": "proactive"},
            "tool_response": "Not sent — this terminal is active",
        },
    )
    assert notify.owed(ledger.conn, ctx["run_id"]) == []
    after = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert after is None or "PushNotification" not in after["reason"]


def test_the_push_debt_blocks_past_a_needs_user_directive_and_a_pause(
    ledger: Ledger, claude: FakeClaude
) -> None:
    ctx = _owe_push(ledger, claude)
    directive = ledger.directive_submit("user_chat", "alex", "Ask me first.")
    ledger.directive_resolve(
        "oracle", ctx["oracle_id"], directive["directive_id"], "needs_user", "Which one?"
    )
    blocked = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert blocked is not None and blocked["reason"].startswith("You owe the user")

    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET state = 'paused'")
    paused = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert paused is not None and "PushNotification(" in paused["reason"]


def test_the_push_debt_lets_a_repeat_stop_through(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _owe_push(ledger, claude)
    data = {"agent_id": ctx["oracle_id"], "stop_hook_active": True}
    assert events.handle_stop(ledger, data) is None


def test_the_push_debt_outlives_run_finish(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _owe_push(ledger, claude)
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE runs SET state = 'finished'")

    blocked = events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]})
    assert blocked is not None and "PushNotification(" in blocked["reason"]

    events.handle_post_any(
        ledger,
        {"agent_id": ctx["oracle_id"], "tool_name": "PushNotification", "tool_input": {}},
    )
    assert events.handle_stop(ledger, {"agent_id": ctx["oracle_id"]}) is None


def test_a_push_pays_the_matching_debt_first(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _open_request(ledger, claude)
    with write_tx(ledger.conn) as conn:
        for key in ("a", "b"):
            notify.record(
                conn, ctx["run_id"], kind="error", event_key=key, message=key, channels=["push"]
            )

    assert notify.mark_sent(ledger.conn, ctx["run_id"], "b") is not None
    assert [n["message"] for n in notify.owed(ledger.conn, ctx["run_id"])] == ["a"]


def test_a_push_from_another_role_pays_nothing(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _owe_push(ledger, claude)
    events.handle_post_any(
        ledger,
        {"agent_id": ctx["driver_id"], "tool_name": "PushNotification", "tool_input": {}},
    )
    assert len(notify.owed(ledger.conn, ctx["run_id"])) == 1


def test_the_monitor_prints_an_owed_push_once(ledger: Ledger, claude: FakeClaude) -> None:
    ctx = _owe_push(ledger, claude)
    now = watchdog.utcnow()

    _, lines = watch.poll(ledger.conn, now)
    assert lines == [
        wake.timed(
            "Notification 1 is owed to the user: "
            + notify.push_call("Driver done: exploration 1 clean, 0 open bugs"),
            wake.time_signal(ledger.conn, ctx["run_id"], now),
        )
    ]
    assert watch.poll(ledger.conn, now) == (True, [])
