from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from .db import NOW
from .paths import config_dir

SESSION_MB = 300
CODER_CHILDREN_MB = 300
# A role session measured about 600 MB: two processes of about 150 MB and 450 MB.
SESSION_COST_MB = 600
MIN_PARALLELISM_CAP = 4
_COMMAND_TIMEOUT_S = 5
_LOG_TAIL = 512 * 1024
_RETIRE_LINE = re.compile(r"bg retire (\S+?):\s*(.+?)\s*$")
_VM_PAGE = re.compile(r"page size of (\d+) bytes")
_VM_LINE = re.compile(r"^(Pages [a-z ]+):\s+(\d+)\.?$", re.MULTILINE)
_VM_FREE = ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")


def free_mb() -> int | None:
    return platform_free_mb()


def platform_free_mb() -> int | None:
    try:
        if sys.platform == "win32":
            return _windows_free_mb()
        if sys.platform == "darwin":
            return _mac_free_mb()
        return _linux_free_mb()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _windows_memory_mb() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    mb = 1024 * 1024
    return int(status.ullAvailPhys // mb), int(status.ullTotalPhys // mb)


def _windows_free_mb() -> int | None:
    figures = _windows_memory_mb()
    return figures[0] if figures is not None else None


def total_mb() -> int | None:
    return platform_total_mb()


def platform_total_mb() -> int | None:
    try:
        if sys.platform == "win32":
            figures = _windows_memory_mb()
            return figures[1] if figures is not None else None
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    return int(pages * size // (1024 * 1024)) if pages > 0 and size > 0 else None


def default_parallelism_cap(total: int | None) -> int | None:
    if total is None:
        return None
    return max(MIN_PARALLELISM_CAP, total // 2 // SESSION_COST_MB)


def _mac_free_mb() -> int | None:
    output = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=_COMMAND_TIMEOUT_S, check=True
    ).stdout
    return parse_vm_stat(output)


def parse_vm_stat(output: str) -> int | None:
    page = _VM_PAGE.search(output)
    pages = {name: int(count) for name, count in _VM_LINE.findall(output)}
    if page is None or "Pages free" not in pages:
        return None
    free = sum(pages.get(name, 0) for name in _VM_FREE)
    return int(free * int(page.group(1)) // (1024 * 1024))


def _linux_free_mb() -> int | None:
    return parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))


def parse_meminfo(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def process_rss_mb(pid: int) -> int | None:
    try:
        if sys.platform == "win32":
            return _windows_rss_mb(pid)
        output = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
        ).stdout.strip()
        return int(output) // 1024 if output else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _windows_rss_mb(pid: int) -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return None
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(ProcessMemoryCounters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize // (1024 * 1024))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def session_pids() -> dict[str, int]:
    pids: dict[str, int] = {}
    folder = config_dir() / "sessions"
    with suppress(OSError):
        for path in folder.glob("*.json"):
            with suppress(OSError, ValueError):
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("sessionId") and path.stem.isdigit():
                    pids[str(data["sessionId"])] = int(path.stem)
    return pids


def default_mb(role: str) -> int:
    return SESSION_MB + (CODER_CHILDREN_MB if role == "coder" else 0)


def estimate_mb(conn: sqlite3.Connection, role: str) -> int:
    row = conn.execute("SELECT mean_mb FROM memory_estimates WHERE role = ?", (role,)).fetchone()
    if row is None:
        return default_mb(role)
    return int(row["mean_mb"]) + (CODER_CHILDREN_MB if role == "coder" else 0)


def estimates(conn: sqlite3.Connection, roles: tuple[str, ...]) -> dict[str, int]:
    return {role: estimate_mb(conn, role) for role in roles}


def record_sample(conn: sqlite3.Connection, role: str, rss_mb: int) -> None:
    conn.execute(
        "INSERT INTO memory_estimates (role, mean_mb, samples, updated_at) "
        f"VALUES (?, ?, 1, {NOW}) ON CONFLICT (role) DO UPDATE SET "
        "mean_mb = (mean_mb * samples + excluded.mean_mb) / (samples + 1), "
        f"samples = samples + 1, updated_at = {NOW}",
        (role, rss_mb),
    )


def retire_reason(bg_id: str | None, log_path: Path | None = None) -> str | None:
    if not bg_id:
        return None
    path = log_path or config_dir() / "daemon.log"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    reason = None
    for line in text.splitlines():
        match = _RETIRE_LINE.search(line)
        if match is not None and match.group(1) == bg_id:
            reason = match.group(2)
    return reason
