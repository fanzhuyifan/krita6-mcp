"""Exercise the production plugin via real MCP stdio in an isolated Krita profile."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
import traceback
from zipfile import ZipFile

from mcp import Client, StdioServerParameters

from probe_krita import isolated_environment, launch_krita


async def scenario(base, instance_id):
    env = dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state"))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "krita6_mcp.cli", "serve"],
        env=env,
    )
    checks = {}
    async with Client(parameters) as client:

        async def call(tool_name, **arguments):
            (base / "progress.json").write_text(
                json.dumps({"tool": tool_name, "arguments": arguments})
            )
            response = await client.call_tool(tool_name, {"instance_id": instance_id, **arguments})
            if response.is_error:
                raise AssertionError(f"{tool_name}: {response.structured_content}")
            data = response.structured_content
            deadline = time.monotonic() + 30
            while data.get("state") in {"queued", "running", "cancel_requested"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{tool_name} still pending: {data['operation_id']}")
                await asyncio.sleep(0.05)
                response = await client.call_tool(
                    "krita_get_operation",
                    {
                        "instance_id": instance_id,
                        "operation_id": data["operation_id"],
                    },
                )
                if response.is_error:
                    raise AssertionError(f"{tool_name}: {response.structured_content}")
                data = response.structured_content
            return response, data.get("result", data)

        _, session = await call("krita_status")
        listed = await client.list_tools()
        tools = listed.tools if hasattr(listed, "tools") else listed
        checks["tool_count"] = len(tools)
        assert len(tools) == 48
        _, diffusion = await call("krita_diffusion_status")
        assert diffusion["availability"] == "not_loaded"
        assert diffusion["generation_control"] is False
        checks["optional_diffusion_absent"] = True
        _, created = await call(
            "krita_create_document",
            operation_id="smoke-create",
            width=160,
            height=120,
            name="MCP smoke",
        )
        document_id = created["document_id"]
        _, repeated = await call(
            "krita_create_document",
            operation_id="smoke-create",
            width=160,
            height=120,
            name="MCP smoke",
        )
        assert repeated == created
        _, inventory = await call("krita_list_documents")
        assert len(inventory["documents"]) == 1
        checks["duplicate_document_dispatched_once"] = True
        _, layer = await call(
            "krita_create_paint_layer",
            operation_id="smoke-layer",
            document_id=document_id,
            name="Native strokes",
        )
        _, presets = await call("krita_list_brush_presets", query="Basic-5 Size")
        assert presets["presets"], "Bundled Basic-5 Size preset missing"
        preset_id = presets["presets"][0]["preset_id"]
        target = {
            "document_id": document_id,
            "node_id": layer["node_id"],
            "preset_id": preset_id,
            "size_px": 12.0,
            "opacity": 1.0,
        }
        _, painted = await call(
            "krita_paint_path",
            operation_id="smoke-path",
            **target,
            color="#ff0000",
            points=[[20, 30], [80, 70], [140, 30]],
        )
        assert painted["native_strokes"] == 1
        await call(
            "krita_paint_line",
            operation_id="smoke-line",
            **target,
            color="#0000ff",
            start=[20, 95],
            end=[140, 95],
            pressure_start=0.2,
            pressure_end=1.0,
        )
        checks["native_path_and_line"] = True
        response, preview = await call("krita_get_preview", document_id=document_id, max_edge=160)
        images = [item for item in response.content if item.type == "image"]
        assert len(images) == 1
        png = base64.b64decode(images[0].data)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", png[16:24]) == (160, 120)
        (base / "preview.png").write_bytes(png)
        checks["inline_png"] = True
        _, before = await call("krita_inspect_document", document_id=document_id)
        assert any(node["node_id"] == layer["node_id"] for node in before["layers"])
        await call(
            "krita_save_document",
            operation_id="smoke-save",
            document_id=document_id,
            root="scratch",
            path="smoke.kra",
        )
        await call(
            "krita_export_png",
            operation_id="smoke-export",
            document_id=document_id,
            root="scratch",
            path="smoke.png",
        )
        _, after = await call("krita_inspect_document", document_id=document_id)
        assert after["filename"].endswith("smoke.kra")
        assert not after["modified"]
        with ZipFile(base / "artwork" / "smoke.kra") as archive:
            assert b"Native strokes" in archive.read("maindoc.xml")
            assert archive.read("mergedimage.png").startswith(b"\x89PNG")
        checks["layered_save_and_separate_export"] = True
        denied = await client.call_tool(
            "krita_export_png",
            {
                "instance_id": instance_id,
                "operation_id": "smoke-no-overwrite",
                "document_id": document_id,
                "root": "scratch",
                "path": "smoke.png",
            },
        )
        assert denied.is_error
        checks["overwrite_rejected"] = True
        return {
            "passed": True,
            "session": session,
            "checks": checks,
            "preview": preview,
            "preview_sha256": hashlib.sha256(png).hexdigest(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--krita", default="krita")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = (args.output or Path(tempfile.mkdtemp(prefix="krita6-smoke-"))).resolve()
    if (base / "report.json").exists():
        parser.error("Output already contains a report; choose a new directory.")
    env = isolated_environment(base)
    (base / "artwork").mkdir(exist_ok=True)
    plugins = base / "data" / "krita" / "pykrita"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        root / "plugin" / "krita6_bridge",
        plugins / "krita6_bridge",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    shutil.copyfile(root / "plugin" / "krita6_bridge.desktop", plugins / "krita6_bridge.desktop")
    (base / "config" / "kritarc").write_text("[python]\nenable_krita6_bridge=true\n")
    env.update(
        KRITA6_MCP_STATE_DIR=str(base / "state"),
        KRITA6_MCP_OUTPUT_ROOTS=json.dumps({"scratch": str(base / "artwork")}),
        KRITA6_MCP_AUTOSTART="1",
    )
    report = {"passed": False}
    with launch_krita(base, args.krita, env) as process:
        deadline = time.monotonic() + 90
        try:
            while True:
                discovery = list((base / "state").glob("instance-*.json"))
                if discovery:
                    # Never copy the token into retained test output.
                    instance_id = json.loads(discovery[0].read_text())["instance_id"]
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"Bridge did not start. Inspect {base / 'krita.log'}")
                time.sleep(0.1)
            report = asyncio.run(scenario(base, instance_id))
        except Exception as error:
            report["error"] = str(error)
            report["traceback"] = traceback.format_exc()
        finally:
            (base / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {base}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
