from http.client import HTTPConnection
import json
import os
import socket
import stat
import time

import pytest

from krita6_bridge.discovery import default_state_dir
from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError
from krita6_bridge.transport import ArtifactStore, BridgeServer


def operation(op="op-1"):
    return {
        "bridge_protocol": 1,
        "instance_id": "instance-a",
        "operation_id": op,
        "command": "create_document",
        "params": {"width": 32, "height": 32, "name": "Scratch"},
    }


@pytest.fixture
def bridge(tmp_path):
    ledger = OperationLedger("instance-a", queue_limit=1)
    artifacts = ArtifactStore()
    server = BridgeServer(
        ledger, {"krita_version": "mock-no-host", "capabilities": []}, artifacts, state_dir=tmp_path
    )
    discovery = server.start()
    yield server, discovery
    server.stop()


def http(discovery, path="/v1/session", *, method="GET", body=None, headers=None):
    request_headers = {
        "Host": "127.0.0.1:" + str(discovery["port"]),
        "Authorization": "Bearer " + discovery["token"],
    }
    if body is not None:
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    connection = HTTPConnection("127.0.0.1", discovery["port"], timeout=2)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        if response.headers.get_content_type() == "application/json":
            data = json.loads(data)
        return response.status, data, dict(response.headers)
    finally:
        connection.close()


def test_auth_host_origin_protocol_and_stale_instance_are_admission_guards(bridge):
    server, discovery = bridge
    for headers, code in [
        ({"Authorization": "Bearer wrong"}, "UNAUTHORIZED"),
        ({"Authorization": ""}, "UNAUTHORIZED"),
        ({"Host": "localhost:" + str(discovery["port"])}, "INVALID_HOST"),
        ({"Origin": "https://example.test"}, "ORIGIN_FORBIDDEN"),
        ({"Origin": ""}, "ORIGIN_FORBIDDEN"),
    ]:
        status, data, _ = http(
            discovery, "/v1/operations", method="POST", body=operation(), headers=headers
        )
        assert status in {401, 403}
        assert data["error"]["code"] == code
    for changes, code in [
        ({"bridge_protocol": 2}, "PROTOCOL_MISMATCH"),
        ({"instance_id": "old-instance"}, "INSTANCE_MISMATCH"),
    ]:
        status, data, _ = http(
            discovery, "/v1/operations", method="POST", body={**operation(), **changes}
        )
        assert status == 409
        assert data["error"]["code"] == code
    assert server.ledger.status()["mutations"] == 0


def test_queue_saturation_leaves_status_cancel_and_original_id_available(bridge):
    server, discovery = bridge
    status, queued, _ = http(discovery, "/v1/operations", method="POST", body=operation())
    assert status == 202 and queued["state"] == "queued"
    assert http(discovery, "/v1/operations", method="POST", body=operation())[1] == queued
    assert http(discovery, "/v1/operations", method="POST", body=operation("other"))[0] == 429
    status, session, headers = http(discovery)
    assert status == 200 and session["ledger"]["queued"] == 1
    assert "token" not in session and discovery["token"] not in json.dumps(session)
    assert headers["Cache-Control"] == "no-store"
    assert http(discovery, "/v1/operations/op-1")[1]["state"] == "queued"
    assert (
        http(discovery, "/v1/operations/op-1/cancel", method="POST", body={"instance_id": "old"})[0]
        == 409
    )
    status, cancelled, _ = http(
        discovery, "/v1/operations/op-1/cancel", method="POST", body={"instance_id": "instance-a"}
    )
    assert status == 200 and cancelled["state"] == "cancelled"
    assert server.ledger.take_next() is None
    assert http(discovery, "/v1/operations", method="POST", body=operation("other"))[0] == 202
    request = server.ledger.take_next()
    server.ledger.finish(request["operation_id"], result={"document_id": "scratch"})
    assert http(discovery, "/v1/operations/other")[1]["result"] == {"document_id": "scratch"}


@pytest.mark.parametrize(
    "body,headers,expected",
    [
        (b'{"command":"list_documents","command":"run_python"}', {}, "INVALID_REQUEST"),
        (b'{"value":NaN}', {}, "INVALID_REQUEST"),
        (b"[]", {}, "INVALID_REQUEST"),
        (b"\xff", {}, "INVALID_REQUEST"),
        (b"{}", {"Content-Type": "text/plain"}, "UNSUPPORTED_MEDIA_TYPE"),
        (b"{}", {"Content-Length": "1048577"}, "REQUEST_TOO_LARGE"),
        (b"{}", {"Transfer-Encoding": "chunked"}, "INVALID_REQUEST"),
    ],
)
def test_http_validation_precedes_admission(bridge, body, headers, expected):
    server, discovery = bridge
    status, data, _ = http(discovery, "/v1/operations", method="POST", body=body, headers=headers)
    assert status >= 400
    assert data["error"]["code"] == expected
    assert server.ledger.status()["queued"] == 0


def test_duplicate_authorization_header_is_rejected(bridge):
    _, d = bridge
    message = (
        f"GET /v1/session HTTP/1.1\r\nHost: 127.0.0.1:{d['port']}\r\n"
        f"Authorization: Bearer {d['token']}\r\nAuthorization: Bearer wrong\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", d["port"]), timeout=1) as client:
        client.sendall(message.encode())
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
    response = b"".join(chunks)
    assert b"400 Bad Request" in response
    assert b"INVALID_REQUEST" in response
    assert d["token"].encode() not in response


def test_artifact_access_is_authenticated_and_storage_has_independent_bounds(bridge):
    server, d = bridge
    data = b"\x89PNG\r\n\x1a\n" + b"fake-test-payload"
    record = server.artifacts.put(data)
    path = "/v1/artifacts/" + record["artifact_id"]
    status, result, headers = http(d, path)
    assert status == 200 and result == data
    assert headers["Content-Type"] == "image/png"
    assert http(d, path, headers={"Authorization": ""})[0] == 401
    assert http(d, "/v1/artifacts/missing")[0] == 404
    clock = [0.0]
    store = ArtifactStore(
        max_count=2, max_bytes=6, max_artifact_bytes=4, ttl=1, clock=lambda: clock[0]
    )
    first = store.put(b"abc")
    store.put(b"def")
    with pytest.raises(BridgeError) as error:
        store.put(b"a")
    assert error.value.code == "ARTIFACT_STORE_FULL"
    with pytest.raises(BridgeError) as error:
        store.put(b"12345")
    assert error.value.code == "ARTIFACT_TOO_LARGE"
    clock[0] = 2
    with pytest.raises(BridgeError) as error:
        store.get(first["artifact_id"])
    assert error.value.code == "ARTIFACT_NOT_FOUND"
    assert store.put(b"abc")["size_bytes"] == 3


def test_discovery_is_private_atomic_and_only_removed_by_its_owner(bridge):
    server, d = bridge
    path = server.discovery_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == d
    assert len(d["token"]) == 64
    assert not list(path.parent.glob(".discovery-*"))
    replacement = {**d, "token": "a-different-token"}
    path.write_text(json.dumps(replacement))
    server.stop()
    assert json.loads(path.read_text()) == replacement


def test_stop_removes_discovery_cancels_queued_and_forbids_reusing_instance(bridge):
    server, d = bridge
    server.ledger.admit(operation())
    path = server.discovery_path
    server.stop()
    assert not path.exists()
    assert server.ledger.get("op-1")["state"] == "cancelled"
    with pytest.raises(BridgeError) as error:
        server.start()
    assert error.value.code == "BRIDGE_STOPPED"


def test_insecure_or_symlink_state_directory_is_rejected(tmp_path):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    link = tmp_path / "link"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link.symlink_to(private, target_is_directory=True)
    for path in (insecure, link):
        server = BridgeServer(OperationLedger("instance-a"), {}, ArtifactStore(), state_dir=path)
        with pytest.raises(BridgeError) as error:
            server.start()
        assert error.value.code == "INSECURE_STATE_DIR"
        assert not list(path.glob("instance-*.json"))


def test_discovery_environment_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("KRITA6_MCP_STATE_DIR", str(tmp_path / "override"))
    assert default_state_dir() == tmp_path / "override"
    monkeypatch.delenv("KRITA6_MCP_STATE_DIR")
    if os.name == "posix" and os.sys.platform != "darwin":
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert default_state_dir() == tmp_path / "state" / "krita6-mcp"


def test_worker_limit_and_absolute_deadline_recover_from_trickled_connections(tmp_path):
    server = BridgeServer(
        OperationLedger("instance-a"),
        {},
        ArtifactStore(),
        state_dir=tmp_path,
        max_workers=2,
        read_timeout=0.25,
    )
    d = server.start()
    clients = []
    try:
        for _ in range(2):
            client = socket.create_connection(("127.0.0.1", d["port"]), timeout=1)
            client.sendall(b"GET /v1/session HTTP/1.1\r\n")
            clients.append(client)
        deadline = time.monotonic() + 1
        while len(server._server._active) != 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(server._server._active) == 2
        with socket.create_connection(("127.0.0.1", d["port"]), timeout=1) as excess:
            assert excess.recv(1) == b""
        # Bytes arriving faster than socket timeout still cannot renew admission.
        for _ in range(10):
            for client in clients:
                try:
                    client.sendall(b"X")
                except OSError:
                    pass
            time.sleep(0.04)
        assert len(server._server._active) == 0
        assert http(d)[0] == 200
    finally:
        for client in clients:
            client.close()
        server.stop()


def test_shutdown_closes_idle_workers_without_waiting_for_deadline(tmp_path):
    server = BridgeServer(
        OperationLedger("instance-a"), {}, ArtifactStore(), state_dir=tmp_path, read_timeout=20
    )
    d = server.start()
    with socket.create_connection(("127.0.0.1", d["port"]), timeout=1) as client:
        client.sendall(b"GET /v1/session HTTP/1.1\r\n")
        start = time.monotonic()
        server.stop()
        assert time.monotonic() - start < 1
        try:
            assert client.recv(1) == b""
        except ConnectionResetError:
            pass  # The listener can close an accepted-but-not-yet-parsed socket.
