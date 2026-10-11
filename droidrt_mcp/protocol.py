"""Minimal MCP server core: JSON-RPC 2.0 over newline-delimited stdio.

Implements the parts of the Model Context Protocol (revision 2025-06-18, with
negotiation down to 2025-03-26 and 2024-11-05) that droidrt needs: lifecycle,
ping, tools and resources. Only the standard library is used.

stdout carries protocol messages only; every log line goes to stderr.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("droidrt_mcp")

LATEST_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002
SERVER_BUSY = -32000

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_BATCH = 32
MAX_PENDING = 64
# How long in flight requests may still finish after stdin closes, before the
# server shuts down anyway (the client is gone and nobody reads the answers).
DRAIN_SECONDS = 5.0


def redact(text: Any) -> str:
    """Error text for the client with the user's home directory shown as ~, so
    local paths (and the account name in them) never reach the model."""
    text = str(text)
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError):
        return text
    if len(home) > 1:
        text = text.replace(home.rstrip("/"), "~")
    return text


class RpcError(Exception):
    """A JSON-RPC error answered to the client."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_json(self) -> dict:
        err = {"code": self.code, "message": redact(self.message)}
        if self.data is not None:
            err["data"] = self.data
        return err


class ToolInputError(Exception):
    """Bad tool arguments or a failed tool run. Reported as a result with isError."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    title: str | None = None
    annotations: dict | None = None

    def describe(self, version: str) -> dict:
        out = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if version >= "2025-06-18" and self.title:
            out["title"] = self.title
        if version >= "2025-03-26" and self.annotations:
            out["annotations"] = self.annotations
        return out


@dataclass
class Resource:
    uri: str
    name: str
    description: str
    reader: Callable[[], str]
    mime_type: str = "application/json"


@dataclass
class ResourceTemplate:
    uri_template: str
    name: str
    description: str
    prefix: str
    reader: Callable[[str], str]
    mime_type: str = "application/json"


def text_content(text: str) -> dict:
    return {"type": "text", "text": text}


def image_content(data_b64: str, mime_type: str = "image/png") -> dict:
    return {"type": "image", "data": data_b64, "mimeType": mime_type}


def to_content(value: Any) -> list[dict]:
    """Normalize a handler return value into a list of content blocks."""
    if value is None:
        return [text_content("ok")]
    if isinstance(value, str):
        return [text_content(value)]
    if isinstance(value, dict) and value.get("type") in ("text", "image"):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) and v.get("type") in ("text", "image") for v in value):
        return value
    return [text_content(json.dumps(value, ensure_ascii=False, indent=1))]


@dataclass
class McpServer:
    name: str
    version: str
    instructions: str | None = None
    workers: int = 4
    drain_seconds: float = DRAIN_SECONDS
    tools: dict[str, Tool] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    templates: list[ResourceTemplate] = field(default_factory=list)
    on_initialized: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.protocol_version = LATEST_VERSION
        self.initialize_seen = False
        self.client_ready = False
        self._out = None
        self._write_lock = threading.Lock()
        self._tools_lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._pending = 0
        self._pending_lock = threading.Lock()
        # Pooled requests by id: False while running, True once cancelled.
        self._inflight: dict[tuple, bool] = {}
        self._futures: set[Future] = set()
        self._out_broken = False

    # ------------------------------------------------------------ registry

    def set_tools(self, tools: Iterable[Tool]) -> bool:
        """Replace the tool set. Sends tools/list_changed when it differs."""
        new = {t.name: t for t in tools}
        with self._tools_lock:
            old_sig = {n: (t.description, json.dumps(t.input_schema, sort_keys=True)) for n, t in self.tools.items()}
            new_sig = {n: (t.description, json.dumps(t.input_schema, sort_keys=True)) for n, t in new.items()}
            self.tools = new
        changed = old_sig != new_sig
        if changed and self.client_ready:
            self.notify("notifications/tools/list_changed")
        return changed

    def tool_list(self) -> list[Tool]:
        with self._tools_lock:
            return list(self.tools.values())

    # ------------------------------------------------------------ output

    def send(self, message: dict) -> None:
        if self._out is None or self._out_broken:
            return
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError) as e:
                # The client went away (broken pipe or closed stream). Later
                # messages are dropped instead of raising in every thread.
                self._out_broken = True
                log.info("cannot write to the client: %s", e)

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    # ------------------------------------------------------------ dispatch

    def handle_line(self, line: str) -> list[dict]:
        """Parse one input line and return the responses to write (possibly none)."""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return [error_response(None, RpcError(PARSE_ERROR, f"Parse error: {e}"))]
        if isinstance(msg, list):
            # Batches were removed in 2025-06-18 but older revisions allow them.
            if not msg:
                return [error_response(None, RpcError(INVALID_REQUEST, "Invalid Request: empty batch"))]
            if len(msg) > MAX_BATCH:
                return [error_response(None, RpcError(INVALID_REQUEST,
                                                      f"Invalid Request: batch too large (max {MAX_BATCH})"))]
            out = []
            for item in msg:
                resp = self.handle_message(item)
                if resp is not None:
                    out.append(resp)
            return [out] if out else []  # type: ignore[list-item]
        resp = self.handle_message(msg)
        return [resp] if resp is not None else []

    def handle_message(self, msg: Any) -> dict | None:
        """Handle one decoded message synchronously. Returns the response or None."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return error_response(_safe_id(msg), RpcError(INVALID_REQUEST, "Invalid Request"))
        has_id = "id" in msg
        msg_id = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            if has_id and ("result" in msg or "error" in msg):
                return None  # a response to a request we never send; ignore
            return error_response(_safe_id(msg), RpcError(INVALID_REQUEST, "Invalid Request: missing method"))
        if has_id and (msg_id is None or isinstance(msg_id, bool) or not isinstance(msg_id, (str, int))):
            return error_response(None, RpcError(INVALID_REQUEST, "Invalid Request: id must be a string or integer"))
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if not has_id:
                return None
            return error_response(msg_id, RpcError(INVALID_PARAMS, "params must be an object"))
        if not has_id:
            self.handle_notification(method, params)
            return None
        try:
            result = self.dispatch(method, params)
        except RpcError as e:
            return error_response(msg_id, e)
        except Exception as e:  # never let a handler kill the server
            log.error("internal error in %s: %s\n%s", method, e, traceback.format_exc())
            return error_response(msg_id, RpcError(INTERNAL_ERROR, f"Internal error: {e}"))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def handle_notification(self, method: str, params: dict) -> None:
        if method == "notifications/initialized":
            if not self.client_ready:
                self.client_ready = True
                if self.on_initialized:
                    threading.Thread(target=self._safe_initialized, daemon=True).start()
        elif method == "notifications/cancelled":
            self.cancel(params.get("requestId"))
        elif method in ("notifications/roots/list_changed", "notifications/progress"):
            pass
        else:
            log.debug("ignoring notification %s", method)

    def _safe_initialized(self) -> None:
        try:
            self.on_initialized()  # type: ignore[misc]
        except Exception as e:
            log.error("initialization hook failed: %s", e)

    def dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if not self.initialize_seen:
            raise RpcError(INVALID_REQUEST, "Server not initialized: send initialize first")
        handlers = {
            "tools/list": self.tools_list,
            "tools/call": self.tools_call,
            "resources/list": self.resources_list,
            "resources/read": self.resources_read,
            "resources/templates/list": self.resource_templates_list,
            "prompts/list": lambda p: {"prompts": []},
            "logging/setLevel": self.set_level,
        }
        handler = handlers.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return handler(params)

    # ------------------------------------------------------------ methods

    def initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise RpcError(INVALID_PARAMS, "protocolVersion is required")
        self.protocol_version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        self.initialize_seen = True
        info = params.get("clientInfo") or {}
        log.info("client %s %s, protocol %s", info.get("name", "?"), info.get("version", "?"), self.protocol_version)
        result = {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": False, "subscribe": False},
                "logging": {},
            },
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.protocol_version >= "2025-06-18":
            result["serverInfo"]["title"] = "droidrt"
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def set_level(self, params: dict) -> dict:
        level = params.get("level")
        mapping = {"debug": logging.DEBUG, "info": logging.INFO, "notice": logging.INFO, "warning": logging.WARNING,
                   "error": logging.ERROR, "critical": logging.CRITICAL, "alert": logging.CRITICAL,
                   "emergency": logging.CRITICAL}
        if level not in mapping:
            raise RpcError(INVALID_PARAMS, f"unknown log level {level!r}")
        log.setLevel(mapping[level])
        return {}

    def tools_list(self, params: dict) -> dict:
        return {"tools": [t.describe(self.protocol_version) for t in self.tool_list()]}

    def tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
        args = params.get("arguments", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, "arguments must be an object")
        with self._tools_lock:
            tool = self.tools.get(name)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            value = tool.handler(args)
        except ToolInputError as e:
            return {"content": [text_content(redact(e))], "isError": True}
        except RpcError:
            raise
        except Exception as e:
            log.error("tool %s failed: %s\n%s", name, e, traceback.format_exc())
            return {"content": [text_content(redact(f"{type(e).__name__}: {e}"))], "isError": True}
        return {"content": to_content(value), "isError": False}

    def resources_list(self, params: dict) -> dict:
        return {"resources": [
            {"uri": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime_type}
            for r in self.resources.values()
        ]}

    def resource_templates_list(self, params: dict) -> dict:
        return {"resourceTemplates": [
            {"uriTemplate": t.uri_template, "name": t.name, "description": t.description, "mimeType": t.mime_type}
            for t in self.templates
        ]}

    def resources_read(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str) or len(uri) > 2048:
            raise RpcError(INVALID_PARAMS, "resources/read needs a uri")
        res = self.resources.get(uri)
        try:
            if res is not None:
                text, mime = res.reader(), res.mime_type
            else:
                tpl = next((t for t in self.templates if uri.startswith(t.prefix) and len(uri) > len(t.prefix)), None)
                if tpl is None:
                    raise RpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
                text, mime = tpl.reader(uri[len(tpl.prefix):]), tpl.mime_type
        except (ToolInputError, ValueError) as e:
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not available: {e}", {"uri": uri}) from None
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}

    # ------------------------------------------------------------ cancellation

    @staticmethod
    def _id_key(msg_id: Any) -> tuple | None:
        """Dictionary key for a request id: 1 and "1" are different ids."""
        if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
            return None
        return (type(msg_id).__name__, msg_id)

    def cancel(self, request_id: Any) -> bool:
        """Mark an in flight pooled request as cancelled. Its response is never
        sent; a request still waiting in the queue does not run at all. Unknown
        or finished ids are ignored, as the specification asks."""
        key = self._id_key(request_id)
        if key is None:
            return False
        with self._pending_lock:
            if key not in self._inflight:
                return False
            self._inflight[key] = True
        log.info("request %r cancelled by the client", request_id)
        return True

    def _cancelled(self, key: tuple | None) -> bool:
        if key is None:
            return False
        with self._pending_lock:
            return self._inflight.get(key, False)

    # ------------------------------------------------------------ loop

    def serve(self, stdin=None, stdout=None) -> None:
        """Read requests until EOF. Requests run on a small thread pool so that a
        slow tool call does not block ping or other requests."""
        stdin = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._out_broken = False
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="mcp")
        drain = self.drain_seconds
        try:
            while True:
                line = stdin.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES and not line.endswith("\n"):
                    # Drop the rest of an oversized line, then report it.
                    while line and not line.endswith("\n"):
                        line = stdin.readline(MAX_LINE_BYTES)
                    self.send(error_response(None, RpcError(INVALID_REQUEST, "Request too large")))
                    continue
                line = line.strip()
                if not line:
                    continue
                msg = self._pooled_message(line)
                if msg is None:
                    for resp in self.handle_line(line):
                        self.send(resp)  # type: ignore[arg-type]
                    continue
                key = self._id_key(msg.get("id")) if "id" in msg else None
                status = self._reserve(key)
                if status == "busy":
                    # Bounded queue: a client flooding slow calls gets an error
                    # instead of growing memory without limit.
                    if "id" in msg:
                        self.send(error_response(_safe_id(msg), RpcError(SERVER_BUSY, "server busy")))
                elif status == "duplicate":
                    self.send(error_response(_safe_id(msg), RpcError(
                        INVALID_REQUEST, "Invalid Request: id is already used by a request in progress")))
                else:
                    try:
                        fut = self._pool.submit(self._run_line, line, key)
                    except RuntimeError:
                        self._release(key)
                        raise
                    with self._pending_lock:
                        self._futures.add(fut)
                    fut.add_done_callback(lambda f, k=key: self._forget_future(f, k))
        except BaseException:
            # SIGTERM or Ctrl-C: do not wait for slow tool calls, the process
            # is about to stop and the device cleanup must run first.
            drain = 0
            raise
        finally:
            with self._pending_lock:
                running = set(self._futures)
            if running and drain > 0:
                wait_futures(running, timeout=drain)
            # Queued requests are dropped; running ones finish in their worker
            # thread while the caller cleans up, and their answers go nowhere.
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _forget_future(self, fut: Future, key: tuple | None) -> None:
        with self._pending_lock:
            self._futures.discard(fut)
        if fut.cancelled():
            # Dropped from the queue at shutdown: _run_line never ran, so the
            # slot is given back here.
            self._release(key)

    def _reserve(self, key: tuple | None = None) -> str:
        with self._pending_lock:
            if key is not None and key in self._inflight:
                return "duplicate"
            if self._pending >= MAX_PENDING:
                return "busy"
            self._pending += 1
            if key is not None:
                self._inflight[key] = False
            return "ok"

    def _release(self, key: tuple | None = None) -> None:
        with self._pending_lock:
            self._pending -= 1
            if key is not None:
                self._inflight.pop(key, None)

    def _run_line(self, line: str, key: tuple | None = None) -> None:
        try:
            if self._cancelled(key):
                return
            responses = self.handle_line(line)
            if self._cancelled(key):
                return  # the client cancelled it: no response at all
            for resp in responses:
                self.send(resp)  # type: ignore[arg-type]
        except Exception as e:
            log.error("failed to handle message: %s", e)
        finally:
            self._release(key)

    @staticmethod
    def _pooled_message(line: str) -> dict | None:
        """The decoded message when it goes to the pool, else None. Lifecycle
        messages and batches run in order on the reader thread."""
        try:
            msg = json.loads(line)
        except ValueError:
            return None
        if isinstance(msg, dict) and msg.get("method") in ("tools/call", "resources/read", "tools/list"):
            return msg
        return None

    @staticmethod
    def _is_inline(line: str) -> bool:
        return McpServer._pooled_message(line) is None


def _safe_id(msg: Any) -> Any:
    if isinstance(msg, dict):
        i = msg.get("id")
        if isinstance(i, (str, int)) and not isinstance(i, bool):
            return i
    return None


def error_response(msg_id: Any, err: RpcError) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": err.to_json()}
