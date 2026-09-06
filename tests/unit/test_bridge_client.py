import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from krita6_bridge.protocol import BridgeError
from krita6_mcp.bridge_client import MAX_DISCOVERY_BYTES, BridgeClient
from krita6_mcp.cli import main

TOKEN = "a" * 64


@contextmanager
def bridge_endpoint(tmp_path, handler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            handler(self)

        def do_POST(self):
            handler(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_port
    discovery = tmp_path / "instance-test.json"
    discovery.write_text(
        json.dumps({"bridge_protocol": 1, "instance_id": "test", "port": port, "token": TOKEN})
    )
    discovery.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield BridgeClient(tmp_path, timeout=0.3), port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def reply(request, data, status=200):
    encoded = json.dumps(data).encode()
    request.send_response(status)
    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(encoded)))
    request.end_headers()
    request.wfile.write(encoded)


def session(request):
    reply(request, {"bridge_protocol": 1, "instance_id": "test", "krita_version": "6.0-test"})


def test_discovery_uses_auth_and_ignores_proxy(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.path, request.headers["Authorization"], request.headers["Host"]))
        session(request)

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with bridge_endpoint(tmp_path, handler) as (client, port):
        assert client.status()["available"]
        assert seen == [("/v1/session", f"Bearer {TOKEN}", f"127.0.0.1:{port}")]
        assert TOKEN not in repr(client._connections())


def test_redirect_never_followed(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.path)
        request.send_response(302)
        request.send_header("Location", "/credential-trap")
        request.send_header("Content-Length", "0")
        request.end_headers()

    with bridge_endpoint(tmp_path, handler) as (client, _):
        with pytest.raises(BridgeError) as error:
            client.status("test")
        assert error.value.code == "INVALID_BRIDGE_RESPONSE"
        assert seen == ["/v1/session"]


def test_lost_mutation_ack_is_not_replayed(tmp_path):
    submissions = []

    def handler(request):
        if request.path == "/v1/session":
            session(request)
        else:
            submissions.append(
                json.loads(request.rfile.read(int(request.headers["Content-Length"])))
            )
            request.close_connection = True

    with bridge_endpoint(tmp_path, handler) as (client, _):
        with pytest.raises(BridgeError) as error:
            client.execute(
                "create_document",
                instance_id="test",
                operation_id="paint-1",
                params={"width": 32, "height": 32, "name": "scratch"},
            )
        assert error.value.code == "OUTCOME_UNKNOWN"
        assert error.value.effect == "unknown"
        assert len(submissions) == 1
        assert submissions[0]["operation_id"] == "paint-1"


def test_pending_operation_retains_identity(tmp_path):
    submitted = []

    def handler(request):
        if request.path == "/v1/session":
            session(request)
            return
        data = json.loads(request.rfile.read(int(request.headers["Content-Length"])))
        submitted.append(data)
        reply(
            request,
            {
                "instance_id": "test",
                "operation_id": data["operation_id"],
                "command": data["command"],
                "state": "queued",
                "effect": "none",
                "result": None,
                "error": None,
            },
            202,
        )

    with bridge_endpoint(tmp_path, handler) as (client, _):
        result = client.execute(
            "create_document",
            instance_id="test",
            operation_id="paint-2",
            params={"width": 32, "height": 32, "name": "scratch"},
            wait_timeout=0,
        )
        assert result["state"] == "queued"
        assert result["operation_id"] == "paint-2"
        assert len(submitted) == 1
        with pytest.raises(BridgeError):
            client.execute(
                "create_document",
                instance_id="test",
                params={"width": 32, "height": 32, "name": "scratch"},
            )
        assert len(submitted) == 1


def test_doctor_empty_state_is_read_only(tmp_path, capsys):
    assert main(["doctor", "--state-dir", str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {"bridge_protocol": 1, "available": False, "instances": []}
    assert list(tmp_path.iterdir()) == []


def test_wrong_instance_does_not_receive_mutation(tmp_path):
    paths = []

    def handler(request):
        paths.append(request.path)
        reply(request, {"bridge_protocol": 1, "instance_id": "replacement"})

    with bridge_endpoint(tmp_path, handler) as (client, _):
        with pytest.raises(BridgeError) as error:
            client.execute(
                "create_document",
                instance_id="test",
                operation_id="edit-1",
                params={"width": 16, "height": 16, "name": "scratch"},
            )
        assert error.value.code == "INSTANCE_MISMATCH"
        assert paths == ["/v1/session"]


def broken_error_reply(request, fault, release):
    error_body = json.dumps(
        {"error": {"code": "QUEUE_FULL", "message": "Queue full", "effect": "none"}}
    ).encode()
    request.send_response(400)
    request.send_header("Content-Type", "application/json")
    if fault == "incomplete_chunk":
        request.send_header("Transfer-Encoding", "chunked")
        request.end_headers()
        request.wfile.write(b"40\r\n{")
    else:
        if fault == "oversized":
            body = error_body + b" " * (MAX_DISCOVERY_BYTES + 1 - len(error_body))
        elif fault == "malformed":
            body = b'{"error":'
        elif fault == "deep_json":
            body = b"[" * 2000 + b"]" * 2000
        else:
            body = error_body
        declared_length = len(body) + (1 if fault in {"truncated", "stalled"} else 0)
        request.send_header("Content-Length", str(declared_length))
        request.end_headers()
        request.wfile.write(body)
    request.wfile.flush()
    if fault == "stalled":
        release.wait(timeout=2)
    request.close_connection = True


@pytest.mark.parametrize(
    "fault", ["truncated", "stalled", "incomplete_chunk", "oversized", "malformed", "deep_json"]
)
def test_broken_http_error_preserves_uncertain_mutation_identity(tmp_path, fault):
    submitted = []
    release = threading.Event()

    def handler(request):
        if request.path == "/v1/session":
            session(request)
            return
        submitted.append(json.loads(request.rfile.read(int(request.headers["Content-Length"]))))
        broken_error_reply(request, fault, release)

    with bridge_endpoint(tmp_path, handler) as (client, _):
        client.timeout = 0.1
        try:
            with pytest.raises(BridgeError) as error:
                client.execute(
                    "create_document",
                    instance_id="test",
                    operation_id="uncertain-once",
                    params={"width": 16, "height": 16, "name": "scratch"},
                )
            assert error.value.code == "OUTCOME_UNKNOWN"
            assert error.value.effect == "unknown"
            assert "uncertain-once" in error.value.message
            assert len(submitted) == 1
            assert submitted[0]["operation_id"] == "uncertain-once"
        finally:
            release.set()


@pytest.mark.parametrize("fault", ["stalled", "incomplete_chunk", "oversized"])
def test_broken_http_poll_error_retains_snapshot_for_reconciliation(tmp_path, fault):
    submitted = []
    release = threading.Event()
    snapshot = {
        "instance_id": "test",
        "operation_id": "still-running",
        "command": "create_document",
        "state": "running",
        "effect": "unknown",
        "result": None,
        "error": None,
    }

    def handler(request):
        if request.path == "/v1/session":
            session(request)
        elif request.command == "POST":
            submitted.append(json.loads(request.rfile.read(int(request.headers["Content-Length"]))))
            reply(request, snapshot, status=202)
        else:
            broken_error_reply(request, fault, release)

    with bridge_endpoint(tmp_path, handler) as (client, _):
        client.timeout = 0.1
        try:
            result = client.execute(
                "create_document",
                instance_id="test",
                operation_id="still-running",
                params={"width": 16, "height": 16, "name": "scratch"},
            )
            assert result == {**snapshot, "reconciliation_required": True}
            assert len(submitted) == 1
            assert submitted[0]["operation_id"] == "still-running"
        finally:
            release.set()


def test_complete_http_error_preserves_domain_rejection(tmp_path):
    submitted = []

    def handler(request):
        if request.path == "/v1/session":
            session(request)
            return
        submitted.append(json.loads(request.rfile.read(int(request.headers["Content-Length"]))))
        reply(
            request,
            {"error": {"code": "QUEUE_FULL", "message": "Queue full", "effect": "none"}},
            status=429,
        )

    with bridge_endpoint(tmp_path, handler) as (client, _):
        with pytest.raises(BridgeError) as error:
            client.execute(
                "create_document",
                instance_id="test",
                operation_id="unadmitted",
                params={"width": 16, "height": 16, "name": "scratch"},
            )
        assert error.value.code == "QUEUE_FULL"
        assert error.value.effect == "none"
        assert len(submitted) == 1
