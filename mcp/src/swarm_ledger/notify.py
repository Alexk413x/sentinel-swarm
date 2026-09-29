from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from .clock import stamp, utcnow
from .db import write_tx

TITLE = "Sentinel Swarm"
MAX_CHARS = 199
RUN_TIMEOUT_S = 30.0
# Windows shows a toast only for a registered AppUserModelID. A key under
# HKCU\Software\Classes\AppUserModelId registers one without a shortcut or admin rights.
# Windows caches an ID's name and icon the first time it shows a toast, so a changed
# name or icon needs a new ID.
APP_ID = "SentinelSwarm.Notifications"
ASSETS = Path(__file__).resolve().parents[3] / "assets"
LEVELS = {"done": "success", "warning": "warning", "error": "error"}

Runner = Callable[[list[str]], None]
Log = Callable[[str], None]


def _log(message: str) -> None:
    sys.stderr.write(f"{stamp(utcnow())} notify: {message}\n")
    sys.stderr.flush()


def one_line(text: str, limit: int = MAX_CHARS) -> str:
    flat = " ".join(text.replace("`", "").replace("*", "").split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def push_call(message: str) -> str:
    quoted = json.dumps(message, ensure_ascii=False)
    return f'PushNotification(message={quoted}, status="proactive")'


def _asset(name: str) -> Path | None:
    path = ASSETS / name
    return path if path.is_file() else None


def _toast_script(title: str, message: str, icon: Path | None, logo: Path | None) -> str:
    from xml.sax.saxutils import escape

    def text(value: str) -> str:
        return escape(value, {'"': "&quot;", "'": "&apos;"})

    def ps(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    image = f'<image placement="appLogoOverride" src="{text(logo.as_uri())}"/>' if logo else ""
    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{text(message)}</text>{image}"
        "</binding></visual></toast>"
    )
    key = rf"HKCU:\Software\Classes\AppUserModelId\{APP_ID}"
    register = [
        f"New-Item -Path {ps(key)} -Force | Out-Null",
        f"Set-ItemProperty -Path {ps(key)} -Name DisplayName -Value {ps(title)}",
    ]
    if icon:
        register.append(f"Set-ItemProperty -Path {ps(key)} -Name IconUri -Value {ps(str(icon))}")
    return "\n".join(
        [
            *register,
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null",
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] | Out-Null",
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
            f"$xml.LoadXml({ps(xml)})",
            "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            f"{ps(APP_ID)}).Show($toast)",
        ]
    )


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def os_command(
    message: str,
    *,
    level: str | None = None,
    title: str = TITLE,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    icon: Path | None = None,
    logo: Path | None = None,
) -> list[str] | None:
    platform = platform or sys.platform
    icon = icon or _asset("icon.png")
    logo = logo or (_asset(f"icon-{level}.png") if level else None) or icon
    if platform == "win32":
        # Windows PowerShell, not pwsh: only 5.1 loads WinRT types with ContentType.
        script = _toast_script(title, message, icon, logo)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    if platform == "darwin":
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        return ["osascript", "-e", script]
    if which("notify-send") is None:
        return None
    argv = ["notify-send", "--app-name", title]
    if logo:
        argv += ["--icon", str(logo)]
    return [*argv, title, message]


def _run(argv: list[str], log: Log = _log) -> None:
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUN_TIMEOUT_S,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUN_TIMEOUT_S,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"the OS notification failed: {exc}")
        return
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())[:300]
        log(f"the OS notification failed with exit code {result.returncode}: {detail}")


def _run_detached(argv: list[str]) -> None:
    threading.Thread(target=_run, args=(argv,), name="swarm-notify", daemon=True).start()


runner: Runner = _run_detached


def show(
    message: str, *, level: str | None = None, run: Runner | None = None, log: Log = _log
) -> None:
    try:
        argv = os_command(message, level=level)
        if argv is None:
            log("no OS notifier is installed (notify-send); the OS notification was skipped")
            return
        (run or runner)(argv)
    except Exception as exc:
        log(f"the OS notification failed: {type(exc).__name__}: {exc}")


def record(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    kind: str,
    event_key: str,
    message: str,
    channels: Sequence[str],
) -> dict | None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications (run_id, kind, event_key, message, push_owed) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, kind, event_key, one_line(message), 1 if "push" in channels else 0),
    )
    if cur.rowcount == 0:
        return None
    return dict(
        conn.execute(
            "SELECT * FROM notifications WHERE notification_id = ?", (cur.lastrowid,)
        ).fetchone()
    )


def deliver(
    notice: dict | None,
    channels: Sequence[str],
    *,
    run: Runner | None = None,
    log: Log = _log,
) -> None:
    if notice is not None and "os" in channels:
        show(notice["message"], level=LEVELS.get(notice["kind"]), run=run, log=log)


def owed(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM notifications WHERE run_id = ? AND push_owed = 1 AND sent_at IS NULL "
            "ORDER BY notification_id",
            (run_id,),
        )
    ]


def mark_sent(conn: sqlite3.Connection, run_id: int, message: str) -> int | None:
    pending = owed(conn, run_id)
    if not pending:
        return None
    wanted = one_line(message)
    match = next((n for n in pending if n["message"] == wanted), pending[0])
    with write_tx(conn):
        conn.execute(
            "UPDATE notifications SET sent_at = ? WHERE notification_id = ?",
            (stamp(utcnow()), match["notification_id"]),
        )
    return match["notification_id"]


def announce(conn: sqlite3.Connection, run_id: int, at: str) -> list[str]:
    rows = conn.execute(
        "SELECT notification_id, message FROM notifications WHERE run_id = ? AND push_owed = 1 "
        "AND sent_at IS NULL AND announced_at IS NULL ORDER BY notification_id",
        (run_id,),
    ).fetchall()
    conn.executemany(
        "UPDATE notifications SET announced_at = ? WHERE notification_id = ?",
        [(at, row["notification_id"]) for row in rows],
    )
    return [
        f"Notification {row['notification_id']} is owed to the user: {push_call(row['message'])}"
        for row in rows
    ]
