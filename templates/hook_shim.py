# Standard library only: this runs before the plugin's own environment is found.
# `python -m swarm_ledger.setup` copies it to .sentinel-swarm/hook.py and overwrites local edits.

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

PLUGIN_ID = "sentinel-swarm@sentinel-swarm"
GATING_EVENTS = frozenset(
    {"pre_agent", "pre_write", "pre_shell", "pre_ledger", "pre_monitor", "pre_send_message"}
)
HOOK_TIMEOUT_SECONDS = 50
SCOPES = ("local", "project", "user")
USAGE = (
    "usage: hook.py hook <event> | hook.py mcp <plugin_id> <server> | "
    "hook.py mcp-http <plugin_id> <server> <port> [<owner_pid>] | hook.py watch\n"
)
HTTP_HOST = "127.0.0.1"
_UV_VALUE_OPTIONS = frozenset(
    {
        "--project",
        "--directory",
        "--from",
        "--with",
        "-w",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--package",
        "--extra",
        "--group",
        "--only-group",
        "--no-group",
        "--env-file",
        "--index",
        "--default-index",
        "--index-url",
        "-i",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--cache-dir",
        "--config-file",
        "--color",
    }
)
_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_INFINITE = 0xFFFFFFFF
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ShimError(Exception):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else Path.home() / ".claude"


def registry_path() -> Path:
    return config_dir() / "plugins" / "installed_plugins.json"


def _same_path(raw: object, repo: Path) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        left = Path(raw).resolve()
        right = repo.resolve()
    except OSError:
        return False
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def find_install(plugin_id: str, repo: Path, registry: Path | None = None) -> Path:
    registry = registry or registry_path()
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ShimError(f"the plugin registry {registry} does not exist") from None
    except (OSError, ValueError) as exc:
        raise ShimError(f"cannot read the plugin registry {registry}: {exc}") from None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not isinstance(entries, list):
        entries = []
    for scope in SCOPES:
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("scope") != scope:
                continue
            if scope != "user" and not _same_path(entry.get("projectPath"), repo):
                continue
            install = entry.get("installPath")
            if not isinstance(install, str) or not install:
                continue
            path = Path(install)
            if not path.is_dir():
                raise ShimError(f"{plugin_id} is registered at {path}, but that folder is missing")
            return path
    raise ShimError(f"{plugin_id} is not installed for {repo}")


def plugin_servers(install: Path) -> dict[str, dict]:
    mcp_json = install / ".mcp.json"
    manifest = install / ".claude-plugin" / "plugin.json"
    try:
        if mcp_json.is_file():
            servers = json.loads(mcp_json.read_text(encoding="utf-8")).get("mcpServers")
        elif manifest.is_file():
            servers = json.loads(manifest.read_text(encoding="utf-8")).get("mcpServers")
        else:
            servers = None
    except (OSError, ValueError, AttributeError) as exc:
        raise ShimError(f"cannot read the MCP servers of {install}: {exc}") from None
    if not isinstance(servers, dict):
        return {}
    return {name: entry for name, entry in servers.items() if isinstance(entry, dict)}


def expand(text: str, plugin_root: str, env: Mapping[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name == "CLAUDE_PLUGIN_ROOT":
            return plugin_root
        value = env.get(name, "")
        if value:
            return value
        return default if default is not None else ""

    return _VARIABLE.sub(substitute, text)


def failure_answer(event: str, reason: str) -> dict:
    if event in GATING_EVENTS:
        message = f"sentinel-swarm cannot check this call: {reason}; run /sentinel-swarm:setup"
        return {
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            },
        }
    return {
        "systemMessage": f"sentinel-swarm hook {event} did not run: {reason}; "
        "run /sentinel-swarm:setup"
    }


def ledger_command(repo: Path, module: str, *args: str) -> tuple[list[str], dict[str, str]]:
    install = find_install(PLUGIN_ID, repo)
    project = install / "mcp"
    if not (project / "pyproject.toml").is_file():
        raise ShimError(f"the ledger code is missing from {project}")
    uv = shutil.which("uv")
    if uv is None:
        raise ShimError("uv is not on PATH")
    env = dict(os.environ)
    env.setdefault("CLAUDE_PROJECT_DIR", str(repo))
    env["CLAUDE_PLUGIN_ROOT"] = str(install)
    command = [uv, "run", "--project", str(project), "--frozen", "--no-dev"]
    command += ["python", "-m", module, *args]
    return command, env


def run_ledger_hook(event: str, payload: bytes) -> bytes:
    repo = repo_root()
    command, env = ledger_command(repo, "swarm_ledger.hooks", event)
    try:
        done = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            timeout=HOOK_TIMEOUT_SECONDS,
            env=env,
            cwd=str(repo),
        )
    except subprocess.TimeoutExpired:
        raise ShimError(
            f"the ledger hook {event} timed out after {HOOK_TIMEOUT_SECONDS} s"
        ) from None
    except OSError as exc:
        raise ShimError(f"cannot start uv: {exc}") from None
    if done.stderr:
        sys.stderr.write(done.stderr.decode("utf-8", errors="replace"))
    if done.returncode != 0:
        raise ShimError(f"the ledger hook {event} exited with status {done.returncode}")
    return done.stdout


def hook_main(event: str) -> int:
    payload = sys.stdin.buffer.read()
    try:
        output = run_ledger_hook(event, payload)
    except Exception as exc:
        sys.stdout.write(json.dumps(failure_answer(event, str(exc))))
        return 0
    sys.stdout.buffer.write(output)
    sys.stdout.flush()
    return 0


def server_command(plugin_id: str, server: str) -> tuple[list[str], dict[str, str]]:
    install = find_install(plugin_id, repo_root())
    entry = plugin_servers(install).get(server)
    if entry is None:
        raise ShimError(f"{plugin_id} at {install} has no MCP server named {server}")
    if not isinstance(entry.get("command"), str) or not entry["command"]:
        raise ShimError(f"the MCP server {server} of {plugin_id} has no command to run")
    root = str(install)
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = root
    extra = entry.get("env")
    if isinstance(extra, dict):
        for key, value in extra.items():
            env[str(key)] = expand(str(value), root, os.environ)
    command = expand(entry["command"], root, os.environ)
    args = entry.get("args")
    args = [expand(str(arg), root, os.environ) for arg in args] if isinstance(args, list) else []
    executable = find_executable(command, env.get("PATH"))
    if executable is None:
        raise ShimError(f"{command} is not on PATH")
    return [executable, *args], env


def find_executable(command: str, path: str | None) -> str | None:
    # Python 3.12's shutil.which returns an extensionless file first on Windows, and a
    # plugin's POSIX launcher beside its .cmd twin fails there with WinError 193.
    if os.name == "nt" and not Path(command).suffix:
        for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep):
            found = shutil.which(command + ext, path=path)
            if found is not None:
                return found
    return shutil.which(command, path=path)


def _script_index(args: list[str], start: int) -> int:
    index = start
    while index < len(args):
        arg = args[index]
        if arg == "--":
            if index + 1 < len(args):
                return index + 1
            break
        if not arg.startswith("-"):
            return index
        index += 2 if "=" not in arg and arg in _UV_VALUE_OPTIONS else 1
    raise ShimError("the command names no console script to run")


def http_command(command: list[str], port: int, owner_pid: int) -> list[str]:
    executable, args = command[0], command[1:]
    name = Path(executable).name.lower()
    tool = name[:-4] if name.endswith(".exe") else name
    if tool == "uv" and args[:1] == ["run"]:
        index = _script_index(args, 1)
    elif tool == "uvx":
        index = _script_index(args, 0)
    else:
        raise ShimError(
            f"only a uv run or uvx command can be served over HTTP, not {Path(executable).name}"
        )
    script, head = args[index], args[:index]
    if tool == "uvx" and not any(arg == "--from" or arg.startswith("--from=") for arg in head):
        head = ["--from", script, *head]
    entry = ["python", str(Path(__file__).resolve()), "mcp-entry", str(port), str(owner_pid)]
    return [executable, *head, *entry, script, *args[index + 1 :]]


def mcp_main(plugin_id: str, server: str) -> int:
    try:
        command, env = server_command(plugin_id, server)
    except ShimError as exc:
        sys.stderr.write(f"sentinel-swarm cannot start the MCP server {server}: {exc}\n")
        return 1
    try:
        return subprocess.call(command, env=env)
    except OSError as exc:
        sys.stderr.write(
            f"sentinel-swarm cannot start the MCP server {server} of {plugin_id}: {exc}\n"
        )
        return 1


def mcp_http_main(plugin_id: str, server: str, port: int, owner_pid: int) -> int:
    try:
        command, env = server_command(plugin_id, server)
        command = http_command(command, port, owner_pid)
    except ShimError as exc:
        sys.stderr.write(f"sentinel-swarm cannot serve the MCP server {server} over HTTP: {exc}\n")
        return 1
    try:
        return subprocess.call(command, env=env)
    except OSError as exc:
        sys.stderr.write(
            f"sentinel-swarm cannot serve the MCP server {server} of {plugin_id}: {exc}\n"
        )
        return 1


def console_script(script: str):
    from importlib import metadata

    for point in metadata.entry_points(group="console_scripts"):
        if point.name == script:
            return point
    raise ShimError(f"{sys.executable} has no console script named {script}")


def serve_entry(port: int, script: str, args: list[str]) -> int:
    import importlib

    point = console_script(script)
    module = importlib.import_module(point.module)
    server = getattr(module, "mcp", None)
    run = getattr(server, "run", None)
    if server is None or not callable(run):
        raise ShimError(f"{point.module} has no server named mcp with a run method")

    def run_http(*_args: object, **kwargs: object) -> object:
        # Stateless: the ledger restarts a crashed server on the same port, and a stateful
        # server answers a session id that its previous run issued with 404.
        kwargs.update(transport="http", host=HTTP_HOST, port=port, stateless_http=True)
        return run(**kwargs)

    server.run = run_http
    sys.argv = [script, *args]
    result = point.load()()
    return result if isinstance(result, int) else 0


def owner_gone(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = kernel32.OpenProcess(_SYNCHRONIZE, 0, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER
        kernel32.WaitForSingleObject(handle, _INFINITE)
        return True
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(1.0)


def _exit_with_owner(pid: int) -> None:
    if owner_gone(pid):
        os._exit(0)


def entry_main(port: int, owner_pid: int, script: str, args: list[str]) -> int:
    # The owner watch stops a server whose ledger died without stopping it: on POSIX,
    # nothing kills a process group when the process that started it dies.
    if owner_pid > 0:
        threading.Thread(target=_exit_with_owner, args=(owner_pid,), daemon=True).start()
    try:
        return serve_entry(port, script, args)
    except ShimError as exc:
        sys.stderr.write(f"sentinel-swarm cannot serve {script} over HTTP: {exc}\n")
        return 1


def watch_main() -> int:
    repo = repo_root()
    try:
        command, env = ledger_command(repo, "swarm_ledger.watch", "--repo", str(repo))
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=env, cwd=str(repo)
        )
    except (ShimError, OSError) as exc:
        sys.stdout.write(
            f"sentinel-swarm watchdog cannot start: {exc}; run /sentinel-swarm:setup\n"
        )
        sys.stdout.flush()
        return 1
    assert process.stdout is not None
    for line in iter(process.stdout.readline, b""):
        sys.stdout.buffer.write(line)
        sys.stdout.flush()
    return process.wait()


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "hook":
        return hook_main(argv[1])
    if len(argv) == 3 and argv[0] == "mcp":
        return mcp_main(argv[1], argv[2])
    if len(argv) in (4, 5) and argv[0] == "mcp-http" and all(a.isdigit() for a in argv[3:]):
        owner = int(argv[4]) if len(argv) == 5 else 0
        return mcp_http_main(argv[1], argv[2], int(argv[3]), owner)
    if len(argv) >= 4 and argv[0] == "mcp-entry" and argv[1].isdigit() and argv[2].isdigit():
        return entry_main(int(argv[1]), int(argv[2]), argv[3], argv[4:])
    if argv == ["watch"]:
        return watch_main()
    sys.stderr.write(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
