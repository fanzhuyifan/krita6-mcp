"""Authenticated bounded loopback transport. No Krita or Qt objects enter here."""

from collections import OrderedDict
import copy
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import socket
from socketserver import ThreadingMixIn
import threading
import time

from .discovery import default_state_dir, remove_discovery, write_discovery
from .protocol import BridgeError, PLUGIN_VERSION, PROTOCOL_VERSION, validate_id


class ArtifactStore:
    def __init__(
        self,
        *,
        max_count=32,
        max_bytes=16 * 1024 * 1024,
        max_artifact_bytes=2 * 1024 * 1024,
        ttl=60,
        clock=time.monotonic,
    ):
        if min(max_count, max_bytes, max_artifact_bytes, ttl) <= 0:
            raise ValueError("Artifact bounds must be positive")
        self.max_count, self.max_bytes = max_count, max_bytes
        self.max_artifact_bytes, self.ttl = max_artifact_bytes, ttl
        self._clock, self._lock = clock, threading.Lock()
        self._items, self._bytes = OrderedDict(), 0

    def _expire(self):
        now = self._clock()
        for key, (data, _, deadline) in list(self._items.items()):
            if now >= deadline:
                self._bytes -= len(data)
                del self._items[key]

    def put(self, data, mime_type="image/png"):
        if not isinstance(data, bytes) or not data or len(data) > self.max_artifact_bytes:
            raise BridgeError(
                "ARTIFACT_TOO_LARGE", "Preview must be nonempty bounded encoded bytes"
            )
        if mime_type != "image/png":
            raise BridgeError("UNSUPPORTED_FORMAT", "Only PNG preview artifacts are supported")
        with self._lock:
            self._expire()
            if len(self._items) >= self.max_count or self._bytes + len(data) > self.max_bytes:
                raise BridgeError(
                    "ARTIFACT_STORE_FULL", "Preview storage is full; wait for artifacts to expire"
                )
            artifact_id = secrets.token_hex(16)
            self._items[artifact_id] = (data, mime_type, self._clock() + self.ttl)
            self._bytes += len(data)
            return {"artifact_id": artifact_id, "mime_type": mime_type, "size_bytes": len(data)}

    def get(self, artifact_id):
        validate_id(artifact_id, "artifact_id")
        with self._lock:
            self._expire()
            if artifact_id not in self._items:
                raise BridgeError("ARTIFACT_NOT_FOUND", "Preview artifact is absent or expired")
            data, mime_type, _ = self._items[artifact_id]
            return data, mime_type


class _HTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, bridge):
        self.bridge = bridge
        self._slots = threading.BoundedSemaphore(bridge.max_workers)
        self._active, self._active_lock = set(), threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, client_address):
        request.settimeout(self.bridge.read_timeout)
        if not self._slots.acquire(blocking=False):
            # Immediate close keeps idle clients from consuming threads or delaying accept.
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        # Socket timeouts alone reset on each byte and allow indefinite trickling.
        # An absolute connection deadline bounds header/body parsing and writes.
        def close_at_deadline():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        deadline = threading.Timer(self.bridge.read_timeout, close_at_deadline)
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            with self._active_lock:
                self._active.discard(request)
            self._slots.release()

    def close_workers(self):
        with self._active_lock:
            active = list(self._active)
        for request in active:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()

    def handle_error(self, request, client_address):
        # Never log arbitrary request text, credentials, or host exceptions.
        pass


_STATUS = {
    "UNAUTHORIZED": 401,
    "INVALID_HOST": 403,
    "ORIGIN_FORBIDDEN": 403,
    "INSTANCE_MISMATCH": 409,
    "PROTOCOL_MISMATCH": 409,
    "OPERATION_ID_CONFLICT": 409,
    "QUEUE_FULL": 429,
    "LEDGER_FULL": 429,
    "READ_CACHE_FULL": 429,
    "BRIDGE_DRAINING": 503,
    "OPERATION_NOT_FOUND": 404,
    "ARTIFACT_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "REQUEST_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "INTERNAL_ERROR": 500,
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "KritaBridge"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler otherwise produces HTML containing caller input.
        self._json(
            code,
            {
                "error": {
                    "code": "INVALID_HTTP",
                    "message": "Malformed HTTP request",
                    "effect": "none",
                }
            },
        )

    def _json(self, status, value):
        data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, data, "application/json")

    def _send(self, status, data, mime_type):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _header(self, name, required=False):
        values = self.headers.get_all(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            raise BridgeError("INVALID_REQUEST", "Ambiguous or missing HTTP header")
        return values[0] if values else None

    def _authorize(self):
        bridge = self.server.bridge
        if self.headers.get_all("Origin") is not None:
            raise BridgeError("ORIGIN_FORBIDDEN", "Browser origins are not allowed")
        if self._header("Host", True) != "127.0.0.1:" + str(self.server.server_port):
            raise BridgeError("INVALID_HOST", "Host must identify this loopback listener exactly")
        authorization = self._header("Authorization") or ""
        if not hmac.compare_digest(
            authorization.encode("utf-8"), ("Bearer " + bridge._token).encode("utf-8")
        ):
            raise BridgeError("UNAUTHORIZED", "Bearer authentication is required")
        if self.headers.get_all("Transfer-Encoding") is not None:
            raise BridgeError("INVALID_REQUEST", "Transfer encoding is not supported")
        if sum(len(k) + len(v) for k, v in self.headers.items()) > 8192:
            raise BridgeError("REQUEST_TOO_LARGE", "HTTP headers exceed the bridge limit")

    def _body(self):
        content_type = self._header("Content-Type", True)
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise BridgeError("UNSUPPORTED_MEDIA_TYPE", "Use application/json")
        length = self._header("Content-Length", True)
        if not re.fullmatch(r"[0-9]{1,10}", length):
            raise BridgeError("INVALID_REQUEST", "Content-Length must be a bounded decimal")
        length = int(length)
        if length > self.server.bridge.max_request_bytes:
            raise BridgeError("REQUEST_TOO_LARGE", "Request body exceeds the bridge limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise BridgeError("INVALID_REQUEST", "Incomplete request body")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate object keys")
                result[key] = value
            return result

        def reject_constant(value):
            raise ValueError("Non-finite constant")

        try:
            return json.loads(
                raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
            )
        except (ValueError, UnicodeError, RecursionError):
            raise BridgeError(
                "INVALID_REQUEST", "Body must contain unambiguous finite JSON"
            ) from None

    def _route(self):
        self._authorize()
        bridge = self.server.bridge
        if self.command == "GET":
            if self._header("Content-Length") not in (None, "0"):
                raise BridgeError("INVALID_REQUEST", "GET requests cannot carry a body")
            if self.path == "/v1/session":
                return self._json(200, bridge.session())
            op = re.fullmatch(r"/v1/operations/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})", self.path)
            if op:
                return self._json(200, bridge.ledger.get(op[1]))
            artifact = re.fullmatch(r"/v1/artifacts/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})", self.path)
            if artifact:
                data, mime_type = bridge.artifacts.get(artifact[1])
                return self._send(200, data, mime_type)
        elif self.command == "POST":
            if self.path == "/v1/operations":
                return self._json(202, bridge.ledger.admit(self._body()))
            cancel = re.fullmatch(
                r"/v1/operations/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/cancel", self.path
            )
            if cancel:
                body = self._body()
                if not isinstance(body, dict) or set(body) != {"instance_id"}:
                    raise BridgeError("INVALID_REQUEST", "Cancellation requires instance_id")
                if body["instance_id"] != bridge.ledger.instance_id:
                    raise BridgeError(
                        "INSTANCE_MISMATCH", "Cancellation targets another bridge instance"
                    )
                return self._json(200, bridge.ledger.cancel(cancel[1]))
        else:
            raise BridgeError("METHOD_NOT_ALLOWED", "HTTP method is not supported")
        raise BridgeError("NOT_FOUND", "Unknown bridge endpoint")

    def _handle(self):
        try:
            self._route()
        except BridgeError as error:
            self._json(
                _STATUS.get(error.code, 400),
                {"error": {"code": error.code, "message": error.message, "effect": error.effect}},
            )
        except (ConnectionError, TimeoutError, OSError):
            self.close_connection = True
        except Exception:
            self._json(
                500,
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Bridge transport failed",
                        "effect": "unknown",
                    }
                },
            )

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_HEAD = _handle


class BridgeServer:
    def __init__(
        self,
        ledger,
        session_info,
        artifacts,
        state_dir=None,
        *,
        max_workers=4,
        read_timeout=2.0,
        max_request_bytes=1024 * 1024,
    ):
        if min(max_workers, read_timeout, max_request_bytes) <= 0:
            raise ValueError("HTTP bounds must be positive")
        self.ledger, self.artifacts = ledger, artifacts
        self._session_info = json.loads(json.dumps(session_info, allow_nan=False))
        self.state_dir = default_state_dir() if state_dir is None else state_dir
        self.max_workers, self.read_timeout = max_workers, read_timeout
        self.max_request_bytes = max_request_bytes
        self._token = secrets.token_hex(32)
        self._server = self._thread = self._discovery = None
        self._stopped = False
        self.discovery_path = None

    def session(self):
        result = copy.deepcopy(self._session_info)
        result.pop("token", None)
        result.update(
            bridge_protocol=PROTOCOL_VERSION,
            plugin_version=PLUGIN_VERSION,
            instance_id=self.ledger.instance_id,
            ledger=self.ledger.status(),
        )
        return result

    def start(self):
        if self._stopped:
            raise BridgeError(
                "BRIDGE_STOPPED", "Create a new instance and ledger to restart the bridge"
            )
        if self._server is not None:
            return copy.deepcopy(self._discovery)
        server = _HTTPServer(self)
        data = {
            "bridge_protocol": PROTOCOL_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "krita_version": self._session_info.get("krita_version", "unknown"),
            "pid": os.getpid(),
            "port": server.server_port,
            "instance_id": self.ledger.instance_id,
            "token": self._token,
        }
        try:
            self.discovery_path = write_discovery(self.state_dir, data)
            self._server, self._discovery = server, data
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="krita6-bridge-http",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            server.server_close()
            remove_discovery(self.discovery_path, data)
            self._server = None
            raise
        return copy.deepcopy(data)

    def stop(self):
        self.ledger.drain()
        self._stopped = True
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.close_workers()
        server.server_close()
        self._thread.join(timeout=1)
        remove_discovery(self.discovery_path, self._discovery)
        self._server = None
