"""Dependency-free validation for the private, versioned bridge protocol."""

import math
import re

PROTOCOL_VERSION = 1
PLUGIN_VERSION = "0.1.0"
MUTATIONS = frozenset(
    {
        "create_document",
        "create_paint_layer",
        "paint_path",
        "paint_line",
        "save_document",
        "export_png",
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
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


class BridgeError(Exception):
    def __init__(self, code, message, effect="none"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.effect = effect


def _invalid(message):
    raise BridgeError("INVALID_REQUEST", message)


def validate_id(value, field="identifier"):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _invalid(field + " must be a bounded identifier")
    return value


def _object(value, allowed, required=()):
    if not isinstance(value, dict):
        _invalid("Expected a JSON object")
    if set(value) - set(allowed):
        _invalid("Unknown object fields")
    if set(required) - set(value):
        _invalid("Missing required fields")
    return dict(value)


def _integer(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        _invalid(field + " is outside its integer bounds")
    return value


def _number(value, low=None, high=None, field="number"):
    if type(value) not in (int, float):
        _invalid(field + " must be finite numeric data")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (low is not None and value < low) or (high is not None and value > high):
        _invalid(field + " is outside its finite numeric bounds")
    return float(value)


def _string(value, low, high, field):
    if not isinstance(value, str) or not low <= len(value) <= high or "\x00" in value:
        _invalid(field + " must be bounded text without NUL")
    return value


def _point(value, integer=False):
    if not isinstance(value, list) or len(value) != 2:
        _invalid("Coordinates must be pairs")
    if integer:
        return [_integer(v, -(2**31), 2**31 - 1, "coordinate") for v in value]
    return [_number(v, field="coordinate") for v in value]


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
        if c in {"paint_path", "paint_line"}
        else {"document_id"}
        if c
        in {
            "inspect_document",
            "get_preview",
            "create_paint_layer",
            "save_document",
            "export_png",
            "inspect_diffusion_document",
            "list_diffusion_jobs",
        }
        else set()
    )
    target = _object(r.get("target", {}), target_fields, target_fields)
    for k, v in target.items():
        validate_id(v, k)
    p = r.get("params", {})
    if c in {
        "list_documents",
        "inspect_document",
        "diffusion_status",
        "inspect_diffusion_document",
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
    elif c in {"paint_path", "paint_line"}:
        brush = {"preset_id", "size_px", "opacity", "color"}
        geometry = {"points"} if c == "paint_path" else {"start", "end"}
        pressure = set() if c == "paint_path" else {"pressure_start", "pressure_end"}
        p = _object(p, brush | geometry | pressure, brush | geometry)
        validate_id(p["preset_id"], "preset_id")
        p["size_px"] = _number(p["size_px"], 0.1, 1000, "size_px")
        p["opacity"] = _number(p["opacity"], 0, 1, "opacity")
        if not isinstance(p["color"], str) or not _COLOR.fullmatch(p["color"]):
            _invalid("color must be #RRGGBB")
        p["color"] = p["color"].upper()
        if c == "paint_path":
            if not isinstance(p["points"], list) or not 2 <= len(p["points"]) <= 2048:
                _invalid("points requires 2 to 2048 coordinate pairs")
            p["points"] = [_point(v) for v in p["points"]]
        else:
            p["start"], p["end"] = _point(p["start"], True), _point(p["end"], True)
            for k in pressure:
                p[k] = _number(p.get(k, 1), 0, 1, k)
    else:
        p = _object(p, {"root", "path", "overwrite"}, {"root", "path"})
        validate_id(p["root"], "root")
        p["path"] = _string(p["path"], 1, 4096, "path")
        if type(p.get("overwrite", False)) is not bool:
            _invalid("overwrite must be a boolean")
        p["overwrite"] = p.get("overwrite", False)
    r["target"], r["params"] = target, p
    return r
