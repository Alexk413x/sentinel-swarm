from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
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

from . import auth, lock, sessions
from .db import ledger_path
from .identity import LedgerError
from .pool import Pool, worker_command

if TYPE_CHECKING:
    from .settings import Settings

HOST = "127.0.0.1"
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"
HOOK_PATH = "/hook"
REPO_HEADER = "X-Sentinel-Swarm-Repo"
SERVER_FILE = "server.json"
PORT_FILE = "server.port"
LOG_FILE = "server.log"
EXIT_DELAY_S = 3.0
ORACLE_TURN_WAIT_S = 300.0
START_TIMEOUT_S = 30.0
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
        path.write_text(f"{sock.getsockname()[1]}\n", encoding="utf-8", newline="\n")
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
            "start one with python -m swarm_ledger.serve"
        )
    return str(info["url"])


def same_path(a: str, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b.resolve()))


def is_answering(info: dict[str, Any], repo_root: Path) -> bool:
    url = f"http://{HOST}:{info['port']}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return (
        isinstance(data, dict)
        and data.get("name") == "swarm-ledger"
        and same_path(str(data.get("repo_root") or ""), repo_root)
    )


def _answering_url(repo_root: Path) -> str | None:
    info = read_server_info(repo_root)
    if info is not None and is_answering(info, repo_root):
        return str(info["url"])
    return None


def ensure_server(repo_root: Path, timeout: float = START_TIMEOUT_S) -> str:
    root = repo_root.resolve()
    url = _answering_url(root)
    if url is not None:
        return url

    log_path = records_dir(root) / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "swarm_ledger.serve", "--repo", str(root)]
    with log_path.open("ab") as log:
        if sys.platform == "win32":
            # A hidden console, not DETACHED_PROCESS: a process with no console opens a new
            # window for every console program it runs, such as the watchdog's claude calls.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                creationflags=flags,
            )
        else:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = _answering_url(root)
        if url is not None:
            return url
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


def _remove_if_ours(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("pid") == os.getpid():
            path.unlink()
    except (OSError, json.JSONDecodeError):
        pass


def write_server_info(path: Path, info: dict[str, Any]) -> None:
    text = json.dumps(info)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    for _ in range(20):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            # Windows refuses to replace a file that a reader holds open.
            time.sleep(0.05)
    temp.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _shut_down(path: Path, repo_root: Path) -> None:
    # Lock first: once server.json is gone, a new server may start, and it needs the lock free.
    lock.release_owned(repo_root, os.getpid())
    _remove_if_ours(path)


def _exit_now(path: Path, repo_root: Path) -> None:
    _shut_down(path, repo_root)
    # os._exit, not sys.exit: this runs on a timer thread, and the HTTP server owns the main
    # thread.
    os._exit(0)


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
        {"name": "swarm-ledger", "repo_root": str(root), "pid": os.getpid()},
    )
    info = {
        "url": f"http://{HOST}:{port}{MCP_PATH}",
        "port": port,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    write_server_info(path, info)
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
    from . import env, front, watchdog
    from .db import connect

    watchdog.start(
        root,
        connect(env.db_path_for(root)),
        settings.watchdog,
        exit_server=lambda: _exit_now(path, root),
        activity=lambda: front.last_call_at,
        notify_channels=settings.notify,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarm_ledger.serve")
    parser.add_argument("--repo", type=Path, default=None, help="the host repo root")
    args = parser.parse_args(argv)
    # Imported here, not at the top: env imports ledger, which imports this module.
    from . import env

    root = (args.repo or env.repo_root()).resolve()
    url = _answering_url(root)
    if url is not None:
        print(url)
        return 0
    serve(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
