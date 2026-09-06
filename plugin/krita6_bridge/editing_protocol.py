"""Strict, bounded contracts for reference editing; no Qt dependencies."""

from .protocol_validation import _object, _integer, _number, _point, _string, _invalid, validate_id

LAYER_COMMANDS = {"set_layer_properties", "copy_layer", "transform_layer", "move_layer"}
DOCUMENT_COMMANDS = {
    "activate_document",
    "clear_selection",
    "get_region_preview",
    "import_image_layer",
    "set_selection",
}
EDITING_MUTATIONS = (
    LAYER_COMMANDS | (DOCUMENT_COMMANDS - {"get_region_preview"}) | {"open_document"}
)
EDITING_COMMANDS = EDITING_MUTATIONS | {"get_region_preview"}


def rectangle(p):
    for k in ("x", "y"):
        p[k] = _integer(p[k], 0, 2**31 - 1, k)
    for k in ("width", "height"):
        p[k] = _integer(p[k], 1, 8192, k)
    if p["width"] * p["height"] > 16777216:
        _invalid("Region exceeds 16 megapixels")
    return p


def validate_editing(c, p):
    if c in {"activate_document", "clear_selection"}:
        return _object(p, set())
    if c == "get_region_preview":
        p = _object(p, {"x", "y", "width", "height", "max_edge"}, {"x", "y", "width", "height"})
        rectangle(p)
        p["max_edge"] = _integer(p.get("max_edge", 1024), 32, 1024, "max_edge")
    elif c == "set_layer_properties":
        p = _object(p, {"name", "visible", "opacity"})
        if not p:
            _invalid("Provide at least one layer property")
        if "name" in p:
            p["name"] = _string(p["name"], 1, 128, "name")
        if "visible" in p and type(p["visible"]) is not bool:
            _invalid("visible must be a boolean")
        if "opacity" in p:
            p["opacity"] = _number(p["opacity"], 0, 1, "opacity")
    elif c in {"copy_layer", "move_layer"}:
        required = {"destination_document_id", "name"} if c == "copy_layer" else set()
        p = _object(p, required | {"parent_node_id", "above_node_id"}, required)
        for k in set(p) - {"name"}:
            validate_id(p[k], k)
        if "name" in p:
            p["name"] = _string(p["name"], 1, 128, "name")
    elif c == "transform_layer":
        p = _object(
            p,
            {"pivot", "translate_x", "translate_y", "scale_x", "scale_y", "rotation_degrees"},
            {"pivot"},
        )
        p["pivot"] = _point(p["pivot"])
        for v in p["pivot"]:
            _number(v, -32768, 32768, "pivot")
        for k in ("translate_x", "translate_y"):
            p[k] = _number(p.get(k, 0), -32768, 32768, k)
        for k in ("scale_x", "scale_y"):
            p[k] = _number(p.get(k, 1), 0.01, 16, k)
        p["rotation_degrees"] = _number(p.get("rotation_degrees", 0), -360, 360, "rotation_degrees")
    elif c in {"open_document", "import_image_layer"}:
        fields = {"root", "path"} | ({"name", "x", "y"} if c == "import_image_layer" else set())
        p = _object(p, fields, fields)
        validate_id(p["root"], "root")
        p["path"] = _string(p["path"], 1, 4096, "path")
        if "name" in p:
            p["name"] = _string(p["name"], 1, 128, "name")
            for k in ("x", "y"):
                p[k] = _integer(p[k], 0, 2**31 - 1, k)
    elif c == "set_selection":
        p = _object(p, {"shape", "x", "y", "width", "height", "points"}, {"shape"})
        if p["shape"] == "rectangle":
            p = _object(
                p, {"shape", "x", "y", "width", "height"}, {"shape", "x", "y", "width", "height"}
            )
            rectangle(p)
        elif p["shape"] == "polygon":
            p = _object(p, {"shape", "points"}, {"shape", "points"})
            if not isinstance(p["points"], list) or not 3 <= len(p["points"]) <= 256:
                _invalid("polygon requires 3 to 256 points")
            p["points"] = [_point(v, True) for v in p["points"]]
        else:
            _invalid("selection shape must be rectangle or polygon")
    else:
        _invalid("Unknown editing command")
    return p
