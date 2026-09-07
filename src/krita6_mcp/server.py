"""Official MCP SDK adapter. stdout belongs exclusively to MCP stdio."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from krita6_bridge.protocol import BridgeError
from krita6_mcp.bridge_client import BridgeClient

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", strict=True)]
Name = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
Coordinate = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Integer = Annotated[int, Field(strict=True)]
Opacity = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
BrushSize = Annotated[float, Field(ge=0.1, le=1000, allow_inf_nan=False, strict=True)]
Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$", strict=True)]
Point = tuple[Coordinate, Coordinate]
IntegerPoint = tuple[Integer, Integer]
PathPoints = Annotated[list[Point], Field(min_length=2, max_length=2048)]
PixelOffset = Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]
RegionSize = Annotated[int, Field(ge=1, le=8192, strict=True)]
PreviewEdge = Annotated[int, Field(ge=32, le=1024, strict=True)]
TransformCoordinate = Annotated[float, Field(ge=-32768, le=32768, allow_inf_nan=False, strict=True)]
TransformPoint = tuple[TransformCoordinate, TransformCoordinate]
TransformScale = Annotated[float, Field(ge=0.01, le=16, allow_inf_nan=False, strict=True)]
Rotation = Annotated[float, Field(ge=-360, le=360, allow_inf_nan=False, strict=True)]
SelectionCoordinate = Annotated[int, Field(ge=-(2**31), le=2**31 - 1, strict=True)]
SelectionPoint = tuple[SelectionCoordinate, SelectionCoordinate]
PolygonPoints = Annotated[list[SelectionPoint], Field(min_length=3, max_length=256)]
BezierSegment = tuple[Point, Point, Point]
BezierSegments = Annotated[list[BezierSegment], Field(min_length=1, max_length=256)]
RelativePath = Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
Boolean = Annotated[bool, Field(strict=True)]
ControlMode = Literal[
    "reference",
    "style",
    "composition",
    "face",
    "inpaint",
    "universal",
    "scribble",
    "line_art",
    "soft_edge",
    "canny_edge",
    "depth",
    "normal",
    "pose",
    "segmentation",
    "blur",
    "stencil",
    "hands",
]
InpaintMode = Literal[
    "automatic",
    "fill",
    "expand",
    "add_object",
    "remove_object",
    "replace_background",
    "custom",
]
Prompt = Annotated[str, Field(max_length=4096, strict=True)]


class DiffusionControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: Identifier
    mode: ControlMode
    strength: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False, strict=True)] = 1
    start: Opacity = 0
    end: Opacity = 1


READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MUTATION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


def _result(data: dict, *, image: bytes | None = None, error: bool = False) -> CallToolResult:
    content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, allow_nan=False))]
    if image is not None:
        content.append(
            ImageContent(
                type="image", data=base64.b64encode(image).decode("ascii"), mime_type="image/png"
            )
        )
    return CallToolResult(content=content, structured_content=data, is_error=error)


def create_server(client: BridgeClient | None = None) -> MCPServer:
    bridge = client if client is not None else BridgeClient()
    server = MCPServer(
        "krita6-mcp",
        version="0.1.0",
        instructions="Inspect krita_status first. Select explicit instance/document/layer handles. Use krita_diffusion_status and krita_inspect_diffusion_document before generating through the loaded AI Diffusion add-on and its connected local backend. Generation inherits current selection, regional prompts and control layers. A successful generation submission is not a finished image: poll krita_get_diffusion_generation, inspect krita_get_diffusion_result, then explicitly apply it as a new layer. Reuse operation_id for retries of the same edit or generation. Pending bridge operations require reconciliation with krita_get_operation; a timeout never proves that nothing changed. Host capabilities report validation limits.",
    )

    async def call(method, *args, **kwargs) -> CallToolResult:
        try:
            data = await asyncio.to_thread(method, *args, **kwargs)
            return _result(
                data,
                error=bool(data.get("error"))
                or data.get("state") in {"failed", "cancelled", "expired"},
            )
        except BridgeError as exc:
            error_data = {"error": {"code": exc.code, "message": exc.message, "effect": exc.effect}}
            for key in ("instance_id", "operation_id"):
                if kwargs.get(key) is not None:
                    error_data[key] = kwargs[key]
            return _result(error_data, error=True)

    async def execute(command, instance_id, operation_id=None, target=None, params=None):
        return await call(
            bridge.execute,
            command,
            instance_id=instance_id,
            operation_id=operation_id,
            target=target,
            params=params,
        )

    async def attach_preview(response: CallToolResult, instance_id: str) -> CallToolResult:
        data = response.structured_content
        if response.is_error or data.get("state") != "succeeded":
            return response
        artifact_id = (data.get("result") or {}).get("artifact_id")
        if not artifact_id:
            return response
        try:
            png = await asyncio.to_thread(bridge.get_artifact, instance_id, artifact_id)
            return _result(data, image=png)
        except BridgeError as exc:
            return _result(
                {**data, "preview_error": {"code": exc.code, "message": exc.message}},
                error=True,
            )

    @server.tool(annotations=READ_ONLY)
    async def krita_status(instance_id: Identifier | None = None) -> CallToolResult:
        """Discover reachable Krita bridges, exact versions, capabilities and queue health. No document changes."""
        return await call(bridge.status, instance_id=instance_id)

    @server.tool(annotations=READ_ONLY)
    async def krita_list_documents(instance_id: Identifier) -> CallToolResult:
        """List live document handles and active-view status in the selected instance."""
        return await execute("list_documents", instance_id)

    @server.tool(annotations=READ_ONLY)
    async def krita_inspect_document(
        instance_id: Identifier, document_id: Identifier
    ) -> CallToolResult:
        """Inspect document dimensions, color space, editability and layer UUIDs."""
        return await execute("inspect_document", instance_id, target={"document_id": document_id})

    @server.tool(annotations=READ_ONLY)
    async def krita_diffusion_status(instance_id: Identifier) -> CallToolResult:
        """Discover an already loaded Krita AI Diffusion plugin and report integration availability. Does not load plugins or connect to a backend."""
        return await execute("diffusion_status", instance_id)

    @server.tool(annotations=READ_ONLY)
    async def krita_inspect_diffusion_document(
        instance_id: Identifier, document_id: Identifier
    ) -> CallToolResult:
        """Read existing AI Diffusion metadata for the explicit document. Does not create diffusion models, change settings, or generate images."""
        return await execute(
            "inspect_diffusion_document", instance_id, target={"document_id": document_id}
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_list_diffusion_jobs(
        instance_id: Identifier,
        document_id: Identifier,
        offset: Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 50,
    ) -> CallToolResult:
        """Read a bounded page of existing AI Diffusion job metadata for the document. Does not submit, cancel, or apply jobs."""
        return await execute(
            "list_diffusion_jobs",
            instance_id,
            target={"document_id": document_id},
            params={"offset": offset, "limit": limit},
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_list_diffusion_styles(instance_id: Identifier) -> CallToolResult:
        """List available AI Diffusion styles and their handles without changing the current style."""
        return await execute("list_diffusion_styles", instance_id)

    @server.tool(annotations=MUTATION)
    async def krita_configure_diffusion(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        positive_prompt: Prompt | None = None,
        negative_prompt: Prompt | None = None,
        strength: Annotated[float, Field(ge=0.01, le=1, allow_inf_nan=False, strict=True)]
        | None = None,
        seed: Annotated[int, Field(ge=0, le=2**32 - 1, strict=True)] | None = None,
        fixed_seed: Boolean | None = None,
        style_id: Identifier | None = None,
        batch_count: Annotated[int, Field(ge=1, le=16, strict=True)] | None = None,
        region_only: Boolean | None = None,
        resolution_multiplier: Annotated[
            float, Field(ge=0.25, le=2, allow_inf_nan=False, strict=True)
        ]
        | None = None,
        inpaint_mode: InpaintMode | None = None,
        use_inpaint: Boolean | None = None,
        use_prompt_focus: Boolean | None = None,
    ) -> CallToolResult:
        """Persist specified Generate settings without starting a job. Omitted fields stay unchanged. Requires the pinned add-on and active document; style changes require its local backend. Generation requests still supply their own root prompts/strength/seed and request one image. Reuse operation_id on retries; no guaranteed undo."""
        params = {
            key: value
            for key, value in {
                "positive_prompt": positive_prompt,
                "negative_prompt": negative_prompt,
                "strength": strength,
                "seed": seed,
                "fixed_seed": fixed_seed,
                "style_id": style_id,
                "batch_count": batch_count,
                "region_only": region_only,
                "resolution_multiplier": resolution_multiplier,
                "inpaint_mode": inpaint_mode,
                "use_inpaint": use_inpaint,
                "use_prompt_focus": use_prompt_focus,
            }.items()
            if value is not None
        }
        return await execute(
            "configure_diffusion", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_diffusion_controls(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        controls: Annotated[list[DiffusionControl], Field(max_length=64)],
        region_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Replace the root or single-linked region's entire conditioning list; [] clears it. Entries reference existing image layers, with mode, strength 0..2 in steps of 0.02, and 0<=start<=end<=1. Does not generate control maps or images. Inspect is_supported before generation. Requires active Generate document. Reuse operation_id; no guaranteed undo."""
        params = {"controls": [control.model_dump() for control in controls]}
        if region_node_id is not None:
            params["region_node_id"] = region_node_id
        return await execute(
            "set_diffusion_controls",
            instance_id,
            operation_id,
            {"document_id": document_id},
            params,
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_diffusion_region(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        positive_prompt: Prompt | None = None,
        remove: Boolean = False,
    ) -> CallToolResult:
        """Create/update a prompt region linked directly to an existing paint/group layer, or remove that region and its controls without deleting artwork. A prompt is required unless removing. Rejects ambiguous/multiple links. Requires active Generate document. Reuse operation_id; no guaranteed undo."""
        params = {"node_id": node_id, "remove": remove}
        if positive_prompt is not None:
            params["positive_prompt"] = positive_prompt
        return await execute(
            "set_diffusion_region", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_generate_diffusion(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        positive_prompt: Annotated[str, Field(min_length=1, max_length=4096, strict=True)],
        negative_prompt: Annotated[str, Field(max_length=4096, strict=True)] = "",
        strength: Annotated[float, Field(ge=0.01, le=1, allow_inf_nan=False, strict=True)] = 1.0,
        seed: Annotated[int, Field(ge=0, le=2**32 - 1, strict=True)] = 0,
        style_id: Identifier | None = None,
    ) -> CallToolResult:
        """Submit one image through the active document's existing AI Diffusion model and local backend. Inherits canvas selection, regions and controls; strength below 1 refines the canvas. Reuse operation_id on retries. Poll generation_id separately; completion does not automatically apply pixels."""
        params = {
            "positive_prompt": positive_prompt,
            "negative_prompt": negative_prompt,
            "strength": strength,
            "seed": seed,
        }
        if style_id is not None:
            params["style_id"] = style_id
        return await execute(
            "generate_diffusion", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_get_diffusion_generation(
        instance_id: Identifier, document_id: Identifier, generation_id: Identifier
    ) -> CallToolResult:
        """Poll a bridge-owned generation and obtain stable result handles. Job progress belongs to the add-on; a queued job is not proof of backend admission."""
        return await execute(
            "get_diffusion_generation",
            instance_id,
            target={"document_id": document_id},
            params={"generation_id": generation_id},
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_get_diffusion_result(
        instance_id: Identifier,
        document_id: Identifier,
        generation_id: Identifier,
        result_id: Identifier,
        max_edge: Annotated[int, Field(ge=32, le=1024, strict=True)] = 1024,
    ) -> CallToolResult:
        """Inspect a bridge-owned generated image as an inline PNG without selecting its preview or changing canvas layers. Result handles expire when the add-on removes their images."""
        response = await execute(
            "get_diffusion_result",
            instance_id,
            target={"document_id": document_id},
            params={"generation_id": generation_id, "result_id": result_id, "max_edge": max_edge},
        )
        return await attach_preview(response, instance_id)

    @server.tool(annotations=MUTATION)
    async def krita_apply_diffusion_result(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        generation_id: Identifier,
        result_id: Identifier,
    ) -> CallToolResult:
        """Apply an inspected bridge-owned result to its original active document as a new top paint layer at the generation bounds. Reuse operation_id on retries; does not replace layers or resize the canvas."""
        return await execute(
            "apply_diffusion_result",
            instance_id,
            operation_id,
            {"document_id": document_id},
            {"generation_id": generation_id, "result_id": result_id},
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_get_preview(
        instance_id: Identifier,
        document_id: Identifier,
        max_edge: Annotated[int, Field(ge=32, le=1024, strict=True)] = 1024,
    ) -> CallToolResult:
        """Return a settled canvas as an inline PNG with coordinate and color metadata. A pending response can be polled."""
        response = await execute(
            "get_preview",
            instance_id,
            target={"document_id": document_id},
            params={"max_edge": max_edge},
        )
        return await attach_preview(response, instance_id)

    @server.tool(annotations=READ_ONLY)
    async def krita_get_region_preview(
        instance_id: Identifier,
        document_id: Identifier,
        x: PixelOffset,
        y: PixelOffset,
        width: RegionSize,
        height: RegionSize,
        max_edge: PreviewEdge = 1024,
    ) -> CallToolResult:
        """Inspect a settled rectangular canvas crop as an inline PNG with image-space offsets. Region must be in canvas and at most 16 megapixels; output edge is at most 1024. Poll pending previews with krita_get_operation."""
        response = await execute(
            "get_region_preview",
            instance_id,
            target={"document_id": document_id},
            params={"x": x, "y": y, "width": width, "height": height, "max_edge": max_edge},
        )
        return await attach_preview(response, instance_id)

    @server.tool(annotations=MUTATION)
    async def krita_activate_document(
        instance_id: Identifier, operation_id: Identifier, document_id: Identifier
    ) -> CallToolResult:
        """Activate an existing view of the explicit document so native painting can target it. Changes the user's active canvas. Reuse operation_id on retries."""
        return await execute(
            "activate_document", instance_id, operation_id, {"document_id": document_id}
        )

    @server.tool(annotations=MUTATION)
    async def krita_clear_selection(
        instance_id: Identifier, operation_id: Identifier, document_id: Identifier
    ) -> CallToolResult:
        """Clear the explicit document's selection. No guaranteed undo transaction. Reuse operation_id on retries."""
        return await execute(
            "clear_selection", instance_id, operation_id, {"document_id": document_id}
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_selection(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        shape: Literal["rectangle", "polygon"],
        x: PixelOffset | None = None,
        y: PixelOffset | None = None,
        width: RegionSize | None = None,
        height: RegionSize | None = None,
        points: PolygonPoints | None = None,
        mode: Literal["replace", "add", "subtract", "intersect"] = "replace",
    ) -> CallToolResult:
        """Replace or combine the selection with a bounded rectangle or polygon in image pixels. Rectangle requires only x/y/width/height; polygon requires only 3–256 integer points. Selection bounds must fit the canvas and 16 megapixels. No guaranteed undo transaction."""
        params = {"shape": shape, "mode": mode}
        for key, value in (("x", x), ("y", y), ("width", width), ("height", height)):
            if value is not None:
                params[key] = value
        if points is not None:
            params["points"] = [list(point) for point in points]
        return await execute(
            "set_selection", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_open_document(
        instance_id: Identifier,
        operation_id: Identifier,
        root: Identifier,
        path: RelativePath,
    ) -> CallToolResult:
        """Open a bounded local KRA, PNG or JPEG file from a configured input root and attach an active view. Path must be relative to the named root. Reuse operation_id to avoid duplicate opens after a timeout."""
        return await execute(
            "open_document", instance_id, operation_id, params={"root": root, "path": path}
        )

    @server.tool(annotations=MUTATION)
    async def krita_import_image_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        root: Identifier,
        path: RelativePath,
        name: Name,
        x: PixelOffset = 0,
        y: PixelOffset = 0,
    ) -> CallToolResult:
        """Import a bounded PNG or JPEG under a configured input root as a new paint layer at an explicit pixel offset. Image must fit the supported RGBA/U8/sRGB destination; no guaranteed undo transaction. Reuse operation_id on retries."""
        return await execute(
            "import_image_layer",
            instance_id,
            operation_id,
            {"document_id": document_id},
            {"root": root, "path": path, "name": name, "x": x, "y": y},
        )

    @server.tool(annotations=MUTATION)
    async def krita_create_file_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        root: Identifier,
        path: RelativePath,
        name: Name,
        scaling_method: Literal["None", "ToImageSize"] = "None",
        parent_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Create a linked PNG/JPEG file layer from a configured input root. Krita watches the source; keep it available. Supports no scaling or fit to image, with Bicubic filtering. No guaranteed undo. Reuse operation_id on retries."""
        params = {"root": root, "path": path, "name": name, "scaling_method": scaling_method}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        return await execute(
            "create_file_layer", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_file_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        root: Identifier,
        path: RelativePath,
        scaling_method: Literal["None", "ToImageSize"] = "None",
    ) -> CallToolResult:
        """Replace a file layer's linked PNG/JPEG source and scaling, using a configured input root. Uses Bicubic filtering. Source must remain available; no guaranteed undo. Reuse operation_id on retries."""
        return await execute(
            "set_file_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {"root": root, "path": path, "scaling_method": scaling_method},
        )

    @server.tool(annotations=MUTATION)
    async def krita_create_document(
        instance_id: Identifier,
        operation_id: Identifier,
        width: Annotated[int, Field(ge=1, le=8192, strict=True)],
        height: Annotated[int, Field(ge=1, le=8192, strict=True)],
        name: Name,
    ) -> CallToolResult:
        """Create a bounded RGBA/U8/sRGB document with an active view (maximum 16 megapixels). Reuse operation_id on retries."""
        return await execute(
            "create_document",
            instance_id,
            operation_id,
            params={"width": width, "height": height, "name": name},
        )

    @server.tool(annotations=MUTATION)
    async def krita_create_paint_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        name: Name,
        parent_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Create one named paint layer in an explicit document; return its node UUID."""
        params = {"name": name}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        return await execute(
            "create_paint_layer", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_layer_properties(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        name: Name | None = None,
        visible: Boolean | None = None,
        opacity: Opacity | None = None,
        blending_mode: Literal[
            "normal", "multiply", "screen", "overlay", "darken", "lighten", "difference", "addition"
        ]
        | None = None,
        inherit_alpha: Boolean | None = None,
        alpha_locked: Boolean | None = None,
    ) -> CallToolResult:
        """Set paint/group/mask/file-layer name, visibility, or opacity in [0,1]; paint/group/file-layer blend mode and alpha inheritance; paint-layer alpha lock. No guaranteed undo transaction or atomic multi-property rollback. Reuse operation_id on retries."""
        params = {
            key: value
            for key, value in (("name", name), ("visible", visible), ("opacity", opacity))
            if value is not None
        }
        for key, value in (
            ("blending_mode", blending_mode),
            ("inherit_alpha", inherit_alpha),
            ("alpha_locked", alpha_locked),
        ):
            if value is not None:
                params[key] = value
        return await execute(
            "set_layer_properties",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            params,
        )

    @server.tool(annotations=MUTATION)
    async def krita_copy_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        destination_document_id: Identifier,
        name: Name,
        parent_node_id: Identifier | None = None,
        above_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Copy a supported paint layer into an explicit destination document. Omitted parent means document root; above_node_id selects a sibling to insert above. Returns the new node handle. No guaranteed undo transaction. Reuse operation_id to avoid duplicate copies."""
        params = {"destination_document_id": destination_document_id, "name": name}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        if above_node_id is not None:
            params["above_node_id"] = above_node_id
        return await execute(
            "copy_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            params,
        )

    @server.tool(annotations=MUTATION)
    async def krita_transform_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        pivot: TransformPoint,
        translate_x: TransformCoordinate = 0.0,
        translate_y: TransformCoordinate = 0.0,
        scale_x: TransformScale = 1.0,
        scale_y: TransformScale = 1.0,
        rotation_degrees: Rotation = 0.0,
    ) -> CallToolResult:
        """Resample one supported RGBA/U8/sRGB paint layer: scale then clockwise rotate around an explicit image-pixel pivot, then translate. This rewrites raster pixels with no guaranteed undo transaction; it is not a native transform mask. Reuse operation_id on retries."""
        return await execute(
            "transform_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "pivot": list(pivot),
                "translate_x": translate_x,
                "translate_y": translate_y,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "rotation_degrees": rotation_degrees,
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_move_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        parent_node_id: Identifier | None = None,
        above_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Reorder a paint layer or group within its document; rejects cycles and locked/animated subtrees. Omitted parent means document root; above_node_id selects a sibling to insert above. Changes stacking order without translating pixels. No guaranteed undo transaction. Reuse operation_id on retries."""
        params = {}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        if above_node_id is not None:
            params["above_node_id"] = above_node_id
        return await execute(
            "move_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            params,
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_list_brush_presets(
        instance_id: Identifier,
        query: Annotated[str, Field(max_length=256, strict=True)] = "",
        offset: Annotated[int, Field(ge=0, le=2147483647, strict=True)] = 0,
        limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 50,
    ) -> CallToolResult:
        """Search and paginate the current instance's brush preset handles."""
        return await execute(
            "list_brush_presets",
            instance_id,
            params={"query": query, "offset": offset, "limit": limit},
        )

    @server.tool(annotations=MUTATION)
    async def krita_paint_path(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        preset_id: Identifier,
        size_px: BrushSize,
        opacity: Opacity,
        color: Color,
        points: PathPoints,
    ) -> CallToolResult:
        """Paint one native brush path in image pixels. Requires the target's active view; no per-point pressure. Reuse the same operation_id for retries."""
        return await execute(
            "paint_path",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "preset_id": preset_id,
                "size_px": size_px,
                "opacity": opacity,
                "color": color,
                "points": [list(point) for point in points],
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_paint_bezier_path(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        preset_id: Identifier,
        size_px: BrushSize,
        opacity: Opacity,
        color: Color,
        start: Point,
        segments: BezierSegments,
    ) -> CallToolResult:
        """Paint one native cubic Bézier path with 1–256 segments in image pixels. Each segment is [control1, control2, endpoint], each a coordinate pair. Requires the target's active view; no per-point pressure. Reuse operation_id on retries."""
        return await execute(
            "paint_bezier_path",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "preset_id": preset_id,
                "size_px": size_px,
                "opacity": opacity,
                "color": color,
                "start": list(start),
                "segments": [[list(point) for point in segment] for segment in segments],
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_paint_line(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        preset_id: Identifier,
        size_px: BrushSize,
        opacity: Opacity,
        color: Color,
        start: IntegerPoint,
        end: IntegerPoint,
        pressure_start: Opacity = 1.0,
        pressure_end: Opacity = 1.0,
    ) -> CallToolResult:
        """Paint one native line with integer image coordinates and endpoint pressures. Each call is a separate native stroke."""
        return await execute(
            "paint_line",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "preset_id": preset_id,
                "size_px": size_px,
                "opacity": opacity,
                "color": color,
                "start": list(start),
                "end": list(end),
                "pressure_start": pressure_start,
                "pressure_end": pressure_end,
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_save_document(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        root: Identifier,
        path: Annotated[str, Field(min_length=1, max_length=4096, strict=True)],
        overwrite: Annotated[bool, Field(strict=True)] = False,
    ) -> CallToolResult:
        """Save editable .kra work under a configured output root. Path is relative; replacing an existing file requires overwrite=true."""
        return await execute(
            "save_document",
            instance_id,
            operation_id,
            {"document_id": document_id},
            {"root": root, "path": path, "overwrite": overwrite},
        )

    @server.tool(annotations=MUTATION)
    async def krita_export_png(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        root: Identifier,
        path: Annotated[str, Field(min_length=1, max_length=4096, strict=True)],
        overwrite: Annotated[bool, Field(strict=True)] = False,
    ) -> CallToolResult:
        """Export a separate PNG under a configured output root while preserving the document's filename association."""
        return await execute(
            "export_png",
            instance_id,
            operation_id,
            {"document_id": document_id},
            {"root": root, "path": path, "overwrite": overwrite},
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_get_operation(
        instance_id: Identifier, operation_id: Identifier
    ) -> CallToolResult:
        """Reconcile a pending or uncertain operation. Read state/effect separately: failure does not prove no changes occurred."""
        response = await call(
            bridge.get_operation, instance_id=instance_id, operation_id=operation_id
        )
        if response.structured_content.get("command") in {
            "get_preview",
            "get_region_preview",
            "get_layer_preview",
            "get_diffusion_result",
        }:
            return await attach_preview(response, instance_id)
        return response

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    async def krita_cancel_operation(
        instance_id: Identifier, operation_id: Identifier
    ) -> CallToolResult:
        """Cancel queued bridge work atomically. For running work this records intent; it cannot stop Krita or cancel a submitted AI Diffusion backend job."""
        return await call(
            bridge.cancel_operation, instance_id=instance_id, operation_id=operation_id
        )

    async def reject_unknown_arguments(context, call_next):
        # The SDK validates typed values but its generated argument model can
        # ignore extra fields. Reject them before dispatch to prevent a misspelled
        # brush setting from silently changing the requested operation.
        if context.method == "tools/call" and isinstance(context.params, dict):
            arguments = context.params.get("arguments")
            if isinstance(arguments, dict):
                for tool in await server.list_tools():
                    if tool.name == context.params.get("name"):
                        unknown = set(arguments) - set(tool.input_schema.get("properties", {}))
                        if unknown:
                            return _result(
                                {
                                    "error": {
                                        "code": "INVALID_PARAMETERS",
                                        "message": "Unknown tool arguments: "
                                        + ", ".join(sorted(unknown)),
                                        "effect": "none",
                                    }
                                },
                                error=True,
                            )
                        break
        return await call_next(context)

    server.middleware.append(reject_unknown_arguments)
    from .general_tools import register_general_tools

    register_general_tools(server, execute, attach_preview)
    return server


def main() -> None:
    create_server().run(transport="stdio")
