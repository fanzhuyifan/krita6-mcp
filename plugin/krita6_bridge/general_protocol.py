"""Typed contracts for general editing and inspection, without Qt dependencies."""

import re

from .protocol_validation import _object, _integer, _number, _point, _string, _invalid, validate_id

BLEND_MODES = (
    "normal",
    "multiply",
    "screen",
    "overlay",
    "darken",
    "lighten",
    "difference",
    "addition",
)
GENERAL_LAYER_COMMANDS = {
    "create_transparency_mask",
    "set_transparency_mask",
    "delete_layer",
    "merge_layer_down",
    "paint_shape",
    "fill_layer",
    "get_layer_preview",
}
GENERAL_DOCUMENT_COMMANDS = {
    "create_group_layer",
    "edit_history",
    "modify_selection",
    "transform_canvas",
    "sample_color",
    "inspect_brush",
}
GENERAL_READS = {"get_layer_preview", "sample_color", "inspect_brush"}
GENERAL_COMMANDS = GENERAL_LAYER_COMMANDS | GENERAL_DOCUMENT_COMMANDS
GENERAL_MUTATIONS = GENERAL_COMMANDS - GENERAL_READS


def color(value):
    if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
        _invalid("Color must be #RRGGBB")
    return value.upper()


def boolean(value, name):
    if type(value) is not bool:
        _invalid(name + " must be boolean")
    return value


def dimensions(p):
    for k in ("width", "height"):
        p[k] = _integer(p[k], 1, 8192, k)
    if p["width"] * p["height"] > 16777216:
        _invalid("Area exceeds 16 megapixels")


def validate_general(c, p):
    if c in {"delete_layer", "merge_layer_down", "inspect_brush"}:
        return _object(p, set())
    if c == "create_group_layer":
        p = _object(p, {"name", "parent_node_id"}, {"name"})
        p["name"] = _string(p["name"], 1, 128, "name")
        if "parent_node_id" in p:
            validate_id(p["parent_node_id"])
    elif c in {"create_transparency_mask", "set_transparency_mask"}:
        fields = {"source"} | ({"name"} if c == "create_transparency_mask" else set())
        p = _object(p, fields, fields)
        if "name" in p:
            p["name"] = _string(p["name"], 1, 128, "name")
        if p["source"] not in ("selection", "opaque", "transparent"):
            _invalid("Mask source must be selection, opaque, or transparent")
    elif c == "edit_history":
        p = _object(p, {"direction"}, {"direction"})
        if p["direction"] not in ("undo", "redo"):
            _invalid("History direction must be undo or redo")
    elif c == "modify_selection":
        p = _object(p, {"action", "radius"}, {"action"})
        if p["action"] == "invert":
            p = _object(p, {"action"}, {"action"})
        elif p["action"] in ("grow", "shrink", "feather"):
            p["radius"] = _integer(p.get("radius"), 1, 256, "radius")
        else:
            _invalid("Unknown selection action")
    elif c == "transform_canvas":
        p = _object(
            p, {"action", "x", "y", "width", "height", "degrees", "axis", "filter"}, {"action"}
        )
        action = p["action"]
        if action in ("crop", "resize"):
            p = _object(
                p, {"action", "x", "y", "width", "height"}, {"action", "x", "y", "width", "height"}
            )
            for k in ("x", "y"):
                p[k] = _integer(p[k], -8192 if action == "resize" else 0, 8192, k)
            dimensions(p)
        elif action == "scale":
            p = _object(p, {"action", "width", "height", "filter"}, {"action", "width", "height"})
            dimensions(p)
            p["filter"] = p.get("filter", "Bicubic")
            if p["filter"] not in ("Bicubic", "Bilinear", "NearestNeighbor"):
                _invalid("Unsupported scaling filter")
        elif action == "rotate":
            p = _object(p, {"action", "degrees"}, {"action", "degrees"})
            p["degrees"] = _integer(p["degrees"], -180, 180, "degrees")
            if p["degrees"] not in (-180, -90, 90, 180):
                _invalid("Rotate by -180, -90, 90, or 180 degrees")
        elif action == "flip":
            p = _object(p, {"action", "axis"}, {"action", "axis"})
            if p["axis"] not in ("horizontal", "vertical"):
                _invalid("Flip axis must be horizontal or vertical")
        else:
            _invalid("Unknown canvas action")
    elif c == "paint_shape":
        fields = {
            "shape",
            "x",
            "y",
            "width",
            "height",
            "preset_id",
            "size_px",
            "opacity",
            "color",
            "fill",
        }
        p = _object(p, fields, fields - {"fill"})
        if p["shape"] not in ("rectangle", "ellipse"):
            _invalid("Shape must be rectangle or ellipse")
        for k in ("x", "y"):
            p[k] = _integer(p[k], 0, 8192, k)
        dimensions(p)
        validate_id(p["preset_id"])
        p["size_px"] = _number(p["size_px"], 0.1, 1000, "size_px")
        p["opacity"] = _number(p["opacity"], 0, 1, "opacity")
        p["color"] = color(p["color"])
        p["fill"] = boolean(p.get("fill", False), "fill")
    elif c == "fill_layer":
        p = _object(p, {"kind", "color", "opacity", "end_color", "start", "end", "point"}, {"kind"})
        common = {"kind", "opacity"} if p["kind"] == "erase" else {"kind", "color", "opacity"}
        fields = (
            common | {"start", "end", "end_color"}
            if p["kind"] == "linear_gradient"
            else common | {"point"}
            if p["kind"] == "flood"
            else common
        )
        if p["kind"] not in ("solid", "linear_gradient", "flood", "erase"):
            _invalid("Unsupported fill kind")
        p = _object(p, fields, fields - {"opacity"})
        p["opacity"] = _number(p.get("opacity", 1), 0, 1, "opacity")
        if "color" in p:
            p["color"] = color(p["color"])
        if "end_color" in p:
            p["end_color"] = color(p["end_color"])
        for k in ("start", "end", "point"):
            if k in p:
                p[k] = _point(p[k], integer=True)
        if "start" in p and p["start"] == p["end"]:
            _invalid("Gradient endpoints must differ")
    elif c == "sample_color":
        p = _object(p, {"x", "y", "node_id"}, {"x", "y"})
        for k in ("x", "y"):
            p[k] = _integer(p[k], 0, 8192, k)
        if "node_id" in p:
            validate_id(p["node_id"])
    elif c == "get_layer_preview":
        p = _object(p, {"max_edge"})
        p["max_edge"] = _integer(p.get("max_edge", 1024), 32, 1024, "max_edge")
    else:
        _invalid("Unknown general editing command")
    return p
