"""Dependency-free validation for the private, versioned bridge protocol."""

import re

from .protocol_validation import (
    BridgeError,
    _integer,
    _invalid,
    _number,
    _object,
    _point,
    _string,
    validate_id,
)
from .editing_protocol import (
    EDITING_COMMANDS,
    EDITING_MUTATIONS,
    LAYER_COMMANDS,
    DOCUMENT_COMMANDS,
    validate_editing,
)

from .diffusion_protocol import CONFIG_COMMANDS, validate_configuration

from .general_protocol import (
    GENERAL_COMMANDS,
    GENERAL_MUTATIONS,
    GENERAL_LAYER_COMMANDS,
    GENERAL_DOCUMENT_COMMANDS,
    validate_general,
)

from .vector_protocol import (
    VECTOR_COMMANDS,
    VECTOR_MUTATIONS,
    VECTOR_LAYER_COMMANDS,
    validate_vector,
)

PROTOCOL_VERSION = 1
PLUGIN_VERSION = "0.1.0"
MUTATIONS = frozenset(
    {
        "create_document",
        "create_paint_layer",
        "paint_path",
        "paint_bezier_path",
        "paint_line",
        "save_document",
        "export_png",
        "generate_diffusion",
        "apply_diffusion_result",
    }
)
COMMANDS = MUTATIONS | {
    "list_documents",
    "inspect_document",
    "get_preview",
    "list_brush_presets",
    "diffusion_status",
    "inspect_diffusion_document",
    "list_diffusion_jobs",
    "list_diffusion_styles",
    "get_diffusion_generation",
    "get_diffusion_result",
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")

MUTATIONS = MUTATIONS | VECTOR_MUTATIONS | EDITING_MUTATIONS | CONFIG_COMMANDS | GENERAL_MUTATIONS
COMMANDS = COMMANDS | VECTOR_COMMANDS | EDITING_COMMANDS | CONFIG_COMMANDS | GENERAL_COMMANDS


def validate_request(body, instance_id):
    """Return a new normalized request, rejecting ambiguous or unknown input."""
    r = _object(
        body,
        {
            "bridge_protocol",
            "instance_id",
            "operation_id",
            "command",
            "target",
            "params",
            "queue_timeout_ms",
        },
        {"bridge_protocol", "instance_id", "operation_id", "command"},
    )
    if type(r["bridge_protocol"]) is not int or r["bridge_protocol"] != PROTOCOL_VERSION:
        raise BridgeError("PROTOCOL_MISMATCH", "Bridge protocol version is not supported")
    validate_id(r["instance_id"], "instance_id")
    if r["instance_id"] != instance_id:
        raise BridgeError("INSTANCE_MISMATCH", "The requested bridge instance is no longer current")
    validate_id(r["operation_id"], "operation_id")
    c = r["command"]
    if not isinstance(c, str) or c not in COMMANDS:
        _invalid("Unknown command")
    r["queue_timeout_ms"] = _integer(
        r.get("queue_timeout_ms", 10000), 1, 120000, "queue_timeout_ms"
    )
    target_fields = (
        {"document_id", "node_id"}
        if c
        in {"paint_path", "paint_line", "paint_bezier_path"}
        | LAYER_COMMANDS
        | GENERAL_LAYER_COMMANDS
        | VECTOR_LAYER_COMMANDS
        else {"document_id"}
        if c
        in DOCUMENT_COMMANDS
        | GENERAL_DOCUMENT_COMMANDS
        | CONFIG_COMMANDS
        | {
            "inspect_document",
            "get_preview",
            "create_paint_layer",
            "create_vector_layer",
            "save_document",
            "export_png",
            "inspect_diffusion_document",
            "list_diffusion_jobs",
            "generate_diffusion",
            "get_diffusion_generation",
            "get_diffusion_result",
            "apply_diffusion_result",
        }
        else set()
    )
    target = _object(r.get("target", {}), target_fields, target_fields)
    for k, v in target.items():
        validate_id(v, k)
    p = r.get("params", {})
    if c in VECTOR_COMMANDS:
        p = validate_vector(c, p)
    elif c in GENERAL_COMMANDS:
        p = validate_general(c, p)
    elif c in CONFIG_COMMANDS:
        p = validate_configuration(c, p)
    elif c in EDITING_COMMANDS:
        p = validate_editing(c, p)
    elif c in {
        "list_documents",
        "inspect_document",
        "diffusion_status",
        "inspect_diffusion_document",
        "list_diffusion_styles",
    }:
        p = _object(p, set())
    elif c == "get_preview":
        p = _object(p, {"max_edge"})
        p["max_edge"] = _integer(p.get("max_edge", 1024), 32, 1024, "max_edge")
    elif c == "list_brush_presets":
        p = _object(p, {"query", "offset", "limit"})
        p["query"] = _string(p.get("query", ""), 0, 256, "query")
        p["offset"] = _integer(p.get("offset", 0), 0, 2**31 - 1, "offset")
        p["limit"] = _integer(p.get("limit", 50), 1, 100, "limit")
    elif c == "list_diffusion_jobs":
        p = _object(p, {"offset", "limit"})
        p["offset"] = _integer(p.get("offset", 0), 0, 2**31 - 1, "offset")
        p["limit"] = _integer(p.get("limit", 50), 1, 100, "limit")
    elif c == "generate_diffusion":
        p = _object(
            p,
            {"positive_prompt", "negative_prompt", "strength", "seed", "style_id"},
            {"positive_prompt"},
        )
        p["positive_prompt"] = _string(p["positive_prompt"], 1, 4096, "positive_prompt")
        p["negative_prompt"] = _string(p.get("negative_prompt", ""), 0, 4096, "negative_prompt")
        p["strength"] = _number(p.get("strength", 1), 0.01, 1, "strength")
        p["seed"] = _integer(p.get("seed", 0), 0, 2**32 - 1, "seed")
        if "style_id" in p:
            validate_id(p["style_id"], "style_id")
    elif c in {"get_diffusion_generation", "get_diffusion_result", "apply_diffusion_result"}:
        required = {"generation_id"}
        if c != "get_diffusion_generation":
            required.add("result_id")
        allowed = required | ({"max_edge"} if c == "get_diffusion_result" else set())
        p = _object(p, allowed, required)
        for field in required:
            validate_id(p[field], field)
        if c == "get_diffusion_result":
            p["max_edge"] = _integer(p.get("max_edge", 1024), 32, 1024, "max_edge")
    elif c == "create_document":
        fields = {"width", "height", "name"}
        p = _object(p, fields, fields)
        p["width"] = _integer(p["width"], 1, 8192, "width")
        p["height"] = _integer(p["height"], 1, 8192, "height")
        if p["width"] * p["height"] > 16777216:
            _invalid("Document area exceeds 16 megapixels")
        p["name"] = _string(p["name"], 1, 128, "name")
    elif c == "create_paint_layer":
        p = _object(p, {"name", "parent_node_id"}, {"name"})
        p["name"] = _string(p["name"], 1, 128, "name")
        if "parent_node_id" in p:
            validate_id(p["parent_node_id"], "parent_node_id")
    elif c in {"paint_path", "paint_line", "paint_bezier_path"}:
        brush = {"preset_id", "size_px", "opacity", "color"}
        geometry = (
            {"start", "segments"}
            if c == "paint_bezier_path"
            else {"points"}
            if c == "paint_path"
            else {"start", "end"}
        )
        pressure = set() if c != "paint_line" else {"pressure_start", "pressure_end"}
        p = _object(p, brush | geometry | pressure, brush | geometry)
        validate_id(p["preset_id"], "preset_id")
        p["size_px"] = _number(p["size_px"], 0.1, 1000, "size_px")
        p["opacity"] = _number(p["opacity"], 0, 1, "opacity")
        if not isinstance(p["color"], str) or not _COLOR.fullmatch(p["color"]):
            _invalid("color must be #RRGGBB")
        p["color"] = p["color"].upper()
        if c == "paint_bezier_path":
            p["start"] = _point(p["start"])
            if not isinstance(p["segments"], list) or not 1 <= len(p["segments"]) <= 256:
                _invalid("segments requires 1 to 256 cubic segments")
            segments = []
            for segment in p["segments"]:
                if not isinstance(segment, list) or len(segment) != 3:
                    _invalid("Each cubic segment needs two controls and an endpoint")
                segments.append([_point(v) for v in segment])
            p["segments"] = segments
        elif c == "paint_path":
            if not isinstance(p["points"], list) or not 2 <= len(p["points"]) <= 2048:
                _invalid("points requires 2 to 2048 coordinate pairs")
            p["points"] = [_point(v) for v in p["points"]]
        else:
            p["start"], p["end"] = _point(p["start"], True), _point(p["end"], True)
            for k in pressure:
                p[k] = _number(p.get(k, 1), 0, 1, k)
    elif c in {"save_document", "export_png"}:
        p = _object(p, {"root", "path", "overwrite"}, {"root", "path"})
        validate_id(p["root"], "root")
        p["path"] = _string(p["path"], 1, 4096, "path")
        if type(p.get("overwrite", False)) is not bool:
            _invalid("overwrite must be a boolean")
        p["overwrite"] = p.get("overwrite", False)
    r["target"], r["params"] = target, p
    return r
