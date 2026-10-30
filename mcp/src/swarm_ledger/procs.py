from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_COMMAND_TIMEOUT_S = 10
_LSTART = "%a %b %d %H:%M:%S %Y"
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "fish", "cmd", "powershell", "pwsh"})
_KERNEL32: Any = None
_ENTRY: Any = None
_INVALID_HANDLE = 2**64 - 1 if sys.maxsize > 2**32 else 2**32 - 1


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    created: int | None
    name: str


def snapshot() -> dict[int, Proc]:
    try:
        if sys.platform == "win32":
            return _windows_snapshot()
        return _posix_snapshot()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def short_name(raw: str) -> str:
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def is_shell(proc: Proc) -> bool:
    return short_name(proc.name).lstrip("-") in SHELLS


def same_process(proc: Proc | None, created: int | None) -> bool:
    return proc is not None and (created is None or proc.created is None or proc.created == created)


def _born_after(child: Proc, parent_created: int | None) -> bool:
    # A ppid can name a dead process whose pid was reused; a real child starts after its parent.
    return parent_created is None or child.created is None or child.created >= parent_created


def children_map(table: dict[int, Proc]) -> dict[int, list[Proc]]:
    children: dict[int, list[Proc]] = {}
    for proc in table.values():
        if proc.ppid != proc.pid:
            children.setdefault(proc.ppid, []).append(proc)
    return children


def descendants(children: dict[int, list[Proc]], pid: int, created: int | None) -> list[Proc]:
    found: list[Proc] = []
    seen: set[int] = {pid}
    stack: list[tuple[int, int | None]] = [(pid, created)]
    while stack:
        parent, parent_created = stack.pop()
        for child in children.get(parent, []):
            if child.pid in seen or not _born_after(child, parent_created):
                continue
            seen.add(child.pid)
            found.append(child)
            stack.append((child.pid, child.created))
    return found


def tool_tree(table: dict[int, Proc], root_pid: int, root_created: int | None) -> list[Proc]:
    """Every process the session's tool shells started, and the shells themselves.

    A session's other children are its MCP servers. They exit with the session, and a plugin's
    relay among them can start the machine's one shared server, which other sessions use.
    """
    children = children_map(table)
    found: list[Proc] = []
    for child in children.get(root_pid, []):
        if is_shell(child) and _born_after(child, root_created):
            found.append(child)
            found += descendants(children, child.pid, child.created)
    return found


def survivors(
    table: dict[int, Proc], recorded: Iterable[tuple[int, int | None]], protected: set[int]
) -> list[Proc]:
    children = children_map(table)
    found: dict[int, Proc] = {}
    for pid, created in recorded:
        current = table.get(pid)
        if current is not None and same_process(current, created):
            found[pid] = current
        if current is None or same_process(current, created):
            for proc in descendants(children, pid, created):
                found.setdefault(proc.pid, proc)
    return [proc for pid, proc in found.items() if pid not in protected]


def ancestors(table: dict[int, Proc], pid: int) -> set[int]:
    found: set[int] = set()
    current = table.get(pid)
    while current is not None and current.pid not in found:
        found.add(current.pid)
        current = table.get(current.ppid)
    return found


def terminate(proc: Proc) -> bool:
    try:
        if sys.platform == "win32":
            return _windows_terminate(proc)
        current = snapshot().get(proc.pid)
        if not same_process(current, proc.created):
            return False
        os.kill(proc.pid, 9)
        return True
    except OSError:
        return False


def created_of(pid: int) -> int | None:
    try:
        if sys.platform == "win32":
            return _windows_created(pid)
        output = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_S,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout.strip()
        return _parse_lstart(output) if output else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _parse_lstart(text: str) -> int:
    return int(time.mktime(time.strptime(" ".join(text.split()), _LSTART)))


def parse_ps(output: str) -> dict[int, Proc]:
    table: dict[int, Proc] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            created = _parse_lstart(" ".join(parts[2:7]))
        except ValueError:
            continue
        table[pid] = Proc(pid, ppid, created, " ".join(parts[7:]))
    return table


def _posix_snapshot() -> dict[int, Proc]:
    output = subprocess.run(
        ["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "comm="],
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_S,
        env={**os.environ, "LC_ALL": "C"},
        check=True,
    ).stdout
    return parse_ps(output)


def _kernel32() -> Any:
    if sys.platform != "win32":
        return None
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    global _ENTRY
    _ENTRY = ProcessEntry32W
    # A private WinDLL: argtypes set on ctypes.windll.kernel32 would change it for every module.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword = wintypes.HANDLE, wintypes.DWORD
    k.CreateToolhelp32Snapshot.restype = handle
    k.CreateToolhelp32Snapshot.argtypes = [dword, dword]
    k.Process32FirstW.argtypes = [handle, ctypes.POINTER(ProcessEntry32W)]
    k.Process32NextW.argtypes = [handle, ctypes.POINTER(ProcessEntry32W)]
    k.OpenProcess.restype = handle
    k.OpenProcess.argtypes = [dword, wintypes.BOOL, dword]
    k.GetProcessTimes.argtypes = [handle, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    k.TerminateProcess.argtypes = [handle, wintypes.UINT]
    k.CloseHandle.argtypes = [handle]
    _KERNEL32 = k
    return k


def _windows_snapshot() -> dict[int, Proc]:
    if sys.platform != "win32":
        return {}
    import ctypes

    k = _kernel32()
    snap_process = 0x2
    handle = k.CreateToolhelp32Snapshot(snap_process, 0)
    if not handle or handle == _INVALID_HANDLE:
        return {}
    entries: list[tuple[int, int, str]] = []
    try:
        entry = _ENTRY()
        entry.dwSize = ctypes.sizeof(_ENTRY)
        ok = k.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            entries.append(
                (int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile))
            )
            ok = k.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k.CloseHandle(handle)
    return {pid: Proc(pid, ppid, _windows_created(pid), name) for pid, ppid, name in entries}


def _windows_handle_created(k: Any, handle: int) -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return None
    return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)


def _windows_created(pid: int) -> int | None:
    k = _kernel32()
    query_limited_information = 0x1000
    handle = k.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return None
    try:
        return _windows_handle_created(k, handle)
    finally:
        k.CloseHandle(handle)


def _windows_terminate(proc: Proc) -> bool:
    k = _kernel32()
    terminate_access, query_limited_information = 0x0001, 0x1000
    handle = k.OpenProcess(terminate_access | query_limited_information, False, proc.pid)
    if not handle:
        return False
    try:
        if proc.created is not None and _windows_handle_created(k, handle) != proc.created:
            return False
        return bool(k.TerminateProcess(handle, 1))
    finally:
        k.CloseHandle(handle)
