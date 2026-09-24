from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from . import setup

Mode = Literal["interactive", "bg", "headless"]


def _ensure_server(repo: Path) -> str:
    from .serve import ensure_server

    return ensure_server(repo)


def _read_agent_file(repo: Path, role: str) -> dict[str, Any]:
    from .agentfiles import read_agent_file

    return read_agent_file(repo, role)


def _session_options(repo: Path, role: str, model: str | None, ledger_url: str) -> list[str]:
    from .agentfiles import session_options

    return session_options(repo, role, model, ledger_url)


def _claude_binary() -> str:
    from .sessions import claude_binary

    return claude_binary()


def repo_slug(repo: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.resolve().name.lower()).strip("-") or "repo"


def oracle_session_name(repo: Path, started: float | None = None) -> str:
    # A launch-unique name: a stale session, local or over Remote Control, can keep an old name
    # and receive the messages meant for this Oracle.
    stamp = time.strftime("%m%d-%H%M%S", time.localtime(started))
    return f"{repo_slug(repo)}-oracle-{stamp}"


def oracle_command(
    repo: Path, prompt: str, ledger_url: str, mode: Mode, name: str | None = None
) -> list[str]:
    model = _read_agent_file(repo, "oracle").get("model")
    options = _session_options(repo, "oracle", str(model) if model else None, ledger_url)
    claude = _claude_binary()
    if mode == "headless":
        return [claude, "-p", *options, "--output-format", "stream-json", "--verbose"]
    # The prompt goes first: --allowedTools takes a space-separated list and swallows a
    # prompt that follows it.
    head = [claude, prompt]
    if mode == "bg":
        head += ["--bg", "--name", name or oracle_session_name(repo)]
    return [*head, *options]


def _needs_setup(repo: Path) -> bool:
    if not (repo / setup.SHIM_PATH).is_file():
        return True
    return any(not setup.role_file(repo, role).is_file() for role in setup.ROLES)


def _run(command: list[str], repo: Path, prompt: str, mode: Mode, transcript: Path | None) -> int:
    if mode != "headless":
        return subprocess.run(command, cwd=repo).returncode
    payload = prompt.encode("utf-8")
    if transcript is None:
        return subprocess.run(command, input=payload, cwd=repo).returncode
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with transcript.open("wb") as out:
        return subprocess.run(command, input=payload, stdout=out, cwd=repo).returncode


def launch(repo: Path, prompt: str, mode: Mode, transcript: Path | None = None) -> int:
    repo = repo.resolve()
    if _needs_setup(repo):
        report = setup.run_setup(repo)
        print(report.text(with_trust=mode != "bg" or report.trusted))
    if mode == "bg" and not setup.is_trusted(repo):
        sys.stderr.write(setup.trust_instructions(repo) + "\n")
        return 1
    name = oracle_session_name(repo)
    try:
        command = oracle_command(repo, prompt, _ensure_server(repo), mode, name)
    except Exception as exc:
        sys.stderr.write(f"cannot start the Oracle: {exc}\n")
        return 1
    try:
        code = _run(command, repo, prompt, mode, transcript)
    except OSError as exc:
        sys.stderr.write(f"cannot start {command[0]}: {exc}. Set SENTINEL_SWARM_CLAUDE.\n")
        return 1
    if mode == "bg" and code == 0:
        print(f"The Oracle runs in the background as {name}.")
        print("Watch it in agent view or with: claude agents")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarm_ledger.launch")
    parser.add_argument("--repo", type=Path, default=None, help="host repo root; default: cwd")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bg", action="store_true", help="start the Oracle as a background session")
    group.add_argument("--headless", action="store_true", help="run the Oracle with claude -p")
    parser.add_argument("--transcript", type=Path, help="with --headless: the stream-json file")
    parser.add_argument("prompt")
    args = parser.parse_args(argv)
    if args.transcript is not None and not args.headless:
        parser.error("--transcript works only with --headless")
    mode: Mode = "bg" if args.bg else "headless" if args.headless else "interactive"
    return launch(args.repo or Path.cwd(), args.prompt, mode, args.transcript)


if __name__ == "__main__":
    sys.exit(main())
