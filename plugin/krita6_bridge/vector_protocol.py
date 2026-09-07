"""Bounded vector contracts and generated SVG; no Qt or caller-supplied markup."""

from xml.etree import ElementTree as ET

from .general_protocol import color, boolean
from .protocol_validation import _object, _number, _integer, _string, _invalid, _point, validate_id

VECTOR_LAYER_COMMANDS = {
    "merge_vector_layer_down",
    "inspect_vector_layer",
    "add_vector_shape",
    "edit_vector_shape",
    "delete_vector_shape",
}
VECTOR_COMMANDS = VECTOR_LAYER_COMMANDS | {"create_vector_layer"}
VECTOR_MUTATIONS = VECTOR_COMMANDS - {"inspect_vector_layer"}
MAX_SHAPES = 256
MAX_SVG_BYTES = 262144


def vector_point(value):
    point = _point(value)
    return [_number(v, 0, 8192, "coordinate") for v in point]


def validate_geometry(value):
    p = _object(
        value,
        {"kind", "x", "y", "width", "height", "points", "start", "segments", "closed"},
        {"kind"},
    )
    kind = p["kind"]
    if kind in ("rectangle", "ellipse"):
        fields = {"kind", "x", "y", "width", "height"}
        p = _object(p, fields, fields)
        for key in ("x", "y"):
            p[key] = _number(p[key], 0, 8192, key)
        for key in ("width", "height"):
            p[key] = _number(p[key], 0.01, 8192, key)
    elif kind == "polygon":
        p = _object(p, {"kind", "points"}, {"kind", "points"})
        if not isinstance(p["points"], list) or not 3 <= len(p["points"]) <= 256:
            _invalid("Polygon requires 3–256 points")
        p["points"] = [vector_point(v) for v in p["points"]]
    elif kind == "bezier":
        p = _object(p, {"kind", "start", "segments", "closed"}, {"kind", "start", "segments"})
        p["start"] = vector_point(p["start"])
        if not isinstance(p["segments"], list) or not 1 <= len(p["segments"]) <= 256:
            _invalid("Bezier requires 1–256 cubic segments")
        segments = []
        for segment in p["segments"]:
            if not isinstance(segment, list) or len(segment) != 3:
                _invalid("Each cubic segment requires control1, control2, endpoint")
            segments.append([vector_point(v) for v in segment])
        p["segments"] = segments
        p["closed"] = boolean(p.get("closed", False), "closed")
    else:
        _invalid("Unsupported vector geometry")
    return p


def validate_vector(command, value):
    if command == "create_vector_layer":
        p = _object(value, {"name", "parent_node_id"}, {"name"})
        p["name"] = _string(p["name"], 1, 128, "name")
        if "parent_node_id" in p:
            validate_id(p["parent_node_id"])
        return p
    if command in {"inspect_vector_layer", "merge_vector_layer_down"}:
        return _object(value, set())
    if command == "add_vector_shape":
        p = _object(value, {"geometry", "fill", "stroke", "stroke_width", "name"}, {"geometry"})
        p["geometry"] = validate_geometry(p["geometry"])
        for key, default in (("fill", "#000000"), ("stroke", "none")):
            p[key] = p.get(key, default)
            if p[key] != "none":
                p[key] = color(p[key])
        p["stroke_width"] = _number(p.get("stroke_width", 1), 0.01, 256, "stroke_width")
        p["name"] = _string(p.get("name", "Vector shape"), 1, 128, "name")
        if p["fill"] == p["stroke"] == "none":
            _invalid("A shape requires a fill or stroke")
        return p
    fields = {"snapshot_id", "shape_index"}
    extra = {
        "name",
        "visible",
        "z_index",
        "translate_x",
        "translate_y",
        "scale_x",
        "scale_y",
        "rotation_degrees",
    }
    p = _object(value, fields | (extra if command == "edit_vector_shape" else set()), fields)
    validate_id(p["snapshot_id"], "snapshot_id")
    p["shape_index"] = _integer(p["shape_index"], 0, MAX_SHAPES - 1, "shape_index")
    if command == "edit_vector_shape":
        if not (p.keys() & extra):
            _invalid("Provide at least one shape edit")
        if "name" in p:
            p["name"] = _string(p["name"], 1, 128, "name")
        if "visible" in p:
            p["visible"] = boolean(p["visible"], "visible")
        if "z_index" in p:
            p["z_index"] = _integer(p["z_index"], -32768, 32767, "z_index")
        for key in ("translate_x", "translate_y", "rotation_degrees", "scale_x", "scale_y"):
            if key in p:
                lo, hi = (
                    (0.01, 100)
                    if key.startswith("scale")
                    else (-360, 360)
                    if key == "rotation_degrees"
                    else (-8192, 8192)
                )
                p[key] = _number(p[key], lo, hi, key)
    return p


def geometry_points(g):
    if g["kind"] in ("rectangle", "ellipse"):
        return [(g["x"], g["y"]), (g["x"] + g["width"], g["y"] + g["height"])]
    if g["kind"] == "polygon":
        return g["points"]
    return [g["start"]] + [p for segment in g["segments"] for p in segment]


def shape_svg(params, width, height, xres, yres):
    """Explicit point-sized viewport maps the image-pixel viewBox at any DPI."""
    root = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "width": f"{width * 72 / xres:.12g}pt",
            "height": f"{height * 72 / yres:.12g}pt",
            "viewBox": f"0 0 {width} {height}",
            "preserveAspectRatio": "none",
        },
    )
    g = params["geometry"]
    attrs = {
        "fill": params["fill"],
        "stroke": params["stroke"],
        "stroke-width": str(params["stroke_width"]),
    }
    if g["kind"] == "rectangle":
        tag = "rect"
        attrs.update({k: str(g[k]) for k in ("x", "y", "width", "height")})
    elif g["kind"] == "ellipse":
        tag = "ellipse"
        attrs.update(
            cx=str(g["x"] + g["width"] / 2),
            cy=str(g["y"] + g["height"] / 2),
            rx=str(g["width"] / 2),
            ry=str(g["height"] / 2),
        )
    elif g["kind"] == "polygon":
        tag = "polygon"
        attrs["points"] = " ".join(f"{x},{y}" for x, y in g["points"])
    else:
        tag = "path"
        x, y = g["start"]
        parts = [f"M {x} {y}"]
        for segment in g["segments"]:
            parts.append("C " + " ".join(str(v) for point in segment for v in point))
        if g["closed"]:
            parts.append("Z")
        attrs["d"] = " ".join(parts)
    ET.SubElement(root, tag, attrs)
    return ET.tostring(root, encoding="unicode")
