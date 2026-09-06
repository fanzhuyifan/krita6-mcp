"""Typed general-editing MCP catalog; shares the server's bridge execution path."""

from typing import Annotated, Literal
from pydantic import Field
from mcp.types import CallToolResult

from .server import (
    Identifier,
    Name,
    Boolean,
    IntegerPoint,
    Color,
    Opacity,
    BrushSize,
    RegionSize,
    PreviewEdge,
    MUTATION,
    READ_ONLY,
)


def register_general_tools(server, execute, attach_preview):
    @server.tool(annotations=MUTATION)
    async def krita_create_group_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        name: Name,
        parent_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Create a group at the top of the explicit parent (document root by default). Returns its handle. Reuse operation_id on retries; no guaranteed undo grouping."""
        params = {"name": name}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        return await execute(
            "create_group_layer", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_create_transparency_mask(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        name: Name,
        source: Literal["selection", "opaque", "transparent"],
    ) -> CallToolResult:
        """Attach a transparency mask to an explicit paint/group layer, using a copy of the selection or constant canvas opacity. Preserves the canvas selection. Reuse operation_id on retries."""
        return await execute(
            "create_transparency_mask",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {"name": name, "source": source},
        )

    @server.tool(annotations=MUTATION)
    async def krita_set_transparency_mask(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        source: Literal["selection", "opaque", "transparent"],
    ) -> CallToolResult:
        """Replace an existing transparency mask's coverage with a selection copy or constant canvas opacity. Reuse operation_id; no guaranteed undo transaction."""
        return await execute(
            "set_transparency_mask",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {"source": source},
        )

    @server.tool(annotations=MUTATION)
    async def krita_delete_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
    ) -> CallToolResult:
        """Delete the explicit paint/group/mask node and its bounded subtree. Rejects locked/animated descendants and the last top-level layer. Returns removed handles. Reuse operation_id on retries."""
        return await execute(
            "delete_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
        )

    @server.tool(annotations=MUTATION)
    async def krita_merge_layer_down(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
    ) -> CallToolResult:
        """Merge a visible, nonanimated paint layer with the adjacent paint layer below it. Requires no masks/children or alpha inheritance on either. Returns the resulting handle after native completion. Reuse operation_id."""
        return await execute(
            "merge_layer_down",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
        )

    @server.tool(annotations=MUTATION)
    async def krita_edit_history(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        direction: Literal["undo", "redo"],
    ) -> CallToolResult:
        """Perform exactly one enabled native undo/redo step on the active document, then await completion. History includes user edits and is not scoped to bridge operations. Reuse operation_id to avoid stepping twice after timeout."""
        return await execute(
            "edit_history",
            instance_id,
            operation_id,
            {"document_id": document_id},
            {"direction": direction},
        )

    @server.tool(annotations=MUTATION)
    async def krita_modify_selection(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        action: Literal["invert", "grow", "shrink", "feather"],
        radius: Annotated[int, Field(ge=1, le=256, strict=True)] | None = None,
    ) -> CallToolResult:
        """Refine an existing selection and clip the result to the canvas. Grow/shrink/feather require radius in pixels; invert forbids radius. Reuse operation_id; no guaranteed undo transaction."""
        params = {"action": action}
        if radius is not None:
            params["radius"] = radius
        return await execute(
            "modify_selection", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_transform_canvas(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        action: Literal["crop", "resize", "scale", "rotate", "flip"],
        x: Annotated[int, Field(ge=-8192, le=8192, strict=True)] | None = None,
        y: Annotated[int, Field(ge=-8192, le=8192, strict=True)] | None = None,
        width: RegionSize | None = None,
        height: RegionSize | None = None,
        degrees: Literal[-180, -90, 90, 180] | None = None,
        axis: Literal["horizontal", "vertical"] | None = None,
        filter: Literal["Bicubic", "Bilinear", "NearestNeighbor"] | None = None,
    ) -> CallToolResult:
        """Transform the entire document, bounded to 8192 per side/16 MP. Crop/resize require x,y,width,height; resize uses the new canvas rectangle in old image coordinates. Scale requires width,height and optional filter. Rotate requires degrees; flip requires axis and active document. Rejects unrelated fields and locked/animated nodes. Reuse operation_id."""
        params = {"action": action}
        for key, value in (
            ("x", x),
            ("y", y),
            ("width", width),
            ("height", height),
            ("degrees", degrees),
            ("axis", axis),
            ("filter", filter),
        ):
            if value is not None:
                params[key] = value
        return await execute(
            "transform_canvas", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=MUTATION)
    async def krita_paint_shape(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        shape: Literal["rectangle", "ellipse"],
        x: Annotated[int, Field(ge=0, le=8192, strict=True)],
        y: Annotated[int, Field(ge=0, le=8192, strict=True)],
        width: RegionSize,
        height: RegionSize,
        preset_id: Identifier,
        size_px: BrushSize,
        opacity: Opacity,
        color: Color,
        fill: Boolean = False,
    ) -> CallToolResult:
        """Paint one native rectangle/ellipse with explicit brush settings and optional solid foreground fill. Requires active document, pixel brush and no selection. Restores brush context, waits for native completion. Reuse operation_id."""
        return await execute(
            "paint_shape",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "shape": shape,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "preset_id": preset_id,
                "size_px": size_px,
                "opacity": opacity,
                "color": color,
                "fill": fill,
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_fill_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        kind: Literal["solid", "linear_gradient", "flood", "erase"],
        color: Color | None = None,
        opacity: Opacity = 1.0,
        end_color: Color | None = None,
        start: IntegerPoint | None = None,
        end: IntegerPoint | None = None,
        point: IntegerPoint | None = None,
    ) -> CallToolResult:
        """Raster source-over fill or destination-out erase on a simple RGBA/U8/sRGB paint layer, limited to 1 MP and masked by the current selection. Erase forbids color and uses opacity/selection coverage. Other modes require color. Linear gradient needs end_color/start/end; flood needs point and matches exact layer BGRA with four-connected neighbors. No brush simulation, tolerance, or guaranteed undo. Reuse operation_id."""
        params = {"kind": kind, "opacity": opacity}
        if color is not None:
            params["color"] = color
        if end_color is not None:
            params["end_color"] = end_color
        for key, value in (("start", start), ("end", end), ("point", point)):
            if value is not None:
                params[key] = list(value)
        return await execute(
            "fill_layer",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            params,
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_get_layer_preview(
        instance_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        max_edge: PreviewEdge = 1024,
    ) -> CallToolResult:
        """Return an inline PNG of the explicit layer/group projection within canvas bounds. Requires RGBA/U8/standard sRGB; preserves visibility and active layer. Other layers are excluded."""
        response = await execute(
            "get_layer_preview",
            instance_id,
            target={"document_id": document_id, "node_id": node_id},
            params={"max_edge": max_edge},
        )

        return await attach_preview(response, instance_id)

    @server.tool(annotations=READ_ONLY)
    async def krita_sample_color(
        instance_id: Identifier,
        document_id: Identifier,
        x: Annotated[int, Field(ge=0, le=8192, strict=True)],
        y: Annotated[int, Field(ge=0, le=8192, strict=True)],
        node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Read one settled RGBA/U8/sRGB projection pixel from the canvas or explicit node. Returns hex color and alpha without changing foreground color."""
        params = {"x": x, "y": y}
        if node_id is not None:
            params["node_id"] = node_id
        return await execute(
            "sample_color", instance_id, target={"document_id": document_id}, params=params
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_inspect_brush(
        instance_id: Identifier, document_id: Identifier
    ) -> CallToolResult:
        """Inspect the active document view's brush preset name, size, opacity, flow, rotation, blending, eraser, alpha lock, pressure and foreground color without changing them. Use list_brush_presets for painting handles."""
        return await execute("inspect_brush", instance_id, target={"document_id": document_id})
