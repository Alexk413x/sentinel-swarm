from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, auth, lock, procs, sessions
from .agentfiles import LEDGER_SERVER
from .db import ledger_path, open_readonly
from .identity import LedgerError
from .paths import same_path, unlink_quiet
from .pool import Pool, base_python, hidden_console_flags, worker_command

if TYPE_CHECKING:
    from .settings import Settings

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = PLUGIN_ROOT / "mcp" / "launch" / "ledger.py"
HOST = "127.0.0.1"
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"
HOOK_PATH = "/hook"
PANEL_PATH = "/panel"
SHUTDOWN_PATH = "/shutdown"
REPO_HEADER = "X-Sentinel-Swarm-Repo"
SERVER_FILE = "server.json"
PORT_FILE = "server.port"
LOG_FILE = "server.log"
EXIT_DELAY_S = 3.0
ORACLE_TURN_WAIT_S = 300.0
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 10.0
_PID_REUSE_GRACE_MS = 60_000
_SHUTDOWN_DELAY_S = 0.2
_PROBE_TIMEOUT_S = 2.0
_LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_POLL_S = 0.2


def records_dir(repo_root: Path) -> Path:
    return ledger_path(repo_root).parent


def server_info_path(repo_root: Path) -> Path:
    return records_dir(repo_root) / SERVER_FILE


def port_path(repo_root: Path) -> Path:
    return records_dir(repo_root) / PORT_FILE


def _saved_port(repo_root: Path) -> int | None:
    try:
        port = int(port_path(repo_root).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def bind_socket(repo_root: Path) -> socket.socket:
    saved = _saved_port(repo_root)
    for port in (saved, 0) if saved is not None else (0,):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            # On Windows SO_REUSEADDR lets a second server take a port in use; elsewhere it
            # only lets the saved port be bound again while old connections sit in TIME_WAIT.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            sock.close()
            continue
        path = port_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(f"{sock.getsockname()[1]}\n")
        return sock
    raise OSError(f"cannot bind a port on {HOST}")


def read_server_info(repo_root: Path) -> dict[str, Any] | None:
    path = server_info_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("port"):
        return None
    return data


def server_url(repo_root: Path) -> str:
    info = read_server_info(repo_root)
    if info is None:
        raise LedgerError(
            f"no ledger server is recorded in {server_info_path(repo_root)}; "
            "start one with mcp/launch/ledger.py serve --detach"
        )
    return str(info["url"])


def health_info(repo_root: Path) -> dict[str, Any]:
    return {
        "name": LEDGER_SERVER,
        "repo_root": str(repo_root),
        "pid": os.getpid(),
        "version": __version__,
        "plugin_root": str(PLUGIN_ROOT),
    }


def _probe(info: dict[str, Any], repo_root: Path) -> dict[str, Any] | None:
    url = f"http://{HOST}:{info['port']}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if (
        isinstance(data, dict)
        and data.get("name") == LEDGER_SERVER
        and same_path(str(data.get("repo_root") or ""), repo_root)
    ):
        return data
    return None


def is_answering(info: dict[str, Any], repo_root: Path) -> bool:
    return _probe(info, repo_root) is not None


def _is_current(health: dict[str, Any]) -> bool:
    return health.get("version") == __version__ and same_path(
        health.get("plugin_root"), PLUGIN_ROOT
    )


def _run_is_active(repo_root: Path) -> bool:
    from . import env

    path = env.db_path_for(repo_root)
    if not path.is_file():
        return False
    try:
        conn = open_readonly(path)
        try:
            row = conn.execute("SELECT 1 FROM runs WHERE state = 'active' LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise LedgerError(f"cannot read the run state in {path}: {exc}") from None
    return row is not None


def _stale_message(health: dict[str, Any]) -> str:
    version = health.get("version") or "unknown"
    root = health.get("plugin_root") or "unknown"
    return (
        f"the ledger server that answers runs version {version} from {root}, "
        f"and this plugin is version {__version__} at {PLUGIN_ROOT}"
    )


def _request_shutdown(info: dict[str, Any], repo_root: Path) -> bool:
    try:
        token = auth.read_token(repo_root)
    except LedgerError:
        return False
    request = urllib.request.Request(
        f"http://{HOST}:{info['port']}{SHUTDOWN_PATH}",
        data=b"{}",
        method="POST",
        headers={auth.AUTHORIZATION: auth.bearer(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_S) as response:
            return bool(response.status == 200)
    except (OSError, ValueError):
        return False


def _started_ms(info: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(info["started_at"])).timestamp() * 1000
    except (KeyError, ValueError):
        return None


def _terminate_recorded(info: dict[str, Any], health: dict[str, Any]) -> bool:
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or health.get("pid") != pid:
        return False
    created = procs.created_of(pid)
    started = _started_ms(info)
    if created is None or started is None:
        return False
    # A pid that started after the server wrote server.json belongs to another process.
    if procs.created_epoch_ms(created) > started + _PID_REUSE_GRACE_MS:
        return False
    return procs.terminate(procs.Proc(pid, 0, created, ""))


def _wait_until_stopped(info: dict[str, Any], repo_root: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_answering(info, repo_root):
            return True
        time.sleep(_POLL_S)
    return not is_answering(info, repo_root)


def _stop_server(info: dict[str, Any], health: dict[str, Any], repo_root: Path) -> None:
    stopped = _request_shutdown(info, repo_root) and _wait_until_stopped(
        info, repo_root, STOP_TIMEOUT_S
    )
    if not stopped and _terminate_recorded(info, health):
        stopped = _wait_until_stopped(info, repo_root, STOP_TIMEOUT_S)
        if stopped:
            lock.release_owned(repo_root, int(info["pid"]))
            _remove_record(server_info_path(repo_root), int(info["pid"]))
    if not stopped:
        raise LedgerError(
            f"cannot stop the ledger server on port {info['port']} (pid {info.get('pid')}); "
            "stop it by hand, then launch again"
        )


def _replace_stale(
    repo_root: Path, info: dict[str, Any], health: dict[str, Any], replace_active: bool
) -> None:
    if not replace_active and _run_is_active(repo_root):
        raise LedgerError(
            f"{_stale_message(health)}, and a run is active, so the server stays up. "
            "Pause the run with `ledger.py pause --reason <text>`, then launch again."
        )
    _stop_server(info, health, repo_root)


def _answering(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    info = read_server_info(repo_root)
    health = _probe(info, repo_root) if info is not None else None
    return None if info is None or health is None else (info, health)


def _current_url(repo_root: Path, replace_active: bool = False) -> str | None:
    answering = _answering(repo_root)
    if answering is None:
        return None
    info, health = answering
    if _is_current(health):
        return str(info["url"])
    _replace_stale(repo_root, info, health, replace_active)
    return None


def ensure_server(
    repo_root: Path, timeout: float = START_TIMEOUT_S, *, replace_active: bool = False
) -> str:
    root = repo_root.resolve()
    url = _current_url(root, replace_active)
    if url is not None:
        return url

    log_path = records_dir(root) / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [base_python(), "-I", "-S", str(LAUNCHER), "serve", "--repo", str(root)]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            creationflags=hidden_console_flags(new_process_group=True),
            start_new_session=sys.platform != "win32",
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answering = _answering(root)
        if answering is not None and _is_current(answering[1]):
            return str(answering[0]["url"])
        exit_code = process.poll()
        if exit_code is not None and exit_code != 0:
            raise LedgerError(
                f"the ledger server exited with code {exit_code} before it answered; see {log_path}"
            )
        time.sleep(_POLL_S)
    raise LedgerError(f"the ledger server did not answer within {timeout:.0f}s; see {log_path}")


def _is_own_address(netloc: str, port: int) -> bool:
    try:
        parsed = urllib.parse.urlsplit(f"//{netloc}")
        return parsed.hostname in _LOOPBACK_HOSTS and parsed.port == port
    except ValueError:
        return False


def _is_own_origin(origin: str, port: int) -> bool:
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.path in ("", "/")
        and not parsed.query
        and _is_own_address(parsed.netloc, port)
    )


def refusal(
    client: str | None, headers: Mapping[str, str], path: str, token: str, port: int
) -> str | None:
    if client not in _LOOPBACK_CLIENTS:
        return "the ledger answers only local callers"
    if not _is_own_address(headers.get("host", ""), port):
        return f"the ledger answers only requests addressed to {HOST}:{port}"
    origin = headers.get("origin")
    if origin is not None and not _is_own_origin(origin, port):
        return "the ledger refuses requests from a web page"
    if path == HEALTH_PATH:
        return None
    presented = headers.get(auth.AUTHORIZATION.lower(), "").encode("latin-1", errors="replace")
    # 403, never 401: a 401 makes an MCP client start the OAuth flow of the MCP authorization
    # spec against the ledger.
    if not hmac.compare_digest(presented, auth.bearer(token).encode("ascii")):
        return "the ledger needs the bearer token in .sentinel-swarm/http-token"
    return None


def _remove_record(path: Path, pid: int) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict) and data.get("pid") == pid:
        unlink_quiet(path)


def _remove_if_ours(path: Path) -> None:
    _remove_record(path, os.getpid())


def write_server_info(path: Path, info: dict[str, Any]) -> None:
    text = json.dumps(info)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)
    for _ in range(20):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            # Windows refuses to replace a file that a reader holds open.
            time.sleep(0.05)
    temp.unlink(missing_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)


def _shut_down(path: Path, repo_root: Path) -> None:
    # Lock first: once server.json is gone, a new server may start, and it needs the lock free.
    lock.release_owned(repo_root, os.getpid())
    _remove_if_ours(path)


def _exit_now(path: Path, repo_root: Path) -> None:
    _shut_down(path, repo_root)
    # os._exit, not sys.exit: this runs on a timer thread, and the HTTP server owns the main
    # thread.
    os._exit(0)


def shutdown_later(path: Path, repo_root: Path, delay: float = _SHUTDOWN_DELAY_S) -> None:
    timer = threading.Timer(delay, _exit_now, args=(path, repo_root))
    timer.daemon = True
    timer.start()


def stop_finished_oracle(session_id: str | None, wait_s: float = ORACLE_TURN_WAIT_S) -> str:
    if session_id is None:
        return "no Oracle session recorded"
    deadline = time.monotonic() + wait_s
    while True:
        try:
            entry = sessions.find_session(sessions.list_sessions(), session_id)
        except LedgerError as exc:
            return f"Oracle session not checked: {exc}"
        if entry is None or not sessions.is_running(entry):
            return "Oracle session already ended"
        if entry.get("kind") != "background":
            return "Oracle session is interactive; left running"
        if str(entry.get("status") or "").lower() != "busy" or time.monotonic() >= deadline:
            sessions.stop(str(entry["id"]))
            return f"Oracle session {entry['id']} stopped"
        time.sleep(_POLL_S)


def _finish(path: Path, repo_root: Path, oracle_session_id: str | None) -> None:
    try:
        print(stop_finished_oracle(oracle_session_id), file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"Oracle session not stopped: {exc}", file=sys.stderr, flush=True)
    finally:
        _exit_now(path, repo_root)


def finish_later(
    path: Path, repo_root: Path, oracle_session_id: str | None, delay: float = EXIT_DELAY_S
) -> None:
    timer = threading.Timer(delay, _finish, args=(path, repo_root, oracle_session_id))
    timer.daemon = True
    timer.start()


def serve(repo_root: Path) -> None:
    from . import front
    from .http_front import LedgerHttpServer
    from .settings import load_settings

    root = repo_root.resolve()
    token = auth.ensure_token(root)
    sock = bind_socket(root)
    port = sock.getsockname()[1]
    path = server_info_path(root)
    settings = load_settings(root)
    workers, hook_workers = start_pool(root, settings), start_pool(root, settings)
    front.configure(root, workers, hook_workers)
    front.on_run_finish = lambda oracle_session_id: finish_later(path, root, oracle_session_id)
    httpd = LedgerHttpServer(
        sock,
        root,
        lambda client, headers, route: refusal(client, headers, route, token, port),
        health_info(root),
        lambda: shutdown_later(path, root),
    )
    info = {
        "url": f"http://{HOST}:{port}{MCP_PATH}",
        "port": port,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "plugin_root": str(PLUGIN_ROOT),
    }
    write_server_info(path, info)
    print(f"ledger server {__version__} from {PLUGIN_ROOT}", file=sys.stderr, flush=True)
    _start_watchdog(root, path, settings)
    try:
        httpd.serve_forever()
    finally:
        for pool in (workers, hook_workers):
            if pool is not None:
                pool.close()
        _shut_down(path, root)


def start_pool(root: Path, settings: Settings) -> Pool | None:
    from . import env

    if settings.max_workers < 1:
        return None
    return Pool(
        settings.max_workers,
        worker_command(root, env.db_path_for(root), os.getpid(), settings.snapshot()),
    )


def _start_watchdog(root: Path, path: Path, settings: Settings) -> None:
    from . import env, front, testrun, watchdog
    from .db import connect
    from .ledger import Ledger

    conn = connect(env.db_path_for(root))
    testrun.sweep(conn)
    watchdog.start(
        root,
        conn,
        settings.watchdog,
        exit_server=lambda: _exit_now(path, root),
        activity=lambda: front.last_call_at,
        notify_channels=settings.notify,
        notify_events=settings.notify_events,
        ledger=Ledger(root, db_path=env.db_path_for(root), settings=settings),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger.py serve")
    parser.add_argument("--repo", type=Path, default=None, help="the host repo root")
    parser.add_argument(
        "--detach",
        action="store_true",
        help="start the server in the background, wait until it answers, and print its URL",
    )
    args = parser.parse_args(argv)
    # Imported here, not at the top: env imports ledger, which imports this module.
    from . import env

    root = (args.repo or env.repo_root()).resolve()
    try:
        if args.detach:
            print(ensure_server(root))
            return 0
        url = _current_url(root)
    except LedgerError as exc:
        sys.stderr.write(f"cannot start the ledger server: {exc}\n")
        return 1
    if url is not None:
        print(url)
        return 0
    serve(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
