"""Validate reference editing via real MCP in a separate, disposable Krita instance."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback

from mcp import Client, StdioServerParameters

from probe_krita import isolated_environment, launch_krita


def pixel(snapshot, x, y):
    """The scratch documents use the explicitly tested RGBA/U8 BGRA byte layout."""
    data = base64.b64decode(snapshot["pixels"])
    offset = (y * snapshot["width"] + x) * 4
    blue, green, red, alpha = data[offset : offset + 4]
    return [red, green, blue, alpha]


def visible_pixels(snapshot):
    # Krita may retain different hidden RGB in fully transparent projection
    # pixels after redo. Layer byte equality is checked independently below.
    data = bytearray(base64.b64decode(snapshot["pixels"]))
    for offset in range(0, len(data), 4):
        if data[offset + 3] == 0:
            data[offset : offset + 3] = b"\0\0\0"
    return data


async def scenario(base, instance_id):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "krita6_mcp.cli", "serve"],
        env=dict(os.environ, KRITA6_MCP_STATE_DIR=str(base / "state")),
    )
    checks = {}
    fixture_id = 0

    async def fixture(action="snapshot"):
        nonlocal fixture_id
        fixture_id += 1
        temporary = base / "fixture-request.tmp"
        temporary.write_text(json.dumps({"id": fixture_id, "action": action}))
        temporary.replace(base / "fixture-request.json")
        path = base / f"fixture-{fixture_id}.json"
        deadline = time.monotonic() + 30
        while not path.exists():
            if (base / "fixture-error.json").exists():
                raise AssertionError((base / "fixture-error.json").read_text())
            if time.monotonic() > deadline:
                busy = base / "fixture-busy.json"
                detail = busy.read_text() if busy.exists() else "no busy-document observation"
                raise TimeoutError(f"Independent fixture observation did not complete: {detail}")
            await asyncio.sleep(0.05)
        return json.loads(path.read_text())

    def document(snapshot, name):
        return next(doc for doc in snapshot["documents"] if doc["name"] == name)

    async with Client(parameters) as client:

        async def call(tool_name, *, failure=False, failure_effect="none", **arguments):
            (base / "progress.json").write_text(
                json.dumps({"tool": tool_name, "arguments": arguments})
            )
            response = await client.call_tool(tool_name, {"instance_id": instance_id, **arguments})
            data = response.structured_content
            deadline = time.monotonic() + 30
            while (
                not failure
                and tool_name in {"krita_inspect_document", "krita_get_region_preview"}
                and response.is_error
                and (data.get("error") or {}).get("code") == "DOCUMENT_BUSY"
            ):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{tool_name} document stayed busy")
                await asyncio.sleep(0.05)
                response = await client.call_tool(
                    tool_name, {"instance_id": instance_id, **arguments}
                )
                data = response.structured_content
            while data.get("state") in {"queued", "running", "cancel_requested"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{tool_name} still pending")
                await asyncio.sleep(0.05)
                response = await client.call_tool(
                    "krita_get_operation",
                    {
                        "instance_id": instance_id,
                        "operation_id": data["operation_id"],
                    },
                )
                data = response.structured_content
            if failure:
                assert response.is_error, (tool_name, data)
                assert data.get("effect", data.get("error", {}).get("effect")) == failure_effect, (
                    data
                )
            elif response.is_error:
                raise AssertionError(f"{tool_name}: {data}")
            return response, data.get("result", data) if not failure else data

        _, host = await call("krita_status")
        listing = await client.list_tools()
        catalog = listing.tools if hasattr(listing, "tools") else listing
        assert len(catalog) == 35
        checks["tool_count"] = len(catalog)
        _, source = await call(
            "krita_create_document",
            operation_id="edit-source",
            width=128,
            height=96,
            name="Editing source",
        )
        source_id = source["document_id"]
        _, destination = await call(
            "krita_create_document",
            operation_id="edit-destination",
            width=128,
            height=96,
            name="Editing destination",
        )
        destination_id = destination["document_id"]
        for label, handle in (("source", source_id), ("destination", destination_id)):
            _, initial = await call("krita_inspect_document", document_id=handle)
            for index, initial_layer in enumerate(initial["layers"]):
                await call(
                    "krita_set_layer_properties",
                    operation_id=f"edit-hide-initial-{label}-{index}",
                    document_id=handle,
                    node_id=initial_layer["node_id"],
                    visible=False,
                )
        _, layer = await call(
            "krita_create_paint_layer",
            operation_id="edit-brush-layer",
            document_id=source_id,
            name="Bezier",
        )
        _, presets = await call("krita_list_brush_presets", query="Basic-5 Size")
        brush = dict(
            document_id=source_id,
            node_id=layer["node_id"],
            preset_id=presets["presets"][0]["preset_id"],
            size_px=6.0,
            opacity=1.0,
            color="#FF0000",
            start=[10, 60],
            segments=[[[40, 20], [70, 90], [110, 60]]],
        )
        _, denied = await call(
            "krita_paint_bezier_path", failure=True, operation_id="edit-inactive", **brush
        )
        assert denied["error"]["code"] == "TARGET_NOT_ACTIVE", denied
        await call("krita_activate_document", operation_id="edit-activate", document_id=source_id)
        _, inspection = await call("krita_inspect_document", document_id=source_id)
        assert inspection["active_view"]
        checks["activation_selects_requested_document"] = True

        await call(
            "krita_set_selection",
            operation_id="edit-rectangle",
            document_id=source_id,
            shape="rectangle",
            x=10,
            y=10,
            width=20,
            height=15,
        )
        rectangle = document(await fixture(), "Editing source")
        mask = base64.b64decode(rectangle["selection"]["pixels"])
        assert rectangle["selection"]["bounds"] == [10, 10, 20, 15]
        assert mask[12 * 128 + 12] == 255 and mask[9 * 128 + 12] == 0
        await call("krita_paint_bezier_path", failure=True, operation_id="edit-selected", **brush)
        await call(
            "krita_set_selection",
            operation_id="edit-polygon",
            document_id=source_id,
            shape="polygon",
            points=[[10, 10], [30, 10], [10, 30]],
        )
        polygon = document(await fixture(), "Editing source")
        mask = base64.b64decode(polygon["selection"]["pixels"])
        assert mask[12 * 128 + 12] == 255 and mask[25 * 128 + 25] == 0
        await call(
            "krita_set_selection",
            operation_id="edit-bowtie",
            document_id=source_id,
            shape="polygon",
            points=[[10, 10], [30, 30], [10, 30], [30, 10]],
        )
        bowtie = document(await fixture(), "Editing source")
        mask = base64.b64decode(bowtie["selection"]["pixels"])
        assert mask[12 * 128 + 20] == 255 and mask[28 * 128 + 20] == 255
        assert mask[20 * 128 + 11] == 0 and mask[8 * 128 + 20] == 0
        _, rejected = await call(
            "krita_set_selection",
            failure=True,
            operation_id="edit-empty-polygon",
            document_id=source_id,
            shape="polygon",
            points=[[10, 10], [10, 10], [10, 10]],
        )
        assert rejected["error"]["code"] == "INVALID_GEOMETRY"
        assert document(await fixture(), "Editing source")["selection"] == bowtie["selection"]
        checks["odd_even_bowtie_lobes_and_empty_polygon_rejection"] = True
        await call("krita_clear_selection", operation_id="edit-clear", document_id=source_id)
        before = document(await fixture(), "Editing source")
        assert before["selection"] is None or before["selection"]["bounds"][2:] == [0, 0]
        checks["rectangle_polygon_and_clear_selection"] = True
        _, painted = await call("krita_paint_bezier_path", operation_id="edit-bezier", **brush)
        assert painted["native_strokes"] == 1
        after = document(await fixture(), "Editing source")
        assert after["pixels"] != before["pixels"]
        _, repeated = await call("krita_paint_bezier_path", operation_id="edit-bezier", **brush)
        assert repeated == painted
        assert document(await fixture(), "Editing source")["pixels"] == after["pixels"]
        undone = document(await fixture("undo"), "Editing source")
        assert undone["pixels"] == before["pixels"]
        redone = document(await fixture("redo"), "Editing source")
        assert visible_pixels(redone) == visible_pixels(after)
        assert (
            next(n for n in redone["layers"] if n["node_id"] == layer["node_id"])["pixels"]
            == next(n for n in after["layers"] if n["node_id"] == layer["node_id"])["pixels"]
        )
        checks["native_bezier_pixels_duplicate_and_one_undo_redo"] = True
        await call(
            "krita_set_layer_properties",
            operation_id="edit-hide-bezier",
            document_id=source_id,
            node_id=layer["node_id"],
            visible=False,
        )

        import_args = dict(
            document_id=source_id,
            root="scratch",
            path="colors.png",
            name="Color reference",
            x=20,
            y=20,
        )
        _, imported = await call(
            "krita_import_image_layer", operation_id="edit-import", **import_args
        )
        _, repeated = await call(
            "krita_import_image_layer", operation_id="edit-import", **import_args
        )
        assert repeated == imported
        source_node = imported["node_id"]
        original = document(await fixture(), "Editing source")
        assert pixel(original, 22, 22) == [255, 0, 0, 255]
        assert pixel(original, 38, 22) == [0, 255, 0, 255]
        assert pixel(original, 22, 32) == [0, 0, 255, 255]
        assert pixel(original, 38, 32)[3] == 0
        assert len([n for n in original["layers"] if n["name"] == "Color reference"]) == 1
        checks["png_import_offset_channels_alpha_and_retry"] = True
        await call(
            "krita_set_layer_properties",
            operation_id="edit-properties",
            document_id=source_id,
            node_id=source_node,
            name="Renamed reference",
            visible=False,
            opacity=0.5,
        )
        properties = document(await fixture(), "Editing source")
        changed = next(n for n in properties["layers"] if n["node_id"] == source_node)
        assert changed["name"] == "Renamed reference" and not changed["visible"]
        assert changed["opacity"] in {127, 128} and pixel(properties, 22, 22)[3] == 0
        await call(
            "krita_set_layer_properties",
            operation_id="edit-show-half",
            document_id=source_id,
            node_id=source_node,
            visible=True,
        )
        assert pixel(document(await fixture(), "Editing source"), 22, 22)[3] in {127, 128}
        await call(
            "krita_set_layer_properties",
            operation_id="edit-restore-opacity",
            document_id=source_id,
            node_id=source_node,
            opacity=1.0,
        )
        checks["layer_name_visibility_opacity_actual_projection"] = True

        await call(
            "krita_save_document",
            operation_id="edit-save",
            document_id=source_id,
            root="scratch",
            path="editing.kra",
        )
        clean = document(await fixture(), "Editing source")
        assert not clean["modified"]
        response, preview = await call(
            "krita_get_region_preview",
            document_id=source_id,
            x=20,
            y=20,
            width=24,
            height=16,
            max_edge=64,
        )
        images = [item for item in response.content if item.type == "image"]
        assert len(images) == 1
        png = base64.b64decode(images[0].data)
        (base / "preview-region.png").write_bytes(png)
        observed = await fixture()
        assert document(observed, "Editing source") == clean
        region = observed["images"]["preview-region.png"]
        assert (region["width"], region["height"]) == (24, 16)
        rgba = base64.b64decode(region["rgba"])
        assert list(rgba[(2 * 24 + 2) * 4 : (2 * 24 + 2) * 4 + 4]) == [255, 0, 0, 255]
        assert list(rgba[(2 * 24 + 18) * 4 : (2 * 24 + 18) * 4 + 4]) == [0, 255, 0, 255]
        assert list(rgba[(12 * 24 + 2) * 4 : (12 * 24 + 2) * 4 + 4]) == [0, 0, 255, 255]
        assert rgba[(12 * 24 + 18) * 4 + 3] == 0
        assert preview["source_bounds"] == {"x": 20, "y": 20, "width": 24, "height": 16}
        assert preview["scale"] == {"x": 1.0, "y": 1.0}
        response, scaled_preview = await call(
            "krita_get_region_preview",
            document_id=source_id,
            x=0,
            y=0,
            width=128,
            height=96,
            max_edge=64,
        )
        scaled_png = base64.b64decode(next(c for c in response.content if c.type == "image").data)
        (base / "preview-scaled.png").write_bytes(scaled_png)
        observed = await fixture()
        assert document(observed, "Editing source") == clean
        scaled = observed["images"]["preview-scaled.png"]
        assert (scaled["width"], scaled["height"]) == (64, 48)
        assert scaled_preview["scale"] == {"x": 0.5, "y": 0.5}
        assert scaled_preview["source_offset"] == {"x": 0, "y": 0}
        checks["region_preview_coordinates_channels_alpha_and_no_side_effects"] = True

        copy_args = dict(
            document_id=source_id,
            node_id=source_node,
            destination_document_id=destination_id,
            name="Overlay",
        )
        _, copied = await call("krita_copy_layer", operation_id="edit-copy", **copy_args)
        _, repeated = await call("krita_copy_layer", operation_id="edit-copy", **copy_args)
        assert repeated == copied
        copied_node = copied["node_id"]
        assert copied_node != source_node
        observed = await fixture()
        assert document(observed, "Editing source") == clean
        dest = document(observed, "Editing destination")
        assert pixel(dest, 22, 22) == [255, 0, 0, 255]
        assert len([n for n in dest["layers"] if n["name"] == "Overlay"]) == 1
        checks["cross_document_copy_preserves_source_and_deduplicates"] = True
        _, cover = await call(
            "krita_import_image_layer",
            operation_id="edit-jpeg",
            document_id=destination_id,
            root="scratch",
            path="orange.jpg",
            name="Cover",
            x=20,
            y=20,
        )
        await call(
            "krita_move_layer",
            operation_id="edit-cover-above",
            document_id=destination_id,
            node_id=cover["node_id"],
            above_node_id=copied_node,
        )
        orange = pixel(document(await fixture(), "Editing destination"), 22, 22)
        assert all(abs(a - b) <= 3 for a, b in zip(orange, [240, 120, 20, 255]))
        await call(
            "krita_move_layer",
            operation_id="edit-overlay-above",
            document_id=destination_id,
            node_id=copied_node,
            above_node_id=cover["node_id"],
        )
        reordered = document(await fixture(), "Editing destination")
        ordered_ids = [n["node_id"] for n in reordered["layers"]]
        assert len(ordered_ids) == len(set(ordered_ids)) == 3
        assert ordered_ids.index(copied_node) == ordered_ids.index(cover["node_id"]) + 1
        assert pixel(reordered, 22, 22) == [255, 0, 0, 255]
        await call(
            "krita_set_layer_properties",
            operation_id="edit-cover-hide",
            document_id=destination_id,
            node_id=cover["node_id"],
            visible=False,
        )
        checks["jpeg_import_and_layer_reordering_projection"] = True
        transform = dict(
            document_id=destination_id,
            node_id=copied_node,
            pivot=[20, 20],
            scale_x=2.0,
            scale_y=2.0,
            rotation_degrees=90.0,
            translate_x=60.0,
            translate_y=0.0,
        )
        _, transformed = await call(
            "krita_transform_layer", operation_id="edit-transform", **transform
        )
        _, repeated = await call(
            "krita_transform_layer", operation_id="edit-transform", **transform
        )
        assert repeated == transformed
        dest = document(await fixture(), "Editing destination")
        assert pixel(dest, 72, 32) == [255, 0, 0, 255], pixel(dest, 72, 32)
        assert pixel(dest, 72, 56) == [0, 255, 0, 255], pixel(dest, 72, 56)
        assert pixel(dest, 56, 32) == [0, 0, 255, 255], pixel(dest, 56, 32)
        assert pixel(dest, 56, 56)[3] == 0 and pixel(dest, 22, 22)[3] == 0
        checks["affine_scale_rotation_pivot_translation_pixels_and_retry"] = True

        _, opened = await call(
            "krita_open_document", operation_id="edit-open-png", root="scratch", path="colors.png"
        )
        _, repeated = await call(
            "krita_open_document", operation_id="edit-open-png", root="scratch", path="colors.png"
        )
        assert repeated == opened
        _, opened_png = await call("krita_inspect_document", document_id=opened["document_id"])
        assert (opened_png["width"], opened_png["height"]) == (24, 16)
        _, opened_jpeg = await call(
            "krita_open_document", operation_id="edit-open-jpeg", root="scratch", path="orange.jpg"
        )
        _, jpeg_document = await call(
            "krita_inspect_document", document_id=opened_jpeg["document_id"]
        )
        assert (jpeg_document["width"], jpeg_document["height"]) == (24, 16)
        _, reopened = await call(
            "krita_open_document", operation_id="edit-open-kra", root="scratch", path="editing.kra"
        )
        _, opened_kra = await call("krita_inspect_document", document_id=reopened["document_id"])
        assert any(n["name"] == "Renamed reference" for n in opened_kra["layers"])
        _, inventory = await call("krita_list_documents")
        assert len(inventory["documents"]) == 5
        checks["png_jpeg_and_layered_kra_open_and_retry"] = True

        failure_args = [
            (
                "krita_open_document",
                dict(operation_id="edit-traversal", root="scratch", path="../outside.png"),
            ),
            (
                "krita_open_document",
                dict(operation_id="edit-wrong-format", root="scratch", path="text.txt"),
            ),
            (
                "krita_import_image_layer",
                dict(operation_id="edit-off-canvas", **{**import_args, "x": 127}),
            ),
            (
                "krita_get_region_preview",
                dict(document_id=source_id, x=127, y=0, width=24, height=16),
            ),
            (
                "krita_set_selection",
                dict(
                    operation_id="edit-invalid-selection",
                    document_id=source_id,
                    shape="rectangle",
                    x=127,
                    y=0,
                    width=24,
                    height=16,
                ),
            ),
            (
                "krita_set_layer_properties",
                dict(
                    operation_id="edit-properties",
                    document_id=source_id,
                    node_id=source_node,
                    name="Conflicting payload",
                ),
            ),
        ]
        before_failures = await fixture()
        for name, arguments in failure_args:
            await call(name, failure=True, **arguments)
        assert await fixture() == before_failures
        checks["invalid_paths_bounds_and_duplicate_conflicts_preserve_documents"] = True

        control = await fixture("ownership_control")
        assert control["recovery"]["control_owner_removed"] is True
        checks["unretained_create_wrapper_removes_viewless_native_document"] = True
        initial_count = len(control["documents"])
        for recovery_case, creation_tool, creation_arguments in (
            (
                "create",
                "krita_create_document",
                dict(
                    operation_id="recovery-create-document",
                    width=64,
                    height=48,
                    name="Creation recovery fixture",
                ),
            ),
            (
                "open",
                "krita_open_document",
                dict(operation_id="recovery-open-document", root="scratch", path="colors.png"),
            ),
        ):
            await fixture("fail_next_view")
            _, failed_creation = await call(
                creation_tool, failure=True, failure_effect="partial", **creation_arguments
            )
            assert failed_creation["state"] == "failed"
            assert set(failed_creation["result"]) == {"document_id"}
            recovery_id = failed_creation["result"]["document_id"]
            _, repeated_creation = await call(
                creation_tool, failure=True, failure_effect="partial", **creation_arguments
            )
            assert repeated_creation == failed_creation
            _, settled = await call(
                "krita_get_operation",
                failure=True,
                failure_effect="partial",
                operation_id=creation_arguments["operation_id"],
            )
            assert settled == failed_creation
            # Force release of temporary aliases/exception frames, then repeatedly
            # reconcile against fresh native wrappers. Only the host retains the
            # original owning create/open wrapper before a view is attached.
            recovery_snapshot = await fixture("collect")
            assert recovery_snapshot["recovery"]["document_id"] == recovery_id
            assert recovery_snapshot["recovery"]["owner_retained"]
            for _ in range(3):
                _, inventory = await call("krita_list_documents")
                assert len(inventory["documents"]) == initial_count + 1
                assert any(d["document_id"] == recovery_id for d in inventory["documents"])
                _, recovered = await call("krita_inspect_document", document_id=recovery_id)
                assert recovered["active_view"] is False
                await fixture("collect")

            if recovery_case == "create":
                await fixture("fail_layer_activation")
                layer_arguments = dict(
                    operation_id="recovery-create-layer",
                    document_id=recovery_id,
                    name="Attached recovery layer",
                )
                _, failed_layer = await call(
                    "krita_create_paint_layer",
                    failure=True,
                    failure_effect="partial",
                    **layer_arguments,
                )
                assert set(failed_layer["result"]) == {"document_id", "node_id"}
                assert failed_layer["result"]["document_id"] == recovery_id
                _, repeated_layer = await call(
                    "krita_create_paint_layer",
                    failure=True,
                    failure_effect="partial",
                    **layer_arguments,
                )
                assert repeated_layer == failed_layer
                _, recovered = await call("krita_inspect_document", document_id=recovery_id)
                attached = [
                    n for n in recovered["layers"] if n["name"] == "Attached recovery layer"
                ]
                assert len(attached) == 1
                assert attached[0]["node_id"] == failed_layer["result"]["node_id"]
                assert (await fixture("collect"))["recovery"]["layer_failures"] == 1
                checks["failed_layer_activation_retains_partial_node_handle_without_replay"] = True

            await fixture("attach_recovery_view")
            await call(
                "krita_activate_document",
                operation_id=f"recovery-activate-{recovery_case}",
                document_id=recovery_id,
            )
            _, recovered = await call("krita_inspect_document", document_id=recovery_id)
            assert recovered["active_view"] is True
            await fixture("close_recovery")
            _, inventory = await call("krita_list_documents")
            assert len(inventory["documents"]) == initial_count
            assert not any(d["document_id"] == recovery_id for d in inventory["documents"])
            assert (await fixture("collect"))["recovery"]["owner_retained"] is False
            _, closed = await call("krita_inspect_document", failure=True, document_id=recovery_id)
            assert closed["error"]["code"] == "TARGET_NOT_FOUND"
            # Retrying the failed identity after the user closes the recovered
            # document still returns the recorded outcome, never another document.
            _, repeated_closed = await call(
                creation_tool, failure=True, failure_effect="partial", **creation_arguments
            )
            assert repeated_closed == failed_creation
            _, inventory = await call("krita_list_documents")
            assert len(inventory["documents"]) == initial_count
            checks[f"failed_{recovery_case}_view_retains_owner_handle_retry_attach_and_close"] = (
                True
            )

        return {
            "passed": True,
            "host": {
                key: host[key]
                for key in (
                    "krita_version",
                    "qt_version",
                    "pyqt_version",
                    "python_version",
                    "platform",
                    "display_backend",
                    "plugin_version",
                    "bridge_protocol",
                )
            },
            "checks": checks,
            "region_preview": {
                key: value
                for key, value in preview.items()
                if key not in {"artifact_id", "document_id"}
            },
            "region_preview_sha256": hashlib.sha256(png).hexdigest(),
            "limits": "Small RGBA/U8/standard-sRGB scratch canvases, Basic-5 Size pixel brush; no general undo guarantee for direct editing.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--krita", default="krita")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = (args.output or Path(tempfile.mkdtemp(prefix="krita6-editing-"))).resolve()
    if (base / "report.json").exists():
        parser.error("Output already contains a report; choose a new directory.")
    env = isolated_environment(base)
    (base / "artwork").mkdir(exist_ok=True)
    (base / "artwork/text.txt").write_text("Not an image")
    plugins = base / "data/krita/pykrita"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        root / "plugin/krita6_bridge",
        plugins / "krita6_bridge",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    shutil.copyfile(root / "plugin/krita6_bridge.desktop", plugins / "krita6_bridge.desktop")
    (plugins / "editing_fixture").mkdir(exist_ok=True)
    shutil.copyfile(root / "tests/host/editing_fixture.py", plugins / "editing_fixture/__init__.py")
    (plugins / "editing_fixture.desktop").write_text(
        "[Desktop Entry]\nType=Service\nServiceTypes=Krita/PythonPlugin\n"
        "X-KDE-Library=editing_fixture\nX-Python-2-Compatible=false\nName=Editing MCP Probe\n"
    )
    (base / "config/kritarc").write_text(
        "[python]\nenable_krita6_bridge=true\nenable_editing_fixture=true\n"
    )
    env.update(
        KRITA6_MCP_STATE_DIR=str(base / "state"),
        KRITA6_MCP_OUTPUT_ROOTS=json.dumps({"scratch": str(base / "artwork")}),
        KRITA6_MCP_INPUT_ROOTS=json.dumps({"scratch": str(base / "artwork")}),
        KRITA6_MCP_AUTOSTART="1",
        KRITA6_EDITING_FIXTURE_OUTPUT=str(base),
    )
    report = {"passed": False}
    with launch_krita(base, args.krita, env) as process:
        try:
            deadline = time.monotonic() + 90
            while True:
                discovery = list((base / "state").glob("instance-*.json"))
                if discovery and (base / "fixture-ready.json").exists():
                    instance_id = json.loads(discovery[0].read_text())["instance_id"]
                    break
                if (base / "fixture-error.json").exists():
                    raise AssertionError((base / "fixture-error.json").read_text())
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Fixture did not start; inspect the isolated krita.log")
                time.sleep(0.1)
            report = asyncio.run(scenario(base, instance_id))
        except Exception:
            report["traceback"] = traceback.format_exc()
        finally:
            (base / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"Artifacts: {base}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
