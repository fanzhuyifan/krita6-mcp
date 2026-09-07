"""Typed native vector tools."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from mcp.types import CallToolResult

from .server import Identifier, Name, Boolean, MUTATION, READ_ONLY

Coordinate = Annotated[float, Field(ge=0, le=8192, allow_inf_nan=False, strict=True)]
Extent = Annotated[float, Field(ge=0.01, le=8192, allow_inf_nan=False, strict=True)]
Point = tuple[Coordinate, Coordinate]
Paint = Annotated[str, Field(pattern=r"^(#[0-9a-fA-F]{6}|none)$")]
Index = Annotated[int, Field(ge=0, le=255, strict=True)]
Translation = Annotated[float, Field(ge=-8192, le=8192, allow_inf_nan=False, strict=True)]
Scale = Annotated[float, Field(ge=0.01, le=100, allow_inf_nan=False, strict=True)]
Rotation = Annotated[float, Field(ge=-360, le=360, allow_inf_nan=False, strict=True)]


class RectGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["rectangle", "ellipse"]
    x: Coordinate
    y: Coordinate
    width: Extent
    height: Extent


class PolygonGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["polygon"]
    points: Annotated[list[Point], Field(min_length=3, max_length=256)]


class BezierGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bezier"]
    start: Point
    segments: Annotated[list[tuple[Point, Point, Point]], Field(min_length=1, max_length=256)]
    closed: Boolean = False


Geometry = Annotated[RectGeometry | PolygonGeometry | BezierGeometry, Field(discriminator="kind")]


def register_vector_tools(server, execute):
    @server.tool(annotations=MUTATION)
    async def krita_create_vector_layer(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        name: Name,
        parent_node_id: Identifier | None = None,
    ) -> CallToolResult:
        """Create a native editable vector layer in the active document, optionally inside a group. Reuse operation_id on retries; no guaranteed undo grouping."""
        params = {"name": name}
        if parent_node_id is not None:
            params["parent_node_id"] = parent_node_id
        return await execute(
            "create_vector_layer", instance_id, operation_id, {"document_id": document_id}, params
        )

    @server.tool(annotations=READ_ONLY)
    async def krita_inspect_vector_layer(
        instance_id: Identifier, document_id: Identifier, node_id: Identifier
    ) -> CallToolResult:
        """Inspect up to 256 top-level vector shapes and pixel bounds. Returns a layer-state snapshot_id plus shape_index addresses, not persistent shape IDs. Reinspect after edits; identical restored states share a snapshot."""
        return await execute(
            "inspect_vector_layer",
            instance_id,
            None,
            {"document_id": document_id, "node_id": node_id},
        )

    @server.tool(annotations=MUTATION)
    async def krita_add_vector_shape(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        geometry: Geometry,
        fill: Paint = "#000000",
        stroke: Paint = "none",
        stroke_width: Annotated[
            float, Field(ge=0.01, le=256, allow_inf_nan=False, strict=True)
        ] = 1,
        name: Name = "Vector shape",
    ) -> CallToolResult:
        """Add an editable rectangle, ellipse, polygon or cubic Bezier shape in image pixels. Solid sRGB fill/stroke or none; active view required. Reinspect for a shape address. Reuse operation_id on retries."""
        return await execute(
            "add_vector_shape",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {
                "geometry": geometry.model_dump(mode="json"),
                "fill": fill,
                "stroke": stroke,
                "stroke_width": stroke_width,
                "name": name,
            },
        )

    @server.tool(annotations=MUTATION)
    async def krita_edit_vector_shape(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        snapshot_id: Identifier,
        shape_index: Index,
        name: Name | None = None,
        visible: Boolean | None = None,
        z_index: Annotated[int, Field(ge=-32768, le=32767, strict=True)] | None = None,
        translate_x: Translation | None = None,
        translate_y: Translation | None = None,
        scale_x: Scale | None = None,
        scale_y: Scale | None = None,
        rotation_degrees: Rotation | None = None,
    ) -> CallToolResult:
        """Edit one top-level path shape using an unchanged layer snapshot. Scale then clockwise rotate about image origin then translate in pixels, composed after its existing transform. Groups/text/protected shapes rejected. Active view required; no guaranteed undo. Reuse operation_id."""
        params = {"snapshot_id": snapshot_id, "shape_index": shape_index}
        params.update(
            {
                k: v
                for k, v in {
                    "name": name,
                    "visible": visible,
                    "z_index": z_index,
                    "translate_x": translate_x,
                    "translate_y": translate_y,
                    "scale_x": scale_x,
                    "scale_y": scale_y,
                    "rotation_degrees": rotation_degrees,
                }.items()
                if v is not None
            }
        )
        return await execute(
            "edit_vector_shape",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            params,
        )

    @server.tool(annotations=MUTATION)
    async def krita_delete_vector_shape(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
        snapshot_id: Identifier,
        shape_index: Index,
    ) -> CallToolResult:
        """Delete one unprotected top-level path shape from an unchanged layer snapshot in the active document. Reuse operation_id; inspect after completion."""
        return await execute(
            "delete_vector_shape",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
            {"snapshot_id": snapshot_id, "shape_index": shape_index},
        )

    @server.tool(annotations=MUTATION)
    async def krita_merge_vector_layer_down(
        instance_id: Identifier,
        operation_id: Identifier,
        document_id: Identifier,
        node_id: Identifier,
    ) -> CallToolResult:
        """Merge into the immediately lower sibling vector layer, preserving editable paths and stacking. Requires active view, visible full-opacity normal layers without masks/alpha inheritance, and unprotected visible path shapes. Source is removed after copying; no atomic undo. Reuse operation_id."""
        return await execute(
            "merge_vector_layer_down",
            instance_id,
            operation_id,
            {"document_id": document_id, "node_id": node_id},
        )
