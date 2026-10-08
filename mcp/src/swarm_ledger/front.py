"""What the ledger server's front process does with a tool call or a hook. Stdlib only.

The front sends each tool call to a pool worker, or, with `max_workers` 0, runs it here one at
a time under `_CALL_LOCK`. Hooks go to a second pool of the same size, so a long tool call such
as `tests_run` never holds up a hook; with `max_workers` 0 they run here on their own
connection. It keeps what must happen once per server: the activity clock the
watchdog reads, `on_run_finish`, and the OS notifications a worker's reply lists. Both the
lean HTTP front (`http_front.py`) and the fastmcp registrations in `server.py` call it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, notify
from .pool import REPO_ROOT_METHOD, CallError, Pool, WorkerError, error_text, run_tool

if TYPE_CHECKING:
    from .ledger import Ledger

_instance: Ledger | None = None
_root: Path | None = None
_pool: Pool | None = None
_hook_pool: Pool | None = None
_CALL_LOCK = threading.RLock()
on_run_finish: Callable[[str | None], None] | None = None
last_call_at = 0.0


class UnknownHook(LookupError):
    pass


def configure(root: Path, workers: Pool | None = None, hook_workers: Pool | None = None) -> None:
    global _instance, _root, _pool, _hook_pool
    _root = root
    _instance = None
    _pool = workers
    _hook_pool = hook_workers or workers


def _ledger() -> Ledger:
    global _instance
    if _instance is None:
        from . import env

        _instance = env.open_ledger(_root)
    return _instance


def _delivered(reply: dict[str, Any]) -> dict[str, Any]:
    for argv in reply.get("notify") or []:
        notify.runner(argv)
    return reply


def call_method(tool: str | None, agent_id: str | None, method: str, kwargs: dict[str, Any]) -> Any:
    global last_call_at
    last_call_at = time.monotonic()
    request = {"tool": tool, "method": method, "args": kwargs, "agent_id": agent_id}
    result: Any
    try:
        if _pool is not None:
            reply = _delivered(_pool.call(request))
            if not reply.get("ok"):
                raise CallError(str(reply.get("error") or "the worker reported an unnamed error"))
            result = reply.get("result")
        else:
            with _CALL_LOCK:
                result = run_tool(_ledger(), request)
    except CallError:
        raise
    except Exception as exc:
        raise CallError(error_text(exc)) from exc
    if method == "run_finish" and on_run_finish is not None:
        on_run_finish(_oracle_of(result["run_id"]))
    return result


def ledger_info(repo_root: str, tools: int) -> dict[str, Any]:
    return {
        "name": "swarm-ledger",
        "version": __version__,
        "status": "ready",
        "tools": tools,
        "repo_root": repo_root,
    }


def call_tool(name: str, args: dict[str, Any], agent_id: str | None, stamped: bool) -> Any:
    if name == "ledger_info":
        from .catalog import TOOLS

        return ledger_info(call_method(name, agent_id, REPO_ROOT_METHOD, {}), len(TOOLS))
    if name == "run_start":
        kwargs = {**args, "session_id": agent_id or args["session_id"]}
    elif name == "events":
        kwargs = {"agent_id": args.get("target_agent_id"), "limit": args.get("limit")}
    else:
        kwargs = {**args, "agent_id": agent_id} if stamped else dict(args)
    return call_method(name, agent_id, name, kwargs)


def run_hook(root: Path, event: str, payload: str) -> tuple[str, str]:
    if _hook_pool is None:
        from .hooks import HANDLERS, run_event

        if event not in HANDLERS:
            raise UnknownHook(event)
        return run_event(event, payload, root)
    try:
        reply = _delivered(_hook_pool.call({"kind": "hook", "event": event, "payload": payload}))
    except WorkerError as exc:
        return "", f"swarm_ledger.hooks {event}: {exc}\n"
    if reply.get("unknown"):
        raise UnknownHook(event)
    if not reply.get("ok"):
        return "", f"swarm_ledger.hooks {event}: {reply.get('error')}\n"
    return str(reply.get("result") or ""), str(reply.get("stderr") or "")


def _oracle_of(run_id: int) -> str | None:
    from . import env
    from .db import connect

    conn = connect(env.db_path_for(_root or env.repo_root()))
    try:
        row = conn.execute(
            "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' "
            "ORDER BY ended_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["agent_id"] if row is not None else None
