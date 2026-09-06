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
from krita6_bridge.protocol import BridgeError
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
            elif request["command"] == "diffusion_status":
                result = {"available": False, "reason": "PLUGIN_NOT_LOADED"}
                effect = "none"
            elif request["command"] == "inspect_diffusion_document":
                result = {"document_id": request["target"]["document_id"], "model_present": False}
                effect = "none"
            elif request["command"] == "list_diffusion_jobs":
                result = {
                    "document_id": request["target"]["document_id"],
                    "jobs": [],
                    "offset": request["params"]["offset"],
                    "next_offset": None,
                }
                effect = "none"
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
            assert len(by_name) == 16
            assert by_name["krita_status"].annotations.read_only_hint
            diffusion_tools = {
                "krita_diffusion_status",
                "krita_inspect_diffusion_document",
                "krita_list_diffusion_jobs",
            }
            assert {name for name in by_name if "diffusion" in name} == diffusion_tools
            for name in diffusion_tools:
                assert by_name[name].annotations.read_only_hint
                assert not by_name[name].annotations.destructive_hint
                assert "operation_id" not in by_name[name].input_schema["properties"]
            assert "operation_id" in by_name["krita_paint_path"].input_schema["required"]
            assert "pressure" not in by_name["krita_paint_path"].input_schema["properties"]
            status = await client.call_tool("krita_status", {})
            assert not status.is_error
            assert status.structured_content["available"]
            assert discovery["token"] not in str(status)
            diffusion = await client.call_tool(
                "krita_diffusion_status", {"instance_id": "integration"}
            )
            assert not diffusion.is_error
            assert diffusion.structured_content["effect"] == "none"
            assert diffusion.structured_content["result"] == {
                "available": False,
                "reason": "PLUGIN_NOT_LOADED",
            }
            diffusion_document = await client.call_tool(
                "krita_inspect_diffusion_document",
                {"instance_id": "integration", "document_id": "scratch"},
            )
            assert not diffusion_document.is_error
            assert diffusion_document.structured_content["result"]["document_id"] == "scratch"
            for page in ({}, {"offset": 12, "limit": 10}):
                jobs = await client.call_tool(
                    "krita_list_diffusion_jobs",
                    {"instance_id": "integration", "document_id": "scratch", **page},
                )
                assert not jobs.is_error
                assert jobs.structured_content["effect"] == "none"
                assert jobs.structured_content["result"]["offset"] == page.get("offset", 0)
            for invalid_page in ({"offset": True}, {"offset": 2**31}, {"limit": 101}, {"limit": 0}):
                jobs = await client.call_tool(
                    "krita_list_diffusion_jobs",
                    {"instance_id": "integration", "document_id": "scratch", **invalid_page},
                )
                assert jobs.is_error
            for name, forbidden in (
                ("krita_diffusion_status", {"connect": True}),
                (
                    "krita_inspect_diffusion_document",
                    {"document_id": "scratch", "prompt": "generate"},
                ),
                ("krita_list_diffusion_jobs", {"document_id": "scratch", "cancel": True}),
            ):
                rejected = await client.call_tool(name, {"instance_id": "integration", **forbidden})
                assert rejected.is_error
                assert rejected.structured_content["error"]["code"] == "INVALID_PARAMETERS"
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
            retrieved_preview = await client.call_tool(
                "krita_get_operation",
                {
                    "instance_id": "integration",
                    "operation_id": preview.structured_content["operation_id"],
                },
            )
            assert not retrieved_preview.is_error
            assert retrieved_preview.structured_content == preview.structured_content
            assert retrieved_preview.content == preview.content
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
        diffusion_reads = [item for item in dispatched if "diffusion" in item["command"]]
        assert [item["command"] for item in diffusion_reads] == [
            "diffusion_status",
            "inspect_diffusion_document",
            "list_diffusion_jobs",
            "list_diffusion_jobs",
        ]
        assert diffusion_reads[0]["target"] == diffusion_reads[0]["params"] == {}
        assert diffusion_reads[1]["target"] == {"document_id": "scratch"}
        assert diffusion_reads[2]["params"] == {"offset": 0, "limit": 50}
        assert diffusion_reads[3]["params"] == {"offset": 12, "limit": 10}
        assert ledger.status()["mutations"] == 1
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


@pytest.mark.parametrize("tool_name", ["krita_get_preview", "krita_get_operation"])
def test_preview_retrieval_failure_preserves_operation_and_metadata(tool_name):
    snapshot = {
        "instance_id": "preview-test",
        "operation_id": "preview-1",
        "command": "get_preview",
        "state": "succeeded",
        "effect": "none",
        "result": {"artifact_id": "image-1", "preview_width": 160, "preview_height": 120},
    }

    class PreviewBridge:
        def execute(self, command, **kwargs):
            return snapshot

        def get_operation(self, instance_id, operation_id):
            return snapshot

        def get_artifact(self, instance_id, artifact_id):
            assert (instance_id, artifact_id) == ("preview-test", "image-1")
            raise BridgeError("ARTIFACT_NOT_FOUND", "Preview is no longer available")

    params = {"instance_id": "preview-test"}
    if tool_name == "krita_get_preview":
        params["document_id"] = "scratch"
    else:
        params["operation_id"] = "preview-1"
    response = asyncio.run(create_server(PreviewBridge()).call_tool(tool_name, params))
    assert response.is_error
    assert response.structured_content == {
        **snapshot,
        "preview_error": {
            "code": "ARTIFACT_NOT_FOUND",
            "message": "Preview is no longer available",
        },
    }
    assert all(item.type != "image" for item in response.content)


@pytest.mark.parametrize(
    ("command", "state", "error"),
    [
        ("export_png", "succeeded", None),
        ("get_preview", "queued", None),
        ("get_preview", "failed", None),
        ("get_preview", "succeeded", {"code": "RESULT_EXPIRED"}),
    ],
)
def test_operation_only_retrieves_artifacts_for_successful_previews(command, state, error):
    snapshot = {
        "command": command,
        "state": state,
        "error": error,
        "result": {"artifact_id": "image-1"},
    }

    class OperationBridge:
        def get_operation(self, instance_id, operation_id):
            return snapshot

        def get_artifact(self, instance_id, artifact_id):
            pytest.fail("An unrelated or unfinished operation must not fetch an image")

    response = asyncio.run(
        create_server(OperationBridge()).call_tool(
            "krita_get_operation", {"instance_id": "preview-test", "operation_id": "operation-1"}
        )
    )
    assert response.structured_content == snapshot
    assert bool(response.is_error) == bool(error or state == "failed")
    assert all(item.type != "image" for item in response.content)
