from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO, Any

from . import __version__, env, serve, wake

SESSION_VAR = "CLAUDE_CODE_SESSION_ID"
PROTOCOL_VERSION = "2025-06-18"
RETRY_S = 2.0
READ_TIMEOUT_S = wake.PING_S * 3
_META_KEY = re.compile(r"^[A-Za-z0-9_]+$")
INSTRUCTIONS = (
    f'Events from {wake.CHANNEL_SERVER} arrive as <channel source="{wake.CHANNEL_SERVER}" '
    'wakeup_id="..." reason="...">. Each one is a sentinel-swarm wake-up: follow the pointer '
    "in its text, as you would a wake-up message from another swarm session."
)

Opener = Callable[..., Any]


def events_url(repo_root: Path, session_id: str) -> str | None:
    info = serve.read_server_info(repo_root)
    if info is None:
        return None
    query = urllib.parse.urlencode({"session": session_id})
    return f"http://{serve.HOST}:{info['port']}{wake.EVENTS_PATH}?{query}"


class Bridge:
    def __init__(
        self,
        out: IO[bytes],
        url: Callable[[], str | None],
        *,
        opener: Opener = urllib.request.urlopen,
        retry_s: float = RETRY_S,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._out = out
        self._url = url
        self._opener = opener
        self._retry_s = retry_s
        self._log = log or (lambda text: print(f"swarm-events: {text}", file=sys.stderr))
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None

    def send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._write_lock:
            self._out.write(data)
            self._out.flush()

    def notify(self, event: dict[str, Any]) -> bool:
        if event.get("kind") == wake.PING_EVENT or "content" not in event:
            return False
        raw_meta = event.get("meta")
        meta = {
            str(key): str(value)
            for key, value in (raw_meta.items() if isinstance(raw_meta, dict) else ())
            if _META_KEY.match(str(key))
        }
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/claude/channel",
                "params": {"content": str(event["content"]), "meta": meta},
            }
        )
        return True

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        if method == "notifications/initialized":
            self.start()
            return None
        if "id" not in message or method is None:
            return None
        request_id = message["id"]
        if method == "initialize":
            params = message.get("params") or {}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"experimental": {"claude/channel": {}}},
                    "serverInfo": {"name": wake.CHANNEL_SERVER, "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def listen_once(self) -> int:
        url = self._url()
        if url is None:
            raise ConnectionError("no ledger server is recorded for this repo")
        delivered = 0
        with self._opener(url, timeout=READ_TIMEOUT_S) as response:
            self._report(None)
            for raw in response:
                if self._stop.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and self.notify(event):
                    delivered += 1
        return delivered

    def listen(self) -> None:
        while not self._stop.is_set():
            try:
                self.listen_once()
                self._report("the ledger closed the event stream; reconnecting")
            except (OSError, ValueError, http.client.HTTPException) as exc:
                self._report(f"cannot reach the ledger's event stream: {exc}")
            if self._stop.wait(self._retry_s):
                return

    def _report(self, error: str | None) -> None:
        if error != self._last_error and error is not None:
            self._log(error)
        self._last_error = error

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.listen, name="swarm-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def serve_stdio(bridge: Bridge, lines: Iterable[bytes]) -> int:
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        reply = bridge.handle(message)
        if reply is not None:
            bridge.send(reply)
    bridge.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m swarm_ledger.bridge")
    parser.add_argument("--repo", type=Path, default=None, help="the host repo root")
    args = parser.parse_args(argv)
    root = (args.repo or env.repo_root()).resolve()
    session_id = os.environ.get(SESSION_VAR) or ""
    if not session_id:
        print(f"swarm-events: {SESSION_VAR} is not set; no events will arrive", file=sys.stderr)
    bridge = Bridge(sys.stdout.buffer, lambda: events_url(root, session_id) if session_id else None)
    return serve_stdio(bridge, sys.stdin.buffer)


if __name__ == "__main__":
    sys.exit(main())
