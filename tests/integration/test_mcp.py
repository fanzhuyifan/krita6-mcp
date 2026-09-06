"""Real SDK stdio and HTTP integration with a plain-data fake host.

These checks establish adapter behavior, not Krita or native-brush compatibility.
"""

import asyncio
import base64
import os
import sys
import threading

import pytest
from mcp import Client, StdioServerParameters

from krita6_bridge.operations import OperationLedger
from krita6_bridge.transport import ArtifactStore, BridgeServer
from krita6_mcp.server import create_server

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l1sAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_stdio_tools_mutations_and_inline_preview(tmp_path, mode):
    ledger = OperationLedger("integration")
    artifacts = ArtifactStore()
    server = BridgeServer(
        ledger,
        {"krita_version": "6.0-test", "capabilities": {"host_validated": False}},
        artifacts,
        state_dir=tmp_path,
    )
    discovery = server.start()
    stop = threading.Event()
    dispatched = []

    def worker():
        while not stop.wait(0.005):
            request = ledger.take_next()
            if request is None:
                continue
            dispatched.append(request)
            if request["command"] == "get_preview":
                result = {**artifacts.put(PNG), "preview_width": 1, "preview_height": 1}
                effect = "none"
            elif request["command"] == "create_document":
                result = {"document_id": "scratch"}
                effect = "applied"
            else:
                result = {"documents": []}
                effect = "none"
            ledger.finish(request["operation_id"], result=result, effect=effect)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    async def scenario():
        environment = {**os.environ, "KRITA6_MCP_STATE_DIR": str(tmp_path)}
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "krita6_mcp.cli", "serve"], env=environment
        )
        async with Client(parameters, mode=mode) as client:
            listed = await client.list_tools()
            tools = listed.tools if hasattr(listed, "tools") else listed
            by_name = {tool.name: tool for tool in tools}
            assert len(by_name) == 13
            assert by_name["krita_status"].annotations.read_only_hint
            assert "operation_id" in by_name["krita_paint_path"].input_schema["required"]
            assert "pressure" not in by_name["krita_paint_path"].input_schema["properties"]
            status = await client.call_tool("krita_status", {})
            assert not status.is_error
            assert status.structured_content["available"]
            assert discovery["token"] not in str(status)
            params = {
                "instance_id": "integration",
                "operation_id": "make-scratch",
                "width": 32,
                "height": 32,
                "name": "scratch",
            }
            first = await client.call_tool("krita_create_document", params)
            second = await client.call_tool("krita_create_document", params)
            assert not first.is_error and not second.is_error
            assert first.structured_content == second.structured_content
            conflict = await client.call_tool("krita_create_document", {**params, "width": 64})
            assert conflict.is_error
            assert conflict.structured_content["error"]["code"] == "OPERATION_ID_CONFLICT"
            preview = await client.call_tool(
                "krita_get_preview",
                {"instance_id": "integration", "document_id": "scratch", "max_edge": 32},
            )
            assert not preview.is_error
            images = [item for item in preview.content if item.type == "image"]
            assert len(images) == 1
            assert base64.b64decode(images[0].data) == PNG
            invalid = await client.call_tool(
                "krita_create_document", {**params, "operation_id": "bad-number", "width": True}
            )
            assert invalid.is_error
            unknown = await client.call_tool("krita_create_document", {**params, "typo": 5})
            assert unknown.is_error
            assert unknown.structured_content["error"]["code"] == "INVALID_PARAMETERS"

    try:
        asyncio.run(scenario())
        assert len([item for item in dispatched if item["command"] == "create_document"]) == 1
    finally:
        stop.set()
        thread.join(timeout=2)
        server.stop()


def test_expired_result_reports_retrieval_error_without_replay():
    now = [0.0]
    ledger = OperationLedger("expiry-test", result_ttl=1, clock=lambda: now[0])
    dispatched = []

    class LedgerBridge:
        def execute(self, command, *, instance_id, operation_id, target, params):
            snapshot = ledger.admit(
                {
                    "bridge_protocol": 1,
                    "instance_id": instance_id,
                    "operation_id": operation_id,
                    "command": command,
                    "target": target or {},
                    "params": params,
                }
            )
            if snapshot["state"] == "queued":
                dispatched.append(ledger.take_next())
                return ledger.finish(operation_id, result={"document_id": "scratch"})
            return snapshot

        def get_operation(self, instance_id, operation_id):
            assert instance_id == ledger.instance_id
            return ledger.get(operation_id)

    server = create_server(LedgerBridge())
    params = {
        "instance_id": "expiry-test",
        "operation_id": "create-once",
        "width": 32,
        "height": 32,
        "name": "scratch",
    }

    async def scenario():
        original = await server.call_tool("krita_create_document", params)
        assert not original.is_error
        now[0] = 2.0
        retrieved = await server.call_tool(
            "krita_get_operation",
            {
                "instance_id": "expiry-test",
                "operation_id": "create-once",
            },
        )
        retried = await server.call_tool("krita_create_document", params)
        for response in (retrieved, retried):
            assert response.is_error
            assert response.structured_content["state"] == "succeeded"
            assert response.structured_content["effect"] == "applied"
            assert response.structured_content["result"] is None
            assert response.structured_content["error"]["code"] == "RESULT_EXPIRED"
        assert len(dispatched) == 1

    asyncio.run(scenario())
