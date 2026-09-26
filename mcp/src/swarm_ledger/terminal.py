from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .identity import LedgerError

SPEC_DIR = Path(".sentinel-swarm") / "sessions"
ORACLE_TAB = Path(".sentinel-swarm") / "oracle-tab"
WT_WINDOW = "sentinel-swarm"
TMUX_SESSION = "sentinel-swarm"
# Set by a parent Claude Code session. A claude started with them inherited registers as that
# session's child: it is left out of claude agents and saves no transcript.
CHILD_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_JOB_DIR",
    "CLAUDE_PID",
)
_PASSED_PREFIX = "SENTINEL_SWARM_"


def write_spec(repo: Path, argv: list[str]) -> Path:
    folder = repo / SPEC_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.json"
    env = {k: v for k, v in os.environ.items() if k.startswith(_PASSED_PREFIX)}
    path.write_text(json.dumps({"argv": argv, "cwd": str(repo), "env": env}), encoding="utf-8")
    return path


def host_command(spec: Path) -> list[str]:
    return [sys.executable, "-m", "swarm_ledger.terminal", str(spec)]


def terminal_command(title: str, cwd: Path, command: list[str]) -> list[str] | None:
    if sys.platform == "win32":
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt is None:
            return None
        return [wt, "-w", WT_WINDOW, "new-tab", "--title", title, "-d", str(cwd), *command]
    line = f"cd {shlex.quote(str(cwd))} && {shlex.join(command)}"
    if sys.platform == "darwin":
        script = f'tell application "Terminal" to do script {json.dumps(line)}'
        return ["osascript", "-e", script]
    if shutil.which("tmux"):
        has = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if has.returncode == 0:
            return ["tmux", "new-window", "-d", "-t", TMUX_SESSION, "-n", title, line]
        return ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", title, line]
    emulator = shutil.which("x-terminal-emulator")
    if emulator:
        return [emulator, "-T", title, "-e", "sh", "-c", line]
    raise LedgerError(
        "no terminal to open an interactive session in: install tmux or set x-terminal-emulator"
    )


def open_session(repo: Path, title: str, argv: list[str]) -> None:
    command = host_command(write_spec(repo, argv))
    opener = terminal_command(title, repo, command)
    try:
        if opener is None:
            subprocess.Popen(
                command,
                cwd=repo,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        else:
            subprocess.Popen(
                opener,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError as exc:
        raise LedgerError(f"cannot open a terminal for {title}: {exc}") from exc


def record_oracle_tab(repo: Path, name: str) -> None:
    (repo / ORACLE_TAB).write_text(name, encoding="utf-8")


def oracle_tab_name(repo: Path) -> str | None:
    try:
        return (repo / ORACLE_TAB).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def host_env(passed: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_MARKERS}
    env.update(passed)
    return env


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m swarm_ledger.terminal <spec.json>\n")
        return 2
    spec_path = Path(argv[0])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_path.unlink(missing_ok=True)
    return subprocess.call(spec["argv"], cwd=spec["cwd"], env=host_env(spec.get("env", {})))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
