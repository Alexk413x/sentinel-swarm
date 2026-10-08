# Standard library only: this runs before the plugin's own environment is found.
# `python -m swarm_ledger.setup` copies it to .sentinel-swarm/hook.py and overwrites local edits.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ID = "sentinel-swarm@sentinel-swarm"
DATA_FOLDER = "sentinel-swarm-sentinel-swarm"
GATING_EVENTS = frozenset(
    {
        "pre_agent",
        "pre_write",
        "pre_shell",
        "pre_ledger",
        "pre_monitor",
        "pre_send_message",
        "pre_skill",
    }
)
HOOK_TIMEOUT_SECONDS = 50
SERVER_HOST = "127.0.0.1"
SERVER_FILE = "server.json"
TOKEN_FILE = "http-token"
HOOK_PATH = "/hook"
REPO_HEADER = "X-Sentinel-Swarm-Repo"
# Windows takes about 2 s to refuse a connection to a closed local port; a live local
# server accepts in well under a millisecond.
CONNECT_TIMEOUT_SECONDS = 0.25
FAST_TIMEOUT_SECONDS = 10.0
SLOW_EVENT_TIMEOUT_SECONDS = {"stop": 40.0, "session_end": 40.0}
STAMPED_EVENTS = frozenset({"post_activity"})
FIRED_AT_KEY = "sentinel_swarm_fired_at"
SCOPES = ("local", "project", "user")
USAGE = (
    "usage: hook.py hook <event> | hook.py mcp <plugin_id> <server> | hook.py watch "
    "| hook.py channel\n"
)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
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


def ledger_venv(project: Path) -> Path:
    # Mirrors mcp/ledger_venv.py: the hooks and the skills must share one venv.
    digest = hashlib.sha256((project / "uv.lock").read_bytes()).hexdigest()[:12]
    return config_dir() / "plugins" / "data" / DATA_FOLDER / f"venv-{digest}"


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
    try:
        venv = ledger_venv(project)
    except OSError as exc:
        raise ShimError(f"cannot read the ledger lock file in {project}: {exc}") from None
    uv = shutil.which("uv")
    if uv is None:
        raise ShimError("uv is not on PATH")
    env = dict(os.environ)
    env.setdefault("CLAUDE_PROJECT_DIR", str(repo))
    env["CLAUDE_PLUGIN_ROOT"] = str(install)
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    command = [uv, "run", "--project", str(project), "--frozen", "--no-dev"]
    command += ["python", "-m", module, *args]
    return command, env


def server_port(repo: Path) -> int | None:
    try:
        info = json.loads((repo / ".sentinel-swarm" / SERVER_FILE).read_text(encoding="utf-8"))
        port = int(info["port"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return port if 0 < port < 65536 else None


def server_token(repo: Path) -> str | None:
    try:
        token = (repo / ".sentinel-swarm" / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if _TOKEN.fullmatch(token) else None


def fast_hook(event: str, payload: bytes, repo: Path, timeout: float) -> bytes | None:
    port = server_port(repo)
    token = server_token(repo)
    if port is None or token is None:
        return None
    project = os.environ.get("CLAUDE_PROJECT_DIR") or str(repo)
    # A plain HTTP/1.0 request, not http.client: its email imports add about 180 ms to
    # every hook.
    head = (
        f"POST {HOOK_PATH}/{event} HTTP/1.0\r\n"
        f"Host: {SERVER_HOST}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"{REPO_HEADER}: {urllib.parse.quote(project)}\r\n\r\n"
    )
    try:
        with socket.create_connection((SERVER_HOST, port), CONNECT_TIMEOUT_SECONDS) as sock:
            sock.settimeout(timeout)
            sock.sendall(head.encode("ascii") + payload)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
    except OSError:
        return None
    return parse_answer(b"".join(chunks))


def parse_answer(response: bytes) -> bytes | None:
    head, separator, body = response.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split()
    if not separator or len(status) < 2 or status[1] != "200":
        return None
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if REPO_HEADER.lower() not in headers:
        return None
    length = headers.get("content-length")
    if length is not None and length != str(len(body)):
        return None
    return body


def stamp_payload(payload: bytes) -> bytes:
    try:
        data = json.loads(payload.decode("utf-8")) if payload.strip() else {}
    except ValueError:
        return payload
    if not isinstance(data, dict):
        return payload
    now = datetime.now(timezone.utc)
    data[FIRED_AT_KEY] = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return json.dumps(data).encode("utf-8")


def run_hook(event: str, payload: bytes) -> bytes:
    deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS
    repo = repo_root()
    if event in STAMPED_EVENTS:
        payload = stamp_payload(payload)
    fast_timeout = SLOW_EVENT_TIMEOUT_SECONDS.get(event, FAST_TIMEOUT_SECONDS)
    output = fast_hook(event, payload, repo, fast_timeout)
    if output is not None:
        return output
    return run_ledger_hook(event, payload, max(deadline - time.monotonic(), 0.1))


def run_ledger_hook(event: str, payload: bytes, timeout: float = HOOK_TIMEOUT_SECONDS) -> bytes:
    repo = repo_root()
    command, env = ledger_command(repo, "swarm_ledger.hooks", event)
    try:
        done = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            timeout=timeout,
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
        output = run_hook(event, payload)
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


def channel_main() -> int:
    repo = repo_root()
    try:
        command, env = ledger_command(repo, "swarm_ledger.bridge", "--repo", str(repo))
        return subprocess.call(command, env=env, cwd=str(repo))
    except (ShimError, OSError) as exc:
        sys.stderr.write(f"sentinel-swarm cannot start the swarm-events channel: {exc}\n")
        return 1


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "hook":
        return hook_main(argv[1])
    if len(argv) == 3 and argv[0] == "mcp":
        return mcp_main(argv[1], argv[2])
    if argv == ["watch"]:
        return watch_main()
    if argv == ["channel"]:
        return channel_main()
    sys.stderr.write(USAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
