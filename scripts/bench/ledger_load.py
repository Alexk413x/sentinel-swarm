"""Load test for one ledger server: N fake agents of one run, each looping over ledger calls.

Starts a ledger server on a scratch git repo, registers a fake run with 16 Coders, and for each N
runs N Coders at once. Each Coder loops `--rounds` times over message_post (from the Lead to this
Coder, since a Coder has no message_post), message_inbox, issue_list, who_owns, tests_run (the
test command is an `echo`) and POST /hook/pre_write. Reports start time, server memory idle and
under load, per-call latency, the message_post to message_inbox latency, one hook round trip
through the hook shim as a role session runs it, each role's up-front `select:` bytes, and the
whole machine's CPU load.

    uv run --project mcp python scripts/bench/ledger_load.py [--agents 1,4,8,16] [--out FILE]

Memory figures are working sets on Windows and resident sets elsewhere.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import http.client
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "mcp" / "src"))
from swarm_ledger import lock, sessions  # noqa: E402
from swarm_ledger.ledger import Ledger  # noqa: E402
from swarm_ledger.serve import REPO_HEADER, server_info_path  # noqa: E402

MAX_AGENTS = 16
ROLES = ("oracle", "manager", "lead", "coder", "driver")
TOOL_PREFIX = "mcp__swarm-ledger__"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _system_times() -> tuple[int, int]:
    if sys.platform == "win32":
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        ticks = [(t.high << 32) | t.low for t in (idle, kernel, user)]
        return ticks[0], ticks[1] + ticks[2]
    with open("/proc/stat", encoding="ascii") as handle:
        fields = [int(x) for x in handle.readline().split()[1:]]
    return fields[3] + fields[4], sum(fields)


class CpuMeter:
    """Whole-machine CPU load between `__enter__` and `__exit__`, in percent."""

    pct: float = 0.0

    def __enter__(self) -> CpuMeter:
        self._start = _system_times()
        return self

    def __exit__(self, *_: object) -> None:
        idle, total = (b - a for a, b in zip(self._start, _system_times(), strict=True))
        self.pct = round(100.0 * (1 - idle / total), 1) if total else 0.0


def cpu_load(seconds: float = 3.0) -> float:
    with CpuMeter() as meter:
        time.sleep(seconds)
    return meter.pct


def _win_counters(handle: int) -> _Counters:
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
        handle, ctypes.byref(counters), counters.cb
    )
    return counters


def process_mb(pid: int) -> float | None:
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
        if not handle:
            return None
        try:
            return round(_win_counters(handle).WorkingSetSize / 2**20, 1)
        finally:
            kernel32.CloseHandle(handle)
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
        return round(int(out.stdout.strip()) / 1024, 1)
    except (OSError, ValueError):
        return None


def _stats(ms: list[float]) -> dict[str, float | int]:
    if not ms:
        return {"n": 0}
    s = sorted(ms)
    return {
        "n": len(s),
        "median_ms": round(statistics.median(s), 1),
        "p90_ms": round(s[min(len(s) - 1, int(len(s) * 0.9))], 1),
        "max_ms": round(s[-1], 1),
    }


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, creationflags=NO_WINDOW
    )


def make_host(root: Path) -> None:
    (root / ".claude").mkdir(parents=True)
    template = (PLUGIN / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: echo 1 passed {target}\n")
    text = text.replace("interval_seconds: 30", "interval_seconds: 3600")
    text = text.replace("notify: [os, push]", "notify: []")
    (root / ".claude" / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    for i in range(1, MAX_AGENTS + 1):
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / f"f{i}.py").write_text(f"VALUE = {i}\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=bench",
        "-c",
        "user.email=bench@example.invalid",
        "commit",
        "-q",
        "-m",
        "bench host",
    )


def register_run(root: Path) -> list[dict[str, str]]:
    """A run with one Manager, one Lead and 16 Coders, each Coder owning src/f<i>.py."""
    original = sessions._run
    sessions._run = lambda args, cwd=None: "[]"  # type: ignore[assignment]
    ledger = Ledger(root)
    try:
        started = ledger.run_start(prd="ledger load", session_id="bench-oracle")
        oracle_id = started["oracle"]["agent_id"]
        phase_id = ledger.phase_add("oracle", oracle_id, "phase-1")["phase_id"]
        ledger.phase_update("oracle", oracle_id, phase_id, "unlocked")
        ledger.brief_create(
            "oracle", oracle_id, "mgr-p1-phase-1", "manager", "opus", "Own it.", phase_id=phase_id
        )
        ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
        ledger.brief_ack("mgr-p1-phase-1", "mgr-agent")
        module_id = ledger.module_add("mgr-p1-phase-1", "mgr-agent", phase_id, "module-1")[
            "module_id"
        ]
        ledger.brief_create(
            "mgr-p1-phase-1",
            "mgr-agent",
            "lead-p1-module-1",
            "lead",
            "sonnet",
            "Own it.",
            module_id=module_id,
        )
        ledger.agent_register_start("lead-agent", "lead", parent_agent_id="mgr-agent")
        ledger.brief_ack("lead-p1-module-1", "lead-agent")
        coders = []
        for i in range(1, MAX_AGENTS + 1):
            name, path = f"coder-p1-module-1-f{i}", f"src/f{i}.py"
            claimed = ledger.claim_file("lead-p1-module-1", "lead-agent", path, None, name)
            ledger.brief_create(
                "lead-p1-module-1",
                "lead-agent",
                name,
                "coder",
                "sonnet",
                "Write it.",
                module_id=module_id,
                file_id=claimed["file_id"],
            )
            agent_id = f"{name}-agent"
            ledger.agent_register_start(agent_id, "coder", parent_agent_id="lead-agent")
            ledger.brief_ack(name, agent_id)
            coders.append({"name": name, "agent_id": agent_id, "path": path})
        lock.release(root, started["run"]["run_id"])
        return coders
    finally:
        ledger.conn.close()
        sessions._run = original


@contextlib.contextmanager
def ledger_server(root: Path) -> Iterator[dict[str, Any]]:
    info_path = server_info_path(root)
    log = (root / ".sentinel-swarm" / "server.log").open("ab")
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_ledger.serve", "--repo", str(root)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log,
        creationflags=NO_WINDOW,
        env=dict(os.environ, PYTHONPATH=str(PLUGIN / "mcp" / "src")),
    )
    try:
        deadline = time.monotonic() + 60
        while not info_path.is_file():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"no server.json; see {log.name}")
            time.sleep(0.005)
        to_server_json = (time.perf_counter() - started) * 1000
        port = json.loads(info_path.read_text(encoding="utf-8"))["port"]
        while True:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    break
            except OSError:
                pass
            finally:
                conn.close()
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"the server never answered; see {log.name}")
            time.sleep(0.01)
        to_health = (time.perf_counter() - started) * 1000
        yield {
            "pid": json.loads(info_path.read_text(encoding="utf-8"))["pid"],
            "port": port,
            "start_to_server_json_ms": round(to_server_json, 1),
            "start_to_health_ms": round(to_health, 1),
        }
    finally:
        proc.kill()
        proc.wait(timeout=10)
        log.close()


def post_hook(root: Path, port: int, event: str, data: dict[str, Any]) -> float:
    body = json.dumps(data).encode("utf-8")
    started = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(
            "POST",
            f"/hook/{event}",
            body=body,
            headers={
                "Content-Type": "application/json",
                REPO_HEADER: urllib.parse.quote(str(root)),
            },
        )
        response = conn.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(f"hook {event} answered {response.status}")
    finally:
        conn.close()
    return (time.perf_counter() - started) * 1000


class MemorySampler:
    def __init__(self, pid: int) -> None:
        self.pid, self.peak = pid, 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, process_mb(self.pid) or 0.0)
            self._stop.wait(0.1)

    def __enter__(self) -> MemorySampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


def _client(port: int) -> Client:
    return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp"))


async def load(root: Path, server: dict[str, Any], coders: list[dict[str, str]], rounds: int):
    port = server["port"]
    tool_ms: dict[str, list[float]] = {}
    hook_ms: list[float] = []
    post_to_inbox_ms: list[float] = []
    errors: list[str] = []
    clients = [_client(port) for _ in coders]
    for client in clients:
        await client.__aenter__()

    async def call(client: Client, tool: str, args: dict[str, Any]) -> Any:
        started = time.perf_counter()
        result = await client.call_tool(tool, args, raise_on_error=False)
        tool_ms.setdefault(tool, []).append((time.perf_counter() - started) * 1000)
        if result.is_error:
            errors.append(f"{tool}: {result.content}")
        return result

    async def agent(client: Client, coder: dict[str, str]) -> None:
        me = {"caller": coder["name"], "agent_id": coder["agent_id"]}
        lead = {"caller": "lead-p1-module-1", "agent_id": "lead-agent"}
        write = {
            "session_id": coder["agent_id"],
            "tool_name": "Write",
            "tool_input": {"file_path": str(root / coder["path"])},
        }
        for i in range(rounds):
            body = f"round {i} from {coder['name']}"
            posted = time.perf_counter()
            await call(client, "message_post", {**lead, "to_name": coder["name"], "body": body})
            inbox = await call(client, "message_inbox", me)
            if body in json.dumps(inbox.structured_content):
                post_to_inbox_ms.append((time.perf_counter() - posted) * 1000)
            else:
                errors.append(f"message_inbox for {coder['name']} missed {body!r}")
            await call(client, "issue_list", me)
            await call(client, "who_owns", {"path": coder["path"]})
            await call(
                client,
                "tests_run",
                {**me, "scope": "file", "target": coder["path"], "force": True},
            )
            try:
                hook_ms.append(await asyncio.to_thread(post_hook, root, port, "pre_write", write))
            except (OSError, RuntimeError) as exc:
                errors.append(f"pre_write: {exc}")

    try:
        with MemorySampler(server["pid"]) as sampler, CpuMeter() as meter:
            started = time.perf_counter()
            await asyncio.gather(
                *(agent(c, coder) for c, coder in zip(clients, coders, strict=True))
            )
            wall = (time.perf_counter() - started) * 1000
    finally:
        for client in clients:
            await client.__aexit__(None, None, None)

    every_call = [ms for values in tool_ms.values() for ms in values]
    return {
        "agents": len(coders),
        "rounds": rounds,
        "wall_ms": round(wall, 1),
        "cpu_pct": meter.pct,
        "server_peak_mb": sampler.peak,
        "tool_call": _stats(every_call),
        "tool_call_without_tests_run": _stats(
            [ms for tool, values in tool_ms.items() if tool != "tests_run" for ms in values]
        ),
        "per_tool": {tool: _stats(values) for tool, values in sorted(tool_ms.items())},
        "hook_pre_write": _stats(hook_ms),
        "message_post_to_inbox": _stats(post_to_inbox_ms),
        "errors": len(errors),
        "first_errors": errors[:5],
    }


def _base_python() -> str:
    return getattr(sys, "_base_executable", None) or sys.executable


def shim_hook(root: Path, coder: dict[str, str], reps: int) -> dict[str, Any]:
    """The hook as a role session runs it: one shim process per hook call, server fast path."""
    shim = root / ".sentinel-swarm" / "hook.py"
    shutil.copyfile(PLUGIN / "templates" / "hook_shim.py", shim)
    payload = json.dumps(
        {
            "session_id": coder["agent_id"],
            "tool_name": "Write",
            "tool_input": {"file_path": str(root / coder["path"])},
        }
    ).encode("utf-8")
    times, peaks = [], []
    for _ in range(reps):
        started = time.perf_counter()
        proc = subprocess.Popen(
            [_base_python(), str(shim), "hook", "pre_write"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        out, err = proc.communicate(payload)
        times.append((time.perf_counter() - started) * 1000)
        if proc.returncode != 0:
            raise RuntimeError(f"hook shim failed: {err.decode(errors='replace')[:300]}")
        if sys.platform == "win32":
            handle = int(proc._handle)  # type: ignore[attr-defined]
            peaks.append(_win_counters(handle).PeakWorkingSetSize / 2**20)
        del out
    return {
        "python": _base_python(),
        **_stats(times),
        "peak_working_set_mb": round(max(peaks), 1) if peaks else None,
    }


def _frontmatter_tools(text: str) -> list[str]:
    match = re.search(r"^tools:\s*(.+)$", text, re.MULTILINE)
    names = [t.strip() for t in match.group(1).split(",")] if match else []
    return [n[len(TOOL_PREFIX) :] for n in names if n.startswith(TOOL_PREFIX)]


def _select_tools(text: str) -> list[str]:
    match = re.search(r'ToolSearch\(query="select:([^"]+)"', text)
    names = match.group(1).split(",") if match else []
    return [n[len(TOOL_PREFIX) :] for n in names if n.startswith(TOOL_PREFIX)]


async def select_bytes(port: int) -> dict[str, Any]:
    async with _client(port) as client:
        tools = await client.list_tools()
    dumps = [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]
    full = sum(len(json.dumps(dump).encode()) for dump in dumps)
    loaded = ("name", "description", "inputSchema")
    size = {
        dump["name"]: len(json.dumps({key: dump.get(key) for key in loaded}).encode())
        for dump in dumps
    }
    roles: dict[str, Any] = {}
    for role in ROLES:
        text = (PLUGIN / "templates" / "agents" / f"{role}.md").read_text(encoding="utf-8")
        allowed, selected = _frontmatter_tools(text), _select_tools(text)
        roles[role] = {
            "allowed_tools": len(allowed),
            "allowed_bytes": sum(size.get(t, 0) for t in allowed),
            "select_tools": len(selected),
            "select_bytes": sum(size.get(t, 0) for t in selected),
        }
    return {
        "tools": len(size),
        "tools_list_bytes": full,
        "input_schema_bytes": sum(size.values()),
        "roles": roles,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", default="1,4,8,16")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--shim-reps", type=int, default=10)
    parser.add_argument("--out")
    args = parser.parse_args()
    agents = [int(n) for n in args.agents.split(",")]
    if max(agents) > MAX_AGENTS:
        raise SystemExit(f"--agents takes at most {MAX_AGENTS}")

    began = time.perf_counter()
    report: dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "cpu_pct_before": cpu_load(),
    }
    with tempfile.TemporaryDirectory(prefix="ledger-load-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve() / "host"
        root.mkdir()
        make_host(root)
        coders = register_run(root)
        with ledger_server(root) as server:
            time.sleep(1.0)
            report["server"] = {
                "start_to_server_json_ms": server["start_to_server_json_ms"],
                "start_to_health_ms": server["start_to_health_ms"],
                "idle_mb": process_mb(server["pid"]),
            }
            report["select"] = asyncio.run(select_bytes(server["port"]))
            report["load"] = {}
            for n in agents:
                report["load"][n] = asyncio.run(load(root, server, coders[:n], args.rounds))
            report["server"]["after_load_mb"] = process_mb(server["pid"])
            report["shim_hook_pre_write"] = shim_hook(root, coders[0], args.shim_reps)
    report["cpu_pct_after"] = cpu_load()
    report["bench_s"] = round(time.perf_counter() - began, 1)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
