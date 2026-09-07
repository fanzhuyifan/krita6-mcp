"""Exercise general editing via production MCP and an independent isolated native fixture."""

import asyncio
import base64
import json
import os
import sys
import time

from mcp import Client, StdioServerParameters
import probe_editing
from probe_editing import pixel


async def scenario(base, instance_id):
    checks = {}
    counter = 0
    fixture_id = 0

    async def snapshot():
        nonlocal fixture_id
        fixture_id += 1
        request = base / "fixture-request.tmp"
        request.write_text(json.dumps({"id": fixture_id, "action": "snapshot"}))
        request.replace(base / "fixture-request.json")
        result = base / f"fixture-{fixture_id}.json"
        deadline = time.monotonic() + 30
        while not result.exists():
            if (base / "fixture-error.json").exists():
                raise AssertionError((base / "fixture-error.json").read_text())
            if time.monotonic() > deadline:
                raise TimeoutError("Native snapshot timed out")
            await asyncio.sleep(0.05)
        return json.loads(result.read_text())

    async with Client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "krita6_mcp.cli", "serve"],
            env=dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state")),
        )
    ) as client:

        async def call(tool_name, *, mutation=False, error=None, busy_retries=0, **params):
            nonlocal counter
            args = {"instance_id": instance_id, **params}
            if mutation:
                counter += 1
                args["operation_id"] = f"general-{counter}"
            (base / "progress.json").write_text(json.dumps({"tool": tool_name, "arguments": args}))
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
            if (
                not error
                and response.is_error
                and (data.get("error") or {}).get("code") == "DOCUMENT_BUSY"
                and data.get("effect", data.get("error", {}).get("effect")) == "none"
                and busy_retries < 20
            ):
                if mutation:
                    repeated = await client.call_tool("krita_" + tool_name, args)
                    assert repeated.structured_content == data
                await asyncio.sleep(0.1)
                # A settled rejection with effect=none permits a fresh operation ID.
                return await call(
                    tool_name, mutation=mutation, busy_retries=busy_retries + 1, **params
                )
            if error:
                assert response.is_error and data["error"]["code"] == error, data
            else:
                assert not response.is_error, data
            if mutation:
                repeated = await client.call_tool("krita_" + tool_name, args)
                assert repeated.structured_content == data
            return data.get("result", data), response

        host, _ = await call("status")
        listing = await client.list_tools()
        catalog = listing.tools if hasattr(listing, "tools") else listing
        assert len(catalog) == 56
        doc, _ = await call(
            "create_document", mutation=True, name="General editing", width=64, height=64
        )
        target = {"document_id": doc["document_id"]}
        paint, _ = await call("create_paint_layer", mutation=True, **target, name="Color")
        layer = {**target, "node_id": paint["node_id"]}

        async def native():
            return next(
                d for d in (await snapshot())["documents"] if d["name"] == "General editing"
            )

        for initial in (await native())["layers"]:
            if initial["node_id"] != layer["node_id"]:
                await call(
                    "set_layer_properties",
                    mutation=True,
                    **target,
                    node_id=initial["node_id"],
                    visible=False,
                )

        presets, _ = await call("list_brush_presets", query="Basic-5 Size")
        preset = next(p for p in presets["presets"] if p["supported_for_painting"])
        brush = {"preset_id": preset["preset_id"], "size_px": 3, "opacity": 1, "color": "#FFFFFF"}
        await call("fill_layer", mutation=True, **layer, kind="solid", color="#FF0000")
        assert pixel(await native(), 3, 3) == [255, 0, 0, 255]
        sampled, _ = await call("sample_color", **target, x=3, y=3)
        assert sampled["rgba"] == [255, 0, 0, 255]
        preview, response = await call("get_layer_preview", **layer, max_edge=32)
        image = next(c for c in response.content if c.type == "image")
        (base / "preview-general.png").write_bytes(base64.b64decode(image.data))
        assert preview["preview_width"] == preview["preview_height"] == 32
        images = (await snapshot())["images"]
        assert list(base64.b64decode(images["preview-general.png"]["rgba"])[:4]) == [255, 0, 0, 255]
        checks["solid_fill_sampling_and_layer_png"] = True
        before = await native()
        settings, _ = await call("inspect_brush", **target)
        await call(
            "paint_shape",
            mutation=True,
            **layer,
            **brush,
            shape="rectangle",
            x=16,
            y=16,
            width=24,
            height=24,
            fill=True,
        )
        painted = await native()
        assert pixel(painted, 24, 24) == [255, 255, 255, 255]
        await call("edit_history", mutation=True, **target, direction="undo")
        assert (await native())["pixels"] == before["pixels"]
        await call("edit_history", mutation=True, **target, direction="redo")
        assert (await native())["pixels"] == painted["pixels"]
        await call(
            "paint_shape",
            mutation=True,
            **layer,
            **{**brush, "color": "#00FF00"},
            shape="ellipse",
            x=20,
            y=20,
            width=12,
            height=12,
            fill=True,
        )
        assert pixel(await native(), 26, 26) == [0, 255, 0, 255]
        observed, _ = await call("inspect_brush", **target)
        assert observed == settings
        checks["native_shapes_history_and_restoration"] = True
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=4,
            y=3,
            width=16,
            height=3,
        )
        await call("fill_layer", mutation=True, **layer, kind="erase")
        assert pixel(await native(), 8, 4)[3] == 0
        assert pixel(await native(), 8, 8)[3] == 255
        await call("clear_selection", mutation=True, **target)
        checks["selection_aware_raster_erasing"] = True
        await call(
            "fill_layer",
            mutation=True,
            **layer,
            kind="linear_gradient",
            color="#000000",
            end_color="#FFFFFF",
            start=[0, 0],
            end=[63, 0],
        )
        gradient = await native()
        assert pixel(gradient, 2, 50)[0] < pixel(gradient, 60, 50)[0]
        await call("fill_layer", mutation=True, **layer, kind="solid", color="#FF0000")
        await call(
            "paint_shape",
            mutation=True,
            **layer,
            **brush,
            shape="rectangle",
            x=16,
            y=16,
            width=24,
            height=24,
            fill=True,
        )
        await call(
            "fill_layer", mutation=True, **layer, kind="flood", color="#0000FF", point=[0, 0]
        )
        flooded = await native()
        assert pixel(flooded, 2, 2) == [0, 0, 255, 255]
        assert pixel(flooded, 24, 24) == [255, 255, 255, 255]
        checks["gradient_and_connected_flood_fill"] = True
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=0,
            y=0,
            width=16,
            height=16,
        )
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=16,
            y=0,
            width=16,
            height=16,
            mode="add",
        )
        mask = base64.b64decode((await native())["selection"]["pixels"])
        assert mask[8 * 64 + 24] == 255 and mask[8 * 64 + 40] == 0
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=0,
            y=0,
            width=8,
            height=16,
            mode="subtract",
        )
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=0,
            y=0,
            width=24,
            height=24,
            mode="intersect",
        )
        mask = base64.b64decode((await native())["selection"]["pixels"])
        assert mask[8 * 64 + 4] == 0 and mask[8 * 64 + 12] == 255 and mask[8 * 64 + 28] == 0
        await call("modify_selection", mutation=True, **target, action="grow", radius=2)
        grown = base64.b64decode((await native())["selection"]["pixels"])
        assert grown[8 * 64 + 7] > 0
        await call("modify_selection", mutation=True, **target, action="shrink", radius=2)
        await call("modify_selection", mutation=True, **target, action="feather", radius=2)
        feathered = base64.b64decode((await native())["selection"]["pixels"])
        assert any(0 < v < 255 for v in feathered)
        await call("modify_selection", mutation=True, **target, action="invert")
        inverted = base64.b64decode((await native())["selection"]["pixels"])
        assert all(a + b == 255 for a, b in zip(feathered, inverted))
        checks["selection_combinations_and_refinement"] = True
        await call(
            "set_selection",
            mutation=True,
            **target,
            shape="rectangle",
            x=0,
            y=0,
            width=16,
            height=16,
        )
        await call("fill_layer", mutation=True, **layer, kind="solid", color="#00FF00")
        assert pixel(await native(), 4, 4) == [0, 255, 0, 255]
        assert pixel(await native(), 50, 50) == [0, 0, 255, 255]
        mask_layer, _ = await call(
            "create_transparency_mask", mutation=True, **layer, name="Mask", source="selection"
        )
        assert pixel(await native(), 50, 50)[3] == 0
        mask_target = {**target, "node_id": mask_layer["node_id"]}
        await call("set_transparency_mask", mutation=True, **mask_target, source="opaque")
        assert pixel(await native(), 50, 50)[3] == 255
        await call("set_transparency_mask", mutation=True, **mask_target, source="transparent")
        assert pixel(await native(), 4, 4)[3] == 0
        await call("delete_layer", mutation=True, **mask_target)
        assert pixel(await native(), 50, 50)[3] == 255
        await call("clear_selection", mutation=True, **target)
        checks["selected_fill_transparency_masks_and_deletion"] = True
        await call("set_layer_properties", mutation=True, **layer, alpha_locked=True)
        await call(
            "fill_layer",
            mutation=True,
            **layer,
            kind="solid",
            color="#FFFFFF",
            error="TARGET_LOCKED",
        )
        await call(
            "set_layer_properties", mutation=True, **layer, alpha_locked=False, inherit_alpha=True
        )
        await call("set_layer_properties", mutation=True, **layer, inherit_alpha=False)
        group, _ = await call("create_group_layer", mutation=True, **target, name="Group")
        await call("move_layer", mutation=True, **layer, parent_node_id=group["node_id"])
        group_target = {**target, "node_id": group["node_id"]}
        await call(
            "move_layer",
            mutation=True,
            **group_target,
            parent_node_id=group["node_id"],
            error="INVALID_TARGET",
        )
        await call("set_layer_properties", mutation=True, **group_target, visible=False)
        assert pixel(await native(), 4, 4)[3] == 0
        await call("set_layer_properties", mutation=True, **group_target, visible=True)
        await call("move_layer", mutation=True, **layer)
        await call("delete_layer", mutation=True, **group_target)
        await call("fill_layer", mutation=True, **layer, kind="solid", color="#0000FF")
        top, _ = await call("create_paint_layer", mutation=True, **target, name="Top")
        top_target = {**target, "node_id": top["node_id"]}
        await call("fill_layer", mutation=True, **top_target, kind="solid", color="#FF0000")
        await call("set_layer_properties", mutation=True, **top_target, blending_mode="multiply")
        assert pixel(await native(), 4, 4) == [0, 0, 0, 255]
        merged, _ = await call("merge_layer_down", mutation=True, **top_target)
        assert merged["node_id"] and pixel(await native(), 4, 4) == [0, 0, 0, 255]
        checks["groups_compositing_locks_and_merge"] = True
        layer = {**target, "node_id": merged["node_id"]}
        await call(
            "fill_layer",
            mutation=True,
            **layer,
            kind="linear_gradient",
            color="#000000",
            end_color="#FFFFFF",
            start=[0, 0],
            end=[63, 0],
        )
        original = await native()
        await call("transform_canvas", mutation=True, **target, action="flip", axis="horizontal")
        flipped = await native()
        assert pixel(flipped, 3, 20) == pixel(original, 60, 20)
        await call("transform_canvas", mutation=True, **target, action="flip", axis="vertical")
        await call("transform_canvas", mutation=True, **target, action="rotate", degrees=90)
        rotated = await native()
        assert pixel(rotated, 20, 3) != pixel(rotated, 20, 60)
        await call(
            "transform_canvas",
            mutation=True,
            **target,
            action="crop",
            x=8,
            y=8,
            width=48,
            height=48,
        )
        cropped = await native()
        assert cropped["width"] == cropped["height"] == 48
        await call(
            "transform_canvas",
            mutation=True,
            **target,
            action="resize",
            x=-8,
            y=-8,
            width=64,
            height=64,
        )
        resized = await native()
        assert resized["width"] == resized["height"] == 64 and pixel(resized, 1, 1)[3] == 0
        await call(
            "transform_canvas",
            mutation=True,
            **target,
            action="scale",
            width=32,
            height=32,
            filter="NearestNeighbor",
        )
        scaled = await native()
        assert scaled["width"] == scaled["height"] == 32
        checks["canvas_flip_rotate_crop_resize_scale"] = True
        checks["all_mutations_duplicate_ids"] = True
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "tool_count": len(catalog),
            "host": {
                k: host[k]
                for k in (
                    "krita_version",
                    "qt_version",
                    "pyqt_version",
                    "python_version",
                    "platform",
                )
            },
            "limits": "64x64 RGBA/U8/standard sRGB, Basic-5 Size pixel brush. Raster fills and direct edits have no guaranteed undo transaction.",
        }


if __name__ == "__main__":
    probe_editing.scenario = scenario
    raise SystemExit(probe_editing.main())
