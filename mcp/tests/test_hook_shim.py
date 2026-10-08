from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import setup
from swarm_ledger.hooks.events import FIRED_AT_KEY


@pytest.fixture
def shim() -> Any:
    spec = importlib.util.spec_from_file_location(
        "sentinel_swarm_hook_shim_fast", setup.SHIM_TEMPLATE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path, shim: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "host"
    (root / ".sentinel-swarm").mkdir(parents=True)
    monkeypatch.setattr(shim, "repo_root", lambda: root)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


class FakeServer:
    def __init__(self, status: int = 200, echo_repo: bool = True, body: bytes = b"{}") -> None:
        self.status = status
        self.echo_repo = echo_repo
        self.body = body
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                fake.requests.append((self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(fake.status)
                if fake.echo_repo:
                    self.send_header("X-Sentinel-Swarm-Repo", self.headers["X-Sentinel-Swarm-Repo"])
                self.send_header("Content-Length", str(len(fake.body)))
                self.end_headers()
                self.wfile.write(fake.body)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_server() -> Iterator[FakeServer]:
    server = FakeServer()
    yield server
    server.close()


TOKEN = "t0ken-" + "x" * 40


def _record_port(repo: Path, port: int, token: str | None = TOKEN) -> None:
    info = {"url": f"http://127.0.0.1:{port}/mcp", "port": port, "pid": 1}
    (repo / ".sentinel-swarm" / "server.json").write_text(json.dumps(info), encoding="utf-8")
    if token is not None:
        (repo / ".sentinel-swarm" / "http-token").write_text(token + "\n", encoding="utf-8")


def _fallback(shim: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    def run_ledger_hook(event: str, payload: bytes, timeout: float = 50) -> bytes:
        assert 0 < timeout <= shim.HOOK_TIMEOUT_SECONDS
        calls.append((event, payload))
        return b"fallback"

    monkeypatch.setattr(shim, "run_ledger_hook", run_ledger_hook)
    return calls


def test_the_fast_path_answers_from_the_running_server(
    shim: Any, repo: Path, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_server.body = b'{"hookSpecificOutput": {"permissionDecision": "allow"}}'
    _record_port(repo, fake_server.port)
    calls = _fallback(shim, monkeypatch)
    assert shim.run_hook("pre_ledger", b'{"tool_name": "x"}') == fake_server.body
    assert calls == []
    [(path, headers, body)] = fake_server.requests
    assert path == "/hook/pre_ledger"
    assert urllib.parse.unquote(headers["X-Sentinel-Swarm-Repo"]) == str(repo)
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body == b'{"tool_name": "x"}'


@pytest.mark.parametrize("token", [None, "short", "two words " + "x" * 40])
def test_the_shim_falls_back_without_a_valid_token(
    shim: Any,
    repo: Path,
    fake_server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    _record_port(repo, fake_server.port, token)
    calls = _fallback(shim, monkeypatch)
    assert shim.run_hook("pre_write", b"{}") == b"fallback"
    assert calls == [("pre_write", b"{}")]
    assert fake_server.requests == []


def test_the_shim_falls_back_without_a_server_record(
    shim: Any, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fallback(shim, monkeypatch)
    assert shim.run_hook("pre_write", b"{}") == b"fallback"
    assert calls == [("pre_write", b"{}")]


def test_the_shim_falls_back_when_the_recorded_server_is_down(
    shim: Any, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _record_port(repo, port)
    calls = _fallback(shim, monkeypatch)
    assert shim.run_hook("pre_write", b"{}") == b"fallback"
    assert calls == [("pre_write", b"{}")]


@pytest.mark.parametrize(
    ("status", "echo_repo"), [(403, False), (409, True), (404, True), (200, False)]
)
def test_the_shim_falls_back_when_the_server_is_not_this_repos_ledger(
    shim: Any,
    repo: Path,
    fake_server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    echo_repo: bool,
) -> None:
    fake_server.status, fake_server.echo_repo = status, echo_repo
    _record_port(repo, fake_server.port)
    calls = _fallback(shim, monkeypatch)
    assert shim.run_hook("pre_write", b"{}") == b"fallback"
    assert len(fake_server.requests) == 1
    assert calls == [("pre_write", b"{}")]


def test_a_gating_hook_denies_when_the_server_and_the_fallback_both_fail(
    shim: Any, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _record_port(repo, port)

    def no_fallback(*args: Any) -> None:
        raise shim.ShimError("the ledger launcher is missing")

    monkeypatch.setattr(shim, "ledger_command", no_fallback)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert shim.hook_main("pre_ledger") == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        "the ledger launcher is missing" in answer["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_only_post_activity_is_stamped_with_the_time_it_fired(
    shim: Any, repo: Path, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_port(repo, fake_server.port)
    _fallback(shim, monkeypatch)
    shim.run_hook("post_activity", b'{"tool_name": "Read"}')
    shim.run_hook("post_any", b'{"tool_name": "Write"}')
    (_, _, stamped), (_, _, plain) = fake_server.requests
    fired_at = json.loads(stamped)[FIRED_AT_KEY]
    assert fired_at.endswith("Z") and len(fired_at) == len("2026-09-28T00:00:00.000Z")
    assert plain == b'{"tool_name": "Write"}'


_REPO = b"x-sentinel-swarm-repo: %2Fr\r\n"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b"HTTP/1.1 200 OK\r\n" + _REPO + b"content-length: 2\r\n\r\n{}", b"{}"),
        (b"HTTP/1.1 200 OK\r\n" + _REPO + b"\r\n", b""),
        (b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}", None),
        (b"HTTP/1.1 200 OK\r\n" + _REPO + b"content-length: 9\r\n\r\n{}", None),
        (b"HTTP/1.1 409 Conflict\r\n" + _REPO + b"\r\n", None),
        (b"HTTP/1.1 200 OK\r\n" + _REPO.rstrip(), None),
        (b"", None),
    ],
)
def test_the_shim_uses_only_a_complete_200_answer_from_this_repos_ledger(
    shim: Any, response: bytes, expected: bytes | None
) -> None:
    assert shim.parse_answer(response) == expected
