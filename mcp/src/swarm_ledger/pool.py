"""The ledger server's elastic pool of worker processes for tool calls and hooks.

Modeled on codebase-kg's `pool.py`. Each tool call and each `/hook` request goes to a worker,
a Python process that holds its own `Ledger`, over newline-delimited JSON on its stdin and
stdout. An idle worker takes the call. With none idle and fewer than `limit` running, the pool
starts one; otherwise the call waits for the next free worker. A worker idle for `idle` seconds
exits. A worker that exits or hangs during a call is killed, and only that call fails.

The front keeps the stamped `agent_id`, the watchdog, `on_run_finish` and the guard. A worker
never shows an OS notification itself: its reply lists the notifier commands, and the front
runs each one once.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ledger import Ledger

SRC = Path(__file__).resolve().parents[1]
IDLE_EXIT_S = 60.0
CALL_TIMEOUT_S = 1800.0
STOP_WAIT_S = 2.0
REPO_ROOT_METHOD = "repo_root"
_ENTRY = (
    "import sys; sys.path[:0] = sys.argv[5:]; "
    "from swarm_ledger.pool import worker_main; worker_main(*sys.argv[1:5])"
)


class WorkerError(RuntimeError):
    pass


class CallError(RuntimeError):
    pass


def worker_command(root: Path, db_path: Path, server_pid: int, settings: str) -> list[str]:
    return [
        base_python(),
        "-I",
        "-S",
        "-c",
        _ENTRY,
        str(root),
        str(db_path),
        str(server_pid),
        settings,
        str(SRC),
    ]


def base_python() -> str:
    # A Windows venv's python.exe is a launcher that starts the base interpreter as a second
    # process; the ledger imports only the standard library, so the base interpreter runs it.
    return getattr(sys, "_base_executable", None) or sys.executable


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def run_tool(ledger: Ledger, request: dict[str, Any]) -> Any:
    from .identity import LedgerError, require_bound, require_mod_session, require_role_tool

    tool = request.get("tool")
    agent_id = request.get("agent_id")
    if tool is not None:
        require_bound(ledger.conn, tool, agent_id)
        require_role_tool(ledger.conn, tool, agent_id)
        session_id = agent_id or (request.get("args") or {}).get("session_id")
        require_mod_session(ledger.conn, tool, session_id)
    method = request.get("method")
    if method == REPO_ROOT_METHOD:
        return str(ledger.repo_root)
    if not isinstance(method, str) or method.startswith("_") or not hasattr(ledger, method):
        raise LedgerError(f"unknown ledger method {method!r}")
    return getattr(ledger, method)(**(request.get("args") or {}))


def error_text(exc: Exception) -> str:
    from .identity import LedgerError

    return str(exc) if isinstance(exc, LedgerError) else f"{type(exc).__name__}: {exc}"


class _Worker:
    def __init__(self, command: list[str]) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=flags
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.stdin: IO[bytes] = self.proc.stdin
        self.stdout: IO[bytes] = self.proc.stdout
        self.replies: queue.Queue[bytes | None] = queue.Queue()
        self.idle_since = time.monotonic()
        threading.Thread(
            target=self._read, name=f"swarm-worker-{self.proc.pid}", daemon=True
        ).start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self) -> None:
        try:
            for line in self.stdout:
                self.replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)

    def call(self, request: bytes, timeout: float) -> dict[str, Any]:
        try:
            self.stdin.write(request)
            self.stdin.flush()
        except (OSError, ValueError):
            raise WorkerError(f"ledger worker {self.pid} exited before the call") from None
        try:
            line = self.replies.get(timeout=timeout)
        except queue.Empty:
            raise WorkerError(
                f"ledger worker {self.pid} did not answer within {timeout:g} s; it was stopped"
            ) from None
        if line is None:
            raise WorkerError(
                f"ledger worker {self.pid} exited during the call (exit code {self.proc.poll()})"
            )
        try:
            reply = json.loads(line)
        except ValueError:
            raise WorkerError(f"ledger worker {self.pid} sent a malformed reply") from None
        if not isinstance(reply, dict):
            raise WorkerError(f"ledger worker {self.pid} sent a malformed reply")
        return reply

    def stop(self) -> None:
        try:
            self.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class Pool:
    def __init__(
        self,
        limit: int,
        command: list[str],
        idle: float = IDLE_EXIT_S,
        timeout: float = CALL_TIMEOUT_S,
    ) -> None:
        if limit < 1:
            raise ValueError("a pool needs at least one worker")
        self.limit = limit
        self.command = command
        self.idle = idle
        self.timeout = timeout
        self._cond = threading.Condition()
        self._idle: list[_Worker] = []
        self._busy: set[_Worker] = set()
        self._starting = 0
        self._closed = False
        self._stopped = threading.Event()
        self._ids = itertools.count(1)
        threading.Thread(target=self._reap, name="swarm-pool-reaper", daemon=True).start()

    def size(self) -> int:
        with self._cond:
            return len(self._idle) + len(self._busy) + self._starting

    def pids(self) -> list[int]:
        with self._cond:
            return [w.pid for w in (*self._idle, *self._busy)]

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        line = json.dumps({**request, "id": next(self._ids)}).encode("utf-8") + b"\n"
        worker = self._acquire()
        healthy = False
        try:
            reply = worker.call(line, self.timeout)
            healthy = True
        finally:
            self._release(worker, healthy)
        return reply

    def _acquire(self) -> _Worker:
        with self._cond:
            while True:
                if self._closed:
                    raise WorkerError("the ledger worker pool is shut down")
                if self._idle:
                    worker = self._idle.pop()
                    self._busy.add(worker)
                    return worker
                if len(self._busy) + self._starting < self.limit:
                    self._starting += 1
                    break
                self._cond.wait()
        try:
            worker = _Worker(self.command)
        except OSError as exc:
            with self._cond:
                self._starting -= 1
                self._cond.notify()
            raise WorkerError(f"cannot start a ledger worker: {exc}") from exc
        with self._cond:
            self._starting -= 1
            self._busy.add(worker)
        return worker

    def _release(self, worker: _Worker, healthy: bool) -> None:
        keep = healthy and worker.proc.poll() is None
        with self._cond:
            self._busy.discard(worker)
            if keep and not self._closed:
                worker.idle_since = time.monotonic()
                self._idle.append(worker)
                self._cond.notify()
                return
            self._cond.notify()
        if keep:
            worker.stop()
        else:
            worker.kill()

    def _reap(self) -> None:
        tick = max(0.05, min(1.0, self.idle / 4))
        while not self._stopped.wait(tick):
            with self._cond:
                now = time.monotonic()
                expired = [w for w in self._idle if now - w.idle_since >= self.idle]
                self._idle = [w for w in self._idle if w not in expired]
            for worker in expired:
                worker.stop()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._stopped.set()
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        for worker in idle:
            worker.stop()


class _Worked:
    def __init__(self, root: Path, db_path: Path, server_pid: int, settings: str) -> None:
        self.root = root
        self.db_path = db_path
        self.server_pid = server_pid
        self.settings = settings
        self.notices: list[list[str]] = []
        self._ledger: Ledger | None = None

    def ledger(self) -> Ledger:
        if self._ledger is None:
            from .ledger import Ledger
            from .settings import Settings

            self._ledger = Ledger(
                self.root,
                db_path=self.db_path,
                server_pid=self.server_pid,
                settings=Settings.from_snapshot(self.settings),
            )
        return self._ledger

    def collect(self, argv: list[str]) -> None:
        self.notices.append(argv)

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("kind") == "hook":
            from .hooks import HANDLERS, run_event

            event = str(request.get("event"))
            if event not in HANDLERS:
                return {"ok": False, "unknown": True}
            stdout, stderr = run_event(event, str(request.get("payload") or ""), self.root)
            return {"ok": True, "result": stdout, "stderr": stderr}
        try:
            return {"ok": True, "result": run_tool(self.ledger(), request)}
        except Exception as exc:
            return {"ok": False, "error": error_text(exc)}

    def reply(self, line: bytes) -> bytes:
        self.notices = []
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("a request must be a JSON object")
            request_id = request.get("id")
            reply = self.answer(request)
        except Exception as exc:
            reply = {"ok": False, "error": f"malformed request: {exc}"}
        reply["id"] = request_id
        reply["notify"] = self.notices
        try:
            text = json.dumps(reply, ensure_ascii=False, default=_jsonable)
        except (TypeError, ValueError) as exc:
            text = json.dumps(
                {"id": request_id, "ok": False, "error": f"unserializable result: {exc}"}
            )
        return text.encode("utf-8") + b"\n"


def worker_main(root: str, db_path: str, server_pid: str, settings: str) -> None:
    # Replies go on a private copy of stdout, and fds 0 and 1 point elsewhere: a print or a
    # child process that inherits the standard handles would otherwise corrupt the protocol
    # or read the next request.
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    requests = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(sys.stderr.fileno(), 1)
    sys.stdout = sys.stderr

    from . import notify

    state = _Worked(Path(root), Path(db_path), int(server_pid), settings)
    notify.runner = state.collect
    for line in requests:
        if not line.strip():
            continue
        replies.write(state.reply(line))
        replies.flush()
