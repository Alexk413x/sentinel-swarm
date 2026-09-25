from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .agentfiles import OPTIONAL_SERVERS, plugin_installed
from .db import ledger_path
from .setup import SHIM_PATH

HOST = "127.0.0.1"
MCP_PATH = "/mcp"
PORTS_FILE = "shared-ports.json"
LOG_FILE = "server.log"
CODEBASE_KG = ("codebase-kg@codebase-kg", "codebase-kg")
START_TIMEOUT_S = 60.0
STOP_GRACE_S = 2.0
_PROBE_TIMEOUT_S = 2.0
_POLL_S = 0.2
_QUIET_ENV = {"FASTMCP_SHOW_SERVER_BANNER": "false", "FASTMCP_CHECK_FOR_UPDATES": "off"}

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class SharedServerError(Exception):
    pass


@dataclass
class SharedServer:
    name: str
    port: int
    process: subprocess.Popen[bytes]
    job: int | None = None

    @property
    def url(self) -> str:
        return server_url(self.port)


_lock = threading.Lock()
_running: list[SharedServer] = []
_stopping = False


def server_url(port: int) -> str:
    return f"http://{HOST}:{port}{MCP_PATH}"


def planned_servers(repo_root: Path) -> list[tuple[str, str]]:
    servers: list[tuple[str, str]] = [CODEBASE_KG]
    for plugin_id, names in OPTIONAL_SERVERS.items():
        if plugin_installed(repo_root, plugin_id):
            servers += [(plugin_id, name) for name in names]
    return servers


def ports_path(repo_root: Path) -> Path:
    return ledger_path(repo_root).parent / PORTS_FILE


def saved_ports(repo_root: Path) -> dict[str, int]:
    try:
        data = json.loads(ports_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): port for name, port in data.items() if isinstance(port, int) and 0 < port < 65536
    }


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":
            # Matches uvicorn's own bind, so a port that sits in TIME_WAIT counts as free.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def free_port(preferred: int | None, taken: set[int]) -> int:
    if preferred is not None and preferred not in taken and _port_is_free(preferred):
        return preferred
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HOST, 0))
            port = sock.getsockname()[1]
        if port not in taken:
            return port


def is_answering(url: str, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def server_command(repo_root: Path, plugin_id: str, name: str, port: int) -> list[str]:
    shim = repo_root / SHIM_PATH
    if not shim.is_file():
        raise SharedServerError(f"{shim} is missing; run /sentinel-swarm:setup")
    return [sys.executable, str(shim), "mcp-http", plugin_id, name, str(port), str(os.getpid())]


def _server_env() -> dict[str, str]:
    env = {**_QUIET_ENV, **os.environ}
    env.pop("VIRTUAL_ENV", None)
    return env


def spawn_tree(
    command: list[str], cwd: Path, log: IO[bytes] | int
) -> tuple[subprocess.Popen[bytes], int | None]:
    if sys.platform == "win32":
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=_server_env(),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return process, _kill_on_close_job(process.pid)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=_server_env(),
        start_new_session=True,
    )
    return process, None


def launch(repo_root: Path, plugin_id: str, name: str, port: int, log: IO[bytes]) -> SharedServer:
    process, job = spawn_tree(server_command(repo_root, plugin_id, name, port), repo_root, log)
    server = SharedServer(name, port, process, job)
    with _lock:
        if not _stopping:
            _running.append(server)
            return server
    stop_tree(server)
    raise SharedServerError("the ledger server is stopping")


def _kill_on_close_job(pid: int) -> int | None:
    # uv does not stop its Python child when it is killed, so each server's whole tree runs
    # in a job object. The kernel kills the job when the ledger's handle closes, on any exit.
    if sys.platform != "win32":
        return None
    import ctypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process = None
    try:
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise OSError(ctypes.get_last_error())
        process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process or not kernel32.AssignProcessToJobObject(job, process):
            raise OSError(ctypes.get_last_error())
    except OSError:
        kernel32.CloseHandle(job)
        return None
    finally:
        if process:
            kernel32.CloseHandle(process)
    return job


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise OSError("kernel32 exists only on Windows")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


def _terminate_job(job: int) -> None:
    kernel32 = _kernel32()
    kernel32.TerminateJobObject(job, 1)
    kernel32.CloseHandle(job)


def _signal_group(pid: int, force: bool) -> None:
    if sys.platform != "win32":
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _wait(process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def _stop_windows(server: SharedServer, grace: float) -> None:
    if server.job is not None:
        _terminate_job(server.job)
        server.job = None
    else:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(server.process.pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    _wait(server.process, grace)


def _stop_posix(server: SharedServer, grace: float) -> None:
    _signal_group(server.process.pid, force=False)
    _wait(server.process, grace)
    _signal_group(server.process.pid, force=True)
    _wait(server.process, grace)


def stop_tree(server: SharedServer, grace: float = STOP_GRACE_S) -> None:
    if sys.platform == "win32":
        _stop_windows(server, grace)
    else:
        _stop_posix(server, grace)


def stop_all() -> None:
    global _stopping
    with _lock:
        _stopping = True
        servers = list(_running)
        _running.clear()
    for server in servers:
        try:
            stop_tree(server)
        except Exception as exc:
            _note(f"shared MCP server {server.name} not stopped: {exc}")


def _note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _save_ports(repo_root: Path, ports: Mapping[str, int]) -> None:
    path = ports_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(ports)), encoding="utf-8", newline="\n")
    except OSError as exc:
        _note(f"shared MCP server ports not saved: {exc}")


def _await_answers(servers: list[SharedServer], timeout: float) -> dict[str, str]:
    urls: dict[str, str] = {}
    pending = list(servers)
    deadline = time.monotonic() + timeout
    while pending:
        for server in list(pending):
            if is_answering(server.url):
                urls[server.name] = server.url
                pending.remove(server)
            elif server.process.poll() is not None:
                _note(
                    f"shared MCP server {server.name} exited with code "
                    f"{server.process.returncode} before it answered"
                )
                stop_tree(server)
                pending.remove(server)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_S)
    for server in pending:
        _note(f"shared MCP server {server.name} did not answer within {timeout:.0f}s")
        stop_tree(server)
    return urls


def start_shared(
    repo_root: Path,
    record: Callable[[dict[str, str]], None],
    timeout: float = START_TIMEOUT_S,
) -> dict[str, str]:
    root = repo_root.resolve()
    previous = saved_ports(root)
    ports: dict[str, int] = {}
    started: list[SharedServer] = []
    log_path = ledger_path(root).parent / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        for plugin_id, name in planned_servers(root):
            try:
                port = free_port(previous.get(name), set(ports.values()))
                started.append(launch(root, plugin_id, name, port, log))
            except (OSError, SharedServerError) as exc:
                _note(f"shared MCP server {name} did not start: {exc}")
                continue
            ports[name] = port
    if ports:
        _save_ports(root, {**previous, **ports})
    urls = _await_answers(started, timeout)
    with _lock:
        if not _stopping:
            record(urls)
    return urls


def start_in_background(repo_root: Path, record: Callable[[dict[str, str]], None]) -> None:
    def run() -> None:
        try:
            start_shared(repo_root, record)
        except Exception as exc:
            _note(f"shared MCP servers did not start: {exc}")

    threading.Thread(target=run, name="shared-mcp-servers", daemon=True).start()


def answering_urls(servers: object) -> dict[str, str]:
    if not isinstance(servers, dict):
        return {}
    return {
        str(name): url
        for name, url in servers.items()
        if isinstance(url, str) and url and is_answering(url)
    }
