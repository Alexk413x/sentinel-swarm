from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from .db import ledger_path
from .identity import LedgerError

TOKEN_FILE = "http-token"
AUTHORIZATION = "Authorization"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_LEDGER_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/mcp")


def token_path(repo_root: Path) -> Path:
    return ledger_path(repo_root).parent / TOKEN_FILE


def read_token(repo_root: Path) -> str:
    path = token_path(repo_root)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise LedgerError(
            f"no ledger token at {path}; start the ledger with mcp/launch/ledger.py serve --detach"
        ) from None
    if not _TOKEN.fullmatch(token):
        raise LedgerError(f"the ledger token in {path} is malformed; delete it and restart")
    return token


def ensure_token(repo_root: Path) -> str:
    try:
        return read_token(repo_root)
    except LedgerError:
        pass
    path = token_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return read_token(repo_root)


def bearer(token: str) -> str:
    return f"Bearer {token}"


def checked_url(url: str) -> str:
    match = _LEDGER_URL.fullmatch(url)
    if match is None or not 0 < int(match.group(1)) < 65536:
        raise LedgerError(
            f"the recorded ledger URL {url!r} is not http://127.0.0.1:<port>/mcp; "
            "restart the ledger with mcp/launch/ledger.py serve --detach"
        )
    return url


def ledger_entry(repo_root: Path, url: str) -> dict[str, object]:
    return {
        "type": "http",
        "url": checked_url(url),
        "headers": {AUTHORIZATION: bearer(read_token(repo_root))},
    }


def auth_headers(repo_root: Path) -> dict[str, str]:
    try:
        return {AUTHORIZATION: bearer(read_token(repo_root))}
    except LedgerError:
        return {}
