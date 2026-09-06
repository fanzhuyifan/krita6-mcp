"""Bounded, authenticated client for the private Krita bridge.

This module intentionally has no dependency on Qt or the MCP runtime.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from krita6_bridge.discovery import default_state_dir
from krita6_bridge.protocol import PROTOCOL_VERSION, BridgeError, validate_request

MAX_DISCOVERY_BYTES = 16_384
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Connection:
    instance_id: str
    port: int
    token: str = field(repr=False)
    bridge_protocol: int = PROTOCOL_VERSION


class BridgeClient:
    def __init__(self, state_dir: str | Path | None = None, timeout: float = 5.0):
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self.timeout = timeout
        # Local traffic must never inherit HTTP(S)_PROXY, nor forward credentials
        # through a redirect, even to another loopback port.
        self._opener = build_opener(ProxyHandler({}), _NoRedirects())

    def _connections(self) -> list[Connection]:
        found = []
        for path in sorted(self.state_dir.glob("instance-*.json")):
            try:
                with path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DISCOVERY_BYTES:
                        continue
                    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                        continue
                    data = json.loads(stream.read(MAX_DISCOVERY_BYTES + 1))
                if not isinstance(data, dict):
                    continue
                instance_id, port, token = data["instance_id"], data["port"], data["token"]
                if (
                    not isinstance(instance_id, str)
                    or not ID_PATTERN.fullmatch(instance_id)
                    or type(port) is not int
                    or not 1 <= port <= 65535
                    or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)
                    or type(data.get("bridge_protocol")) is not int
                ):
                    continue
                found.append(Connection(instance_id, port, token, data["bridge_protocol"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return found

    def _request(
        self,
        connection: Connection,
        path: str,
        *,
        body: dict | None = None,
        binary: bool = False,
    ) -> dict | bytes:
        headers = {
            "Authorization": f"Bearer {connection.token}",
            "Host": f"127.0.0.1:{connection.port}",
            "Accept": "image/png" if binary else "application/json",
        }
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = Request(
            f"http://127.0.0.1:{connection.port}{path}", data=encoded, headers=headers
        )
        maximum = MAX_IMAGE_BYTES if binary else MAX_RESPONSE_BYTES
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(maximum + 1)
                content_type = response.headers.get_content_type()
        except HTTPError as exc:
            with exc:
                if 300 <= exc.code < 400:
                    raise BridgeError(
                        "INVALID_BRIDGE_RESPONSE", "Bridge redirects are forbidden"
                    ) from None
                try:
                    raw_error = exc.read(MAX_DISCOVERY_BYTES + 1)
                except (URLError, TimeoutError, OSError, HTTPException):
                    # Exceptions raised inside an except block do not reach the
                    # sibling handler below. Preserve the same uncertain-outcome
                    # handling used when a successful response is interrupted.
                    raise BridgeError(
                        "BRIDGE_UNAVAILABLE", "Could not read the Krita bridge error response"
                    ) from None
                if len(raw_error) > MAX_DISCOVERY_BYTES:
                    raise BridgeError(
                        "INVALID_BRIDGE_RESPONSE", "Bridge error response exceeds the size limit"
                    ) from None
                try:
                    expected_length = exc.headers.get("Content-Length")
                    if expected_length is not None and (
                        not expected_length.isascii()
                        or not expected_length.isdecimal()
                        or int(expected_length) != len(raw_error)
                    ):
                        raise ValueError
                    if exc.headers.get_content_type() != "application/json":
                        raise ValueError
                    error = json.loads(raw_error)["error"]
                    code = error["code"]
                    message = error["message"]
                    effect = error.get("effect", "none")
                    if not isinstance(code, str) or not isinstance(message, str):
                        raise ValueError
                    if effect not in {"none", "applied", "partial", "unknown"}:
                        raise ValueError
                except (ValueError, KeyError, TypeError, RecursionError):
                    raise BridgeError(
                        "INVALID_BRIDGE_RESPONSE",
                        f"Bridge returned invalid HTTP {exc.code} error data",
                    ) from None
            raise BridgeError(code, message, effect=effect) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise BridgeError(
                "BRIDGE_UNAVAILABLE", "Could not reach the selected Krita bridge"
            ) from None
        if len(raw) > maximum:
            raise BridgeError("INVALID_BRIDGE_RESPONSE", "Bridge response exceeds the size limit")
        if binary:
            if content_type != "image/png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BridgeError("INVALID_BRIDGE_RESPONSE", "Preview is not a PNG image")
            return raw
        if content_type != "application/json":
            raise BridgeError("INVALID_BRIDGE_RESPONSE", "Bridge response is not JSON")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise BridgeError("INVALID_BRIDGE_RESPONSE", "Bridge returned malformed JSON") from None
        if not isinstance(data, dict):
            raise BridgeError("INVALID_BRIDGE_RESPONSE", "Bridge response must be an object")
        return data

    def _session(self, connection: Connection) -> dict:
        if connection.bridge_protocol != PROTOCOL_VERSION:
            raise BridgeError(
                "PROTOCOL_MISMATCH", "The registered Krita bridge protocol is incompatible"
            )
        session = self._request(connection, "/v1/session")
        if session.get("bridge_protocol") != PROTOCOL_VERSION:
            raise BridgeError("PROTOCOL_MISMATCH", "The Krita bridge protocol is incompatible")
        if session.get("instance_id") != connection.instance_id:
            raise BridgeError("INSTANCE_MISMATCH", "The Krita bridge instance has changed")
        # The transport does not return secrets, but redact defensively before
        # allowing a discovery response to reach an MCP client or doctor stdout.
        return {
            key: value for key, value in session.items() if key not in {"token", "authorization"}
        }

    def _resolve(self, instance_id: str) -> tuple[Connection, dict]:
        self._check_id(instance_id)
        matches = [item for item in self._connections() if item.instance_id == instance_id]
        if not matches:
            raise BridgeError(
                "INSTANCE_NOT_FOUND", "The requested bridge instance is not registered"
            )
        if len(matches) != 1:
            raise BridgeError(
                "INVALID_DISCOVERY", "Multiple records claim the same bridge instance"
            )
        connection = matches[0]
        return connection, self._session(connection)

    def status(self, instance_id: str | None = None) -> dict:
        if instance_id is not None:
            return self._resolve(instance_id)[1]
        instances = []
        for connection in self._connections():
            try:
                instances.append({"reachable": True, **self._session(connection)})
            except BridgeError as exc:
                instances.append(
                    {
                        "instance_id": connection.instance_id,
                        "reachable": False,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                )
        return {
            "bridge_protocol": PROTOCOL_VERSION,
            "available": any(item["reachable"] for item in instances),
            "instances": instances,
        }

    @staticmethod
    def _check_id(value: str) -> None:
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise BridgeError("INVALID_PARAMETERS", "Malformed bridge identifier")

    @staticmethod
    def _snapshot(
        data: dict, instance_id: str, operation_id: str, command: str | None = None
    ) -> dict:
        from krita6_bridge.protocol import COMMANDS

        if (
            data.get("instance_id") != instance_id
            or data.get("operation_id") != operation_id
            or data.get("command") not in COMMANDS
            or (command is not None and data.get("command") != command)
            or data.get("state")
            not in {
                "queued",
                "running",
                "cancel_requested",
                "succeeded",
                "failed",
                "cancelled",
                "expired",
            }
            or data.get("effect") not in {"none", "applied", "partial", "unknown"}
            or not isinstance(data.get("result"), (dict, type(None)))
            or not isinstance(data.get("error"), (dict, type(None)))
        ):
            raise BridgeError(
                "INVALID_BRIDGE_RESPONSE", "Bridge operation response has invalid identity or state"
            )
        return data

    def execute(
        self,
        command: str,
        *,
        instance_id: str,
        operation_id: str | None = None,
        target: dict | None = None,
        params: dict | None = None,
        wait_timeout: float = 10.0,
    ) -> dict:
        # Mutations must receive their identity from the caller, not a retrying
        # adapter. Only read operations get disposable generated identities.
        from krita6_bridge.protocol import MUTATIONS

        if command in MUTATIONS and operation_id is None:
            raise BridgeError(
                "INVALID_PARAMETERS", "Mutations require an operation_id reused for retries"
            )
        operation_id = operation_id or f"read-{uuid.uuid4().hex}"
        request = validate_request(
            {
                "bridge_protocol": PROTOCOL_VERSION,
                "instance_id": instance_id,
                "operation_id": operation_id,
                "command": command,
                "target": target or {},
                "params": params or {},
                "queue_timeout_ms": 10_000,
            },
            instance_id,
        )
        connection, _ = self._resolve(instance_id)
        try:
            snapshot = self._snapshot(
                self._request(connection, "/v1/operations", body=request),
                instance_id,
                operation_id,
                command,
            )
        except BridgeError as exc:
            if exc.code in {"BRIDGE_UNAVAILABLE", "INVALID_BRIDGE_RESPONSE"}:
                raise BridgeError(
                    "OUTCOME_UNKNOWN",
                    f"Operation {operation_id} may have been admitted; reconcile this same operation_id",
                    effect="unknown",
                ) from None
            raise
        deadline = time.monotonic() + max(0.0, min(wait_timeout, 30.0))
        while snapshot.get("state") in {"queued", "running", "cancel_requested"}:
            if time.monotonic() >= deadline:
                return snapshot
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            try:
                snapshot = self._snapshot(
                    self._request(connection, f"/v1/operations/{operation_id}"),
                    instance_id,
                    operation_id,
                    command,
                )
            except BridgeError:
                # Last known state is retained so the caller can reconcile. No
                # mutation is replayed, and a lost poll never implies rollback.
                return {**snapshot, "reconciliation_required": True}
        return snapshot

    def get_operation(self, instance_id: str, operation_id: str) -> dict:
        self._check_id(operation_id)
        connection, _ = self._resolve(instance_id)
        return self._snapshot(
            self._request(connection, f"/v1/operations/{operation_id}"), instance_id, operation_id
        )

    def cancel_operation(self, instance_id: str, operation_id: str) -> dict:
        self._check_id(operation_id)
        connection, _ = self._resolve(instance_id)
        return self._snapshot(
            self._request(
                connection,
                f"/v1/operations/{operation_id}/cancel",
                body={"instance_id": instance_id},
            ),
            instance_id,
            operation_id,
        )

    def get_artifact(self, instance_id: str, artifact_id: str) -> bytes:
        self._check_id(artifact_id)
        connection, _ = self._resolve(instance_id)
        return self._request(connection, f"/v1/artifacts/{artifact_id}", binary=True)
