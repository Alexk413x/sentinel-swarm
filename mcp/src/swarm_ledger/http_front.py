"""The ledger's lean HTTP front: MCP on `/mcp`, hooks on `/hook/<event>`, and `/health`.

Stdlib only, after codebase-kg's `http_transport.py`. It replaces fastmcp and uvicorn at run
time; `server.py` stays the source of the tool catalog (`catalog.py`) and of the in-process
test client.

`/mcp` speaks both MCP eras on single POSTs with JSON replies and no sessions: a request
carrying `MCP-Protocol-Version: 2026-07-28` gets the stateless protocol (`server/discover`, no
`initialize`); any other request gets the handshake era (`initialize`, then calls). A
notification gets 202. GET and DELETE get 405, since the server opens no stream.

Every request passes `serve.refusal` first: a remote client, a foreign `Host` or `Origin`, or,
on every route but `/health`, a missing or wrong bearer token gets 403.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from . import __version__, catalog, front
from .pool import CallError
from .serve import HEALTH_PATH, HOOK_PATH, MCP_PATH, REPO_HEADER, same_path

MODERN_PROTOCOL = "2026-07-28"
CLASSIC_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": "swarm-ledger", "version": __version__}
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022
STATUS = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_VERSION: 400,
    METHOD_NOT_FOUND: 404,
}
EMPTY_LISTS = {
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
}
LISTED = {"ttlMs": 0, "cacheScope": "private"}
NOT_ONE_MESSAGE = "Body must be a single JSON-RPC request or notification object"
MAX_BODY = 8 * 1024 * 1024
KEEPALIVE_TIMEOUT = 60.0
HOOK_PREFIX = HOOK_PATH + "/"

Refusal = Callable[[str | None, dict[str, str], str], "str | None"]


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def text_result(name: str, value: Any) -> dict[str, Any]:
    content = [] if value == [] else [{"type": "text", "text": dumps(value)}]
    if name in catalog.WRAPPED:
        return {
            "_meta": {"fastmcp": {"wrap_result": True}},
            "content": content,
            "structuredContent": {"result": value},
            "isError": False,
        }
    return {"content": content, "structuredContent": value, "isError": False}


def error_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def call_tool(name: Any, raw_args: Any) -> dict[str, Any]:
    if not isinstance(name, str) or name not in catalog.TOOLS:
        return error_result(f"Unknown tool: {name!r}")
    args = dict(raw_args) if isinstance(raw_args, dict) else raw_args
    stamped = args.pop("agent_id", None) if isinstance(args, dict) else None
    agent_id = stamped if isinstance(stamped, str) else None
    try:
        checked = catalog.validate(name, args)
    except ValueError as exc:
        return error_result(f"1 validation error for call[{name}]\n{exc}")
    try:
        value = front.call_tool(name, checked, agent_id, name in catalog.STAMPED)
    except CallError as exc:
        return error_result(str(exc))
    return text_result(name, value)


def classic(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    if not isinstance(method, str) or "id" not in msg:
        return None
    raw = msg.get("params")
    params: dict[str, Any] = raw if isinstance(raw, dict) else {}
    result: dict[str, Any]
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in CLASSIC_VERSIONS else CLASSIC_VERSIONS[-1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        if catalog.CATALOG.get("instructions"):
            result["instructions"] = catalog.CATALOG["instructions"]
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": catalog.CATALOG["tools"]}
    elif method in EMPTY_LISTS:
        result = {EMPTY_LISTS[method]: []}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


class BodyError(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class LedgerHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(
        self,
        sock: socket.socket,
        root: Path,
        refusal: Refusal,
        health: dict[str, Any],
    ) -> None:
        super().__init__(sock.getsockname(), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        sock.setblocking(True)
        sock.listen(self.request_queue_size)
        self.root = root
        self.refusal = refusal
        self.health = health

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = KEEPALIVE_TIMEOUT

    @property
    def srv(self) -> LedgerHttpServer:
        return cast(LedgerHttpServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str | None = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response_only(status)
        if content_type and body:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, dumps(obj).encode("utf-8"))

    def _text(self, status: int, text: str) -> None:
        self.close_connection = True
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _rpc_error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._json(STATUS.get(code, 200), {"jsonrpc": "2.0", "id": request_id, "error": error})

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _gate(self) -> bool:
        headers = {name.lower(): value for name, value in self.headers.items()}
        reason = self.srv.refusal(self.client_address[0], headers, self._path())
        if reason is not None:
            self._text(403, reason)
            return False
        return True

    def do_GET(self) -> None:
        if not self._gate():
            return
        path = self._path()
        if path == HEALTH_PATH:
            self._json(200, self.srv.health)
        elif path == MCP_PATH or path.startswith(HOOK_PREFIX):
            self.close_connection = True
            self._send(405, headers={"Allow": "POST"})
        else:
            self._text(404, "Not Found")

    do_DELETE = do_PUT = do_PATCH = do_GET

    def _body(self) -> bytes:
        chunked = "chunked" in self.headers.get("transfer-encoding", "").lower()
        length = self.headers.get("content-length")
        if chunked and length is not None:
            raise BodyError(400)
        if chunked:
            return self._chunks()
        try:
            size = int(length or 0)
        except ValueError:
            raise BodyError(400) from None
        if size < 0:
            raise BodyError(400)
        if size > MAX_BODY:
            raise BodyError(413)
        body = self.rfile.read(size)
        if len(body) != size:
            raise BodyError(400)
        return body

    def _chunks(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(1024)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise BodyError(400) from None
            if size < 0:
                raise BodyError(400)
            if size == 0:
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(body)
            if len(body) + size > MAX_BODY:
                raise BodyError(413)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.readline(3).strip():
                raise BodyError(400)
            body += chunk

    def do_POST(self) -> None:
        if not self._gate():
            return
        path = self._path()
        if path != MCP_PATH and not path.startswith(HOOK_PREFIX):
            self._text(404, "Not Found")
            return
        try:
            body = self._body()
        except BodyError as exc:
            self.close_connection = True
            self._send(exc.status)
            return
        if path.startswith(HOOK_PREFIX):
            self._hook(urllib.parse.unquote(path[len(HOOK_PREFIX) :]), body)
            return
        accept = self.headers.get("accept", "")
        if accept and "application/json" not in accept and "*/*" not in accept:
            self.close_connection = True
            self._send(406)
            return
        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):
            self._rpc_error(None, PARSE_ERROR, "Parse error")
            return
        self._message(msg)

    def _hook(self, event: str, body: bytes) -> None:
        root = self.srv.root
        repo = urllib.parse.unquote(self.headers.get(REPO_HEADER, ""))
        if not repo or not same_path(repo, root):
            self._text(409, f"this server serves {root}")
            return
        try:
            stdout, stderr = front.run_hook(root, event, body.decode("utf-8", errors="replace"))
        except front.UnknownHook:
            self._text(404, f"unknown hook event {event!r}")
            return
        if stderr:
            sys.stderr.write(stderr)
            sys.stderr.flush()
        self._send(
            200,
            stdout.encode("utf-8"),
            headers={REPO_HEADER: urllib.parse.quote(str(root))},
        )

    def _message(self, msg: Any) -> None:
        version = self.headers.get("mcp-protocol-version")
        if (
            not isinstance(msg, dict)
            or msg.get("jsonrpc") != "2.0"
            or not isinstance(msg.get("method"), str)
        ):
            self._rpc_error(None, INVALID_REQUEST, NOT_ONE_MESSAGE)
            return
        if "id" not in msg:
            self._send(202, content_type=None)
            return
        request_id = msg["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, str | int):
            self._rpc_error(None, INVALID_REQUEST, NOT_ONE_MESSAGE)
            return
        if version is None or version in CLASSIC_VERSIONS:
            self._json(200, classic(msg))
            return
        self._modern(msg, request_id, version)

    def _modern(self, msg: dict[str, Any], request_id: Any, version: str) -> None:
        for name in ("mcp-protocol-version", "mcp-method", "mcp-name"):
            if len(self.headers.get_all(name) or []) > 1:
                self._rpc_error(
                    request_id, HEADER_MISMATCH, f"{name} header appears more than once"
                )
                return
        params = msg.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if (
            not isinstance(params, dict)
            or not isinstance(meta, dict)
            or VERSION_KEY not in meta
            or CAPABILITIES_KEY not in meta
        ):
            self._rpc_error(
                request_id,
                INVALID_PARAMS,
                f"params._meta must be an object carrying the required {VERSION_KEY!r} and "
                f"{CAPABILITIES_KEY!r} envelope keys",
            )
            return
        method = msg["method"]
        if version != meta[VERSION_KEY]:
            self._rpc_error(
                request_id,
                HEADER_MISMATCH,
                "mcp-protocol-version header does not match the request envelope's "
                "protocol version",
            )
            return
        if self.headers.get("mcp-method") != method:
            self._rpc_error(
                request_id,
                HEADER_MISMATCH,
                "mcp-method header does not match the request body's method",
            )
            return
        if (
            method == "tools/call"
            and params.get("name") is not None
            and urllib.parse.unquote(self.headers.get("mcp-name") or "") != params.get("name")
        ):
            self._rpc_error(
                request_id,
                HEADER_MISMATCH,
                "mcp-name header does not match the request body's 'name' parameter",
            )
            return
        if version != MODERN_PROTOCOL:
            self._rpc_error(
                request_id,
                UNSUPPORTED_VERSION,
                "Unsupported protocol version",
                {"supported": [MODERN_PROTOCOL, *CLASSIC_VERSIONS], "requested": version},
            )
            return
        result = self._dispatch(method, params)
        if result is None:
            self._rpc_error(request_id, METHOD_NOT_FOUND, "Method not found", method)
            return
        result.setdefault("resultType", "complete")
        meta_out = result.setdefault("_meta", {})
        meta_out.setdefault(SERVER_INFO_KEY, SERVER_INFO)
        self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == "server/discover":
            result = {
                **LISTED,
                "supportedVersions": [MODERN_PROTOCOL],
                "capabilities": {"tools": {"listChanged": False}},
            }
            if catalog.CATALOG.get("instructions"):
                result["instructions"] = catalog.CATALOG["instructions"]
            return result
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": catalog.CATALOG["tools"], **LISTED}
        if method in EMPTY_LISTS:
            return {EMPTY_LISTS[method]: [], **LISTED}
        if method == "tools/call":
            return call_tool(params.get("name"), params.get("arguments"))
        return None
