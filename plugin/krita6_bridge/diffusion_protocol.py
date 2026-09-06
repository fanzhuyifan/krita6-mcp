"""Bounded, dependency-free diffusion configuration contracts."""

from .protocol_validation import _integer, _invalid, _number, _object, _string, validate_id

CONFIG_COMMANDS = frozenset(
    {"configure_diffusion", "set_diffusion_controls", "set_diffusion_region"}
)
CONTROL_MODES = (
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
)
INPAINT_MODES = (
    "automatic",
    "fill",
    "expand",
    "add_object",
    "remove_object",
    "replace_background",
    "custom",
)


def validate_configuration(command, params):
    if command == "configure_diffusion":
        p = _object(
            params,
            {
                "positive_prompt",
                "negative_prompt",
                "strength",
                "seed",
                "fixed_seed",
                "style_id",
                "batch_count",
                "region_only",
                "resolution_multiplier",
                "inpaint_mode",
                "use_inpaint",
                "use_prompt_focus",
            },
        )
        if not p:
            _invalid("Provide at least one diffusion setting")
        for key, value in p.items():
            if key in {"positive_prompt", "negative_prompt"}:
                p[key] = _string(value, 0, 4096, key)
            elif key in {"fixed_seed", "region_only", "use_inpaint", "use_prompt_focus"}:
                if type(value) is not bool:
                    _invalid(key + " must be boolean")
            elif key == "seed":
                p[key] = _integer(value, 0, 2**32 - 1, key)
            elif key == "batch_count":
                p[key] = _integer(value, 1, 16, key)
            elif key == "strength":
                p[key] = _number(value, 0.01, 1, key)
            elif key == "resolution_multiplier":
                p[key] = _number(value, 0.25, 2, key)
            elif key == "style_id":
                validate_id(value, key)
            elif value not in INPAINT_MODES:
                _invalid("Unknown inpaint mode")
        return p
    if command == "set_diffusion_region":
        p = _object(params, {"node_id", "positive_prompt", "remove"}, {"node_id"})
        validate_id(p["node_id"], "node_id")
        p["remove"] = p.get("remove", False)
        if type(p["remove"]) is not bool:
            _invalid("remove must be boolean")
        if p["remove"]:
            if "positive_prompt" in p:
                _invalid("Removal cannot also set a prompt")
        else:
            p["positive_prompt"] = _string(p.get("positive_prompt"), 0, 4096, "positive_prompt")
        return p
    p = _object(params, {"controls", "region_node_id"}, {"controls"})
    if "region_node_id" in p:
        validate_id(p["region_node_id"], "region_node_id")
    if type(p["controls"]) is not list or len(p["controls"]) > 64:
        _invalid("controls must be a list of at most 64 entries")
    result = []
    for value in p["controls"]:
        c = _object(value, {"node_id", "mode", "strength", "start", "end"}, {"node_id", "mode"})
        validate_id(c["node_id"], "node_id")
        if c["mode"] not in CONTROL_MODES:
            _invalid("Unknown control mode")
        c["strength"] = _number(c.get("strength", 1), 0, 2, "strength")
        # Upstream strength is an integer percentage with a multiplier of 50.
        scaled = c["strength"] * 50
        if abs(scaled - round(scaled)) > 1e-8:
            _invalid("Control strength must use increments of 0.02")
        c["strength"] = round(scaled) / 50
        c["start"] = _number(c.get("start", 0), 0, 1, "start")
        c["end"] = _number(c.get("end", 1), 0, 1, "end")
        if c["start"] > c["end"]:
            _invalid("Control start must not exceed end")
        result.append(c)
    p["controls"] = result
    return p
