"""Validate native vector editing through real MCP in an isolated scratch host."""

import asyncio
import json
import os
import sys
import time

from mcp import Client, StdioServerParameters
import probe_editing
from probe_editing import pixel


async def scenario(base, instance_id):
    checks = {}
    serial = 0
    fixture_id = 0

    async def snapshot(action="snapshot"):
        nonlocal fixture_id
        fixture_id += 1
        request = base / "fixture-request.tmp"
        request.write_text(json.dumps({"id": fixture_id, "action": action}))
        request.replace(base / "fixture-request.json")
        result = base / f"fixture-{fixture_id}.json"
        deadline = time.monotonic() + 30
        while not result.exists():
            if (base / "fixture-error.json").exists():
                raise AssertionError((base / "fixture-error.json").read_text())
            assert time.monotonic() < deadline, "Snapshot timed out"
            await asyncio.sleep(0.05)
        return json.loads(result.read_text())

    async with Client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "krita6_mcp.cli", "serve"],
            env=dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state")),
        )
    ) as client:

        async def call(tool_name, mutation=False, error=None, **kwargs):
            nonlocal serial
            args = {"instance_id": instance_id, **kwargs}
            if mutation:
                serial += 1
                args["operation_id"] = f"vector-{serial}"
            (base / "progress.json").write_text(json.dumps({"tool": tool_name, "args": args}))
            response = await client.call_tool("krita_" + tool_name, args)
            data = response.structured_content
            deadline = time.monotonic() + 30
            while data.get("state") in ("queued", "running", "cancel_requested"):
                assert time.monotonic() < deadline, data
                await asyncio.sleep(0.05)
                response = await client.call_tool(
                    "krita_get_operation",
                    {"instance_id": instance_id, "operation_id": data["operation_id"]},
                )
                data = response.structured_content
            if error:
                assert response.is_error and data["error"]["code"] == error, data
            else:
                assert not response.is_error, data
            if mutation:
                repeated = await client.call_tool("krita_" + tool_name, args)
                assert repeated.structured_content == data, (data, repeated.structured_content)
            return data.get("result", data)

        status = await call("status")
        for dpi in (72, 144):
            doc = await call(
                "create_document",
                mutation=True,
                name=f"Vectors {dpi}",
                width=128,
                height=96,
            )
            target = {"document_id": doc["document_id"]}
            await snapshot(f"vector_resolution_{dpi}")
            lower = await call("create_vector_layer", mutation=True, **target, name="Lower")
            layer = {**target, "node_id": lower["node_id"]}
            inventory = await call("inspect_document", **target)
            background = next(n for n in inventory["layers"] if n["type"] == "paintlayer")
            await call("delete_layer", mutation=True, **target, node_id=background["node_id"])
            await call(
                "add_vector_shape",
                mutation=True,
                **layer,
                geometry={"kind": "rectangle", "x": 8, "y": 8, "width": 16, "height": 16},
                fill="#FF0000",
                name="Red rectangle",
            )
            observed = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert pixel(observed, 12, 12) == [255, 0, 0, 255], observed
            assert pixel(observed, 28, 12)[3] == 0
            inspected = await call("inspect_vector_layer", **layer)
            assert len(inspected["shapes"]) == 1, inspected
            shape = inspected["shapes"][0]
            assert shape["type"] == "KoPathShape", shape
            assert all(abs(a - b) < 0.1 for a, b in zip(shape["bounds_px"], [8, 8, 16, 16])), shape
            address = {"snapshot_id": inspected["snapshot_id"], "shape_index": 0}
            await call(
                "edit_vector_shape",
                mutation=True,
                **layer,
                **address,
                translate_x=24,
                name="Moved red",
            )
            await call(
                "delete_vector_shape",
                mutation=True,
                error="STALE_VECTOR_SNAPSHOT",
                **layer,
                **address,
            )
            observed = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert pixel(observed, 12, 12)[3] == 0 and pixel(observed, 36, 12) == [255, 0, 0, 255]
            checks[f"create_transform_pixels_dpi_{dpi}_and_duplicate_retries"] = True
            # Direct geometry edits must repaint both old and new extents.
            inspected = await call("inspect_vector_layer", **layer)
            await call(
                "edit_vector_shape",
                mutation=True,
                **layer,
                snapshot_id=inspected["snapshot_id"],
                shape_index=0,
                scale_x=0.5,
                scale_y=0.5,
                rotation_degrees=90,
                translate_x=64,
                translate_y=24,
            )
            observed = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert pixel(observed, 36, 12)[3] == 0 and pixel(observed, 56, 44) == [255, 0, 0, 255]
            inspected = await call("inspect_vector_layer", **layer)
            await call(
                "edit_vector_shape",
                mutation=True,
                **layer,
                snapshot_id=inspected["snapshot_id"],
                shape_index=0,
                scale_x=2,
                scale_y=2,
                rotation_degrees=-90,
                translate_x=-48,
                translate_y=128,
            )
            inspected = await call("inspect_vector_layer", **layer)
            await call(
                "edit_vector_shape",
                mutation=True,
                **layer,
                snapshot_id=inspected["snapshot_id"],
                shape_index=0,
                visible=False,
            )
            observed = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert pixel(observed, 36, 12)[3] == 0
            inspected = await call("inspect_vector_layer", **layer)
            await call(
                "edit_vector_shape",
                mutation=True,
                **layer,
                snapshot_id=inspected["snapshot_id"],
                shape_index=0,
                visible=True,
                z_index=3,
            )
            observed = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert pixel(observed, 36, 12) == [255, 0, 0, 255]
            checks[f"scale_rotate_visibility_and_order_dpi_{dpi}"] = True
            top = await call("create_vector_layer", mutation=True, **target, name="Upper")
            upper = {**target, "node_id": top["node_id"]}
            await call(
                "add_vector_shape",
                mutation=True,
                **upper,
                geometry={"kind": "ellipse", "x": 32, "y": 8, "width": 16, "height": 16},
                fill="#0000FF",
                name="Blue ellipse",
            )
            for action, restore, code in (
                ("vector_lock", "vector_unlock", "TARGET_LOCKED"),
                ("vector_protect", "vector_unprotect", "INVALID_TARGET_TYPE"),
                ("vector_antialias_off", "vector_antialias_on", "UNSUPPORTED_COMPOSITING"),
            ):
                await snapshot(action)
                await call("merge_vector_layer_down", mutation=True, error=code, **upper)
                await snapshot(restore)
            await call("set_layer_properties", mutation=True, **upper, opacity=0.5)
            await call(
                "merge_vector_layer_down", mutation=True, error="UNSUPPORTED_COMPOSITING", **upper
            )
            await call(
                "set_layer_properties", mutation=True, **upper, opacity=1, blending_mode="multiply"
            )
            await call(
                "merge_vector_layer_down", mutation=True, error="UNSUPPORTED_COMPOSITING", **upper
            )
            await call("set_layer_properties", mutation=True, **upper, blending_mode="normal")
            checks[f"merge_compositing_rejections_dpi_{dpi}"] = True
            before = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            merged = await call("merge_vector_layer_down", mutation=True, **upper)
            after = next(
                d for d in (await snapshot())["documents"] if d["name"] == f"Vectors {dpi}"
            )
            assert merged["node_id"] == layer["node_id"]
            assert before["pixels"] == after["pixels"], "Merge changed rendered pixels"
            assert len(after["layers"]) == 1 and after["layers"][0]["type"] == "vectorlayer", after
            assert len(after["layers"][0]["vector_shapes"]) == 2
            checks[f"merge_retains_vectors_stacking_and_pixels_dpi_{dpi}"] = True
            inspected = await call("inspect_vector_layer", **layer)
            await call(
                "delete_vector_shape",
                mutation=True,
                **layer,
                snapshot_id=inspected["snapshot_id"],
                shape_index=1,
            )
            inspected = await call("inspect_vector_layer", **layer)
            assert len(inspected["shapes"]) == 1
            await call(
                "add_vector_shape",
                mutation=True,
                **layer,
                geometry={"kind": "polygon", "points": [[64, 8], [80, 8], [72, 24]]},
                fill="#00FF00",
            )
            await call(
                "add_vector_shape",
                mutation=True,
                **layer,
                geometry={
                    "kind": "bezier",
                    "start": [8, 40],
                    "segments": [[[16, 32], [24, 48], [32, 40]]],
                },
                fill="none",
                stroke="#000000",
                stroke_width=2,
            )
            await call(
                "save_document", mutation=True, **target, root="scratch", path=f"vectors-{dpi}.kra"
            )
            reopened = await call(
                "open_document", mutation=True, root="scratch", path=f"vectors-{dpi}.kra"
            )
            inspected_doc = await call("inspect_document", document_id=reopened["document_id"])
            node = next(n for n in inspected_doc["layers"] if n["type"] == "vectorlayer")
            restored = await call(
                "inspect_vector_layer", document_id=reopened["document_id"], node_id=node["node_id"]
            )
            assert len(restored["shapes"]) == 3
            checks[f"delete_polygon_bezier_kra_persistence_dpi_{dpi}"] = True
        return {
            "passed": True,
            "host": {
                k: status[k]
                for k in (
                    "krita_version",
                    "qt_version",
                    "pyqt_version",
                    "python_version",
                    "platform",
                )
            },
            "checks": checks,
        }


if __name__ == "__main__":
    probe_editing.scenario = scenario
    raise SystemExit(probe_editing.main(startup_timeout=180))
