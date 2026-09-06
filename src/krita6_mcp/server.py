"""Official MCP SDK adapter. stdout belongs exclusively to MCP stdio."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

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
        if response.structured_content.get("command") in {"get_preview", "get_diffusion_result"}:
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
    return server


def main() -> None:
    create_server().run(transport="stdio")
