"""Bounds, targeting, raster connectivity and retry regressions for general editing."""

from types import SimpleNamespace

import pytest

from krita6_bridge.general_editing import GeneralEditingMixin, flood_coverage
from krita6_bridge.general_protocol import GENERAL_MUTATIONS
from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError, validate_request


def request(command, params=None, operation_id="edit-1"):
    from krita6_bridge.general_protocol import GENERAL_LAYER_COMMANDS

    target = {"document_id": "doc"}
    if command in GENERAL_LAYER_COMMANDS:
        target["node_id"] = "layer"
    return dict(
        bridge_protocol=1,
        instance_id="test",
        operation_id=operation_id,
        command=command,
        target=target,
        params=params or {},
    )


VALID = {
    "create_group_layer": {"name": "Group"},
    "create_transparency_mask": {"name": "Mask", "source": "selection"},
    "set_transparency_mask": {"source": "opaque"},
    "delete_layer": {},
    "merge_layer_down": {},
    "edit_history": {"direction": "undo"},
    "modify_selection": {"action": "feather", "radius": 2},
    "transform_canvas": {"action": "resize", "x": -2, "y": -2, "width": 64, "height": 64},
    "paint_shape": {
        "shape": "ellipse",
        "x": 0,
        "y": 0,
        "width": 10,
        "height": 10,
        "preset_id": "preset",
        "size_px": 3,
        "opacity": 1,
        "color": "#AABBCC",
    },
    "fill_layer": {"kind": "solid", "color": "#AABBCC"},
}


@pytest.mark.parametrize("command", sorted(GENERAL_MUTATIONS))
def test_cancelled_or_completed_general_mutations_never_dispatch_twice(command):
    ledger = OperationLedger("test")
    body = request(command, VALID[command])
    ledger.admit(body)
    ledger.cancel(body["operation_id"])
    assert ledger.take_next() is None
    assert ledger.admit(body)["state"] == "cancelled"
    body["operation_id"] = "edit-2"
    ledger.admit(body)
    assert ledger.take_next()["command"] == command
    ledger.finish("edit-2", result={"done": True})
    assert ledger.admit(body)["state"] == "succeeded"
    assert ledger.take_next() is None
    changed = {**body, "target": {**body["target"], "document_id": "other"}}
    with pytest.raises(BridgeError) as error:
        ledger.admit(changed)
    assert error.value.code == "OPERATION_ID_CONFLICT"


@pytest.mark.parametrize(
    "command,params",
    [
        ("edit_history", {"direction": "edit_paste"}),
        ("edit_history", {"direction": "undo", "steps": 100}),
        ("create_group_layer", {"name": ""}),
        ("create_transparency_mask", {"name": "Mask", "source": "file"}),
        ("modify_selection", {"action": "invert", "radius": 1}),
        ("modify_selection", {"action": "grow", "radius": True}),
        ("modify_selection", {"action": "feather", "radius": 257}),
        ("transform_canvas", {"action": "rotate", "degrees": 45}),
        ("transform_canvas", {"action": "flip", "axis": "both"}),
        ("transform_canvas", {"action": "scale", "width": 8192, "height": 8192}),
        ("transform_canvas", {"action": "scale", "width": 64, "height": 64, "filter": "script"}),
        ("transform_canvas", {"action": "crop", "x": -1, "y": 0, "width": 32, "height": 32}),
        ("fill_layer", {"kind": "solid", "color": "#00FF00", "point": [0, 0]}),
        ("fill_layer", {"kind": "flood", "color": "#00FF00"}),
        (
            "fill_layer",
            {
                "kind": "linear_gradient",
                "color": "#00FF00",
                "end_color": "#FFFFFF",
                "start": [1, 1],
                "end": [1, 1],
            },
        ),
        ("sample_color", {"x": True, "y": 0}),
        ("get_layer_preview", {"max_edge": 4096}),
        ("paint_shape", {**VALID["paint_shape"], "eraser": 1}),
        ("paint_shape", {**VALID["paint_shape"], "opacity": float("inf")}),
    ],
)
def test_invalid_geometry_options_and_types_rejected_before_admission(command, params):
    with pytest.raises(BridgeError) as error:
        validate_request(request(command, params), "test")
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.effect == "none"


def test_flood_is_four_connected_and_respects_barriers_and_soft_selection():
    red, blue = bytes((0, 0, 255, 255)), bytes((255, 0, 0, 255))
    pixels = red + blue + red + blue + red + red + red + red + red
    assert flood_coverage(pixels, 3, 3, bytes([255]) * 9, 0) == bytes([255] + [0] * 8)
    mask = bytes([255, 255, 128, 255, 0, 255, 255, 255, 255])
    result = flood_coverage(pixels, 3, 3, mask, 2)
    assert result == bytes([0, 0, 128, 0, 0, 255, 255, 255, 255])
    with pytest.raises(BridgeError) as error:
        flood_coverage(pixels, 3, 3, mask, 4)
    assert error.value.code == "SEED_OUTSIDE_SELECTION"


def test_bounded_subtree_rejects_locked_descendants():
    child = SimpleNamespace(locked=lambda: True, animated=lambda: False, childNodes=lambda: [])
    parent = SimpleNamespace(
        locked=lambda: False, animated=lambda: False, childNodes=lambda: [child]
    )
    with pytest.raises(BridgeError) as error:
        GeneralEditingMixin()._bounded_subtree(parent)
    assert error.value.code == "TARGET_LOCKED"


def test_structure_rejects_document_root_and_animation():
    root = object()
    host = GeneralEditingMixin()
    doc = SimpleNamespace(rootNode=lambda: root)
    host._node = lambda *args: root
    with pytest.raises(BridgeError):
        host._structure_node(doc, "root")
    host._node = lambda *args: SimpleNamespace(type=lambda: "paintlayer", animated=lambda: True)
    with pytest.raises(BridgeError):
        host._structure_node(doc, "animated")


def test_erase_requires_no_color_and_preserves_opacity():
    validated = validate_request(request("fill_layer", {"kind": "erase", "opacity": 0.5}), "test")
    assert validated["params"] == {"kind": "erase", "opacity": 0.5}
    for params in ({"kind": "solid"}, {"kind": "erase", "color": "#FFFFFF"}):
        with pytest.raises(BridgeError):
            validate_request(request("fill_layer", params), "test")


def test_history_requires_active_document_before_resolving_action():
    host = GeneralEditingMixin()
    requested = object()
    host._editing_document = lambda _: requested
    host.app = SimpleNamespace(
        activeWindow=lambda: SimpleNamespace(
            activeView=lambda: SimpleNamespace(document=lambda: object())
        )
    )
    with pytest.raises(BridgeError) as error:
        host._edit_history({"document_id": "doc"}, {"direction": "undo"})
    assert error.value.code == "TARGET_NOT_ACTIVE"


def test_disabled_history_never_triggers_action():
    host = GeneralEditingMixin()
    host._editing_document = lambda _: object()
    host._active_view = lambda _: None
    host.app = SimpleNamespace(action=lambda _: SimpleNamespace(isEnabled=lambda: False))
    with pytest.raises(BridgeError) as error:
        host._edit_history({"document_id": "doc"}, {"direction": "redo"})
    assert error.value.code == "HISTORY_UNAVAILABLE"


def test_structure_allows_hidden_layer_so_it_can_be_shown_again():
    host = GeneralEditingMixin()
    node = SimpleNamespace(
        type=lambda: "grouplayer",
        animated=lambda: False,
        locked=lambda: False,
        visible=lambda: False,
        parentNode=lambda: None,
    )
    host._node = lambda *_: node
    assert host._structure_node(SimpleNamespace(rootNode=lambda: object()), "group") is node


@pytest.mark.parametrize("changes_tree", [True, False])
def test_merge_reconciles_actual_tree_when_native_return_is_none(changes_tree):
    host = GeneralEditingMixin()
    children = []
    parent = SimpleNamespace(childNodes=lambda: list(children))
    below = SimpleNamespace(identifier="below", visible=lambda: True, inheritAlpha=lambda: False)
    merged = SimpleNamespace(identifier="merged", type=lambda: "paintlayer")

    def merge():
        if changes_tree:
            children[:] = [merged]
        return None

    top = SimpleNamespace(
        identifier="top",
        parentNode=lambda: parent,
        visible=lambda: True,
        inheritAlpha=lambda: False,
        mergeDown=merge,
    )
    children[:] = [below, top]
    host._editing_document = lambda _: object()
    host._editing_node = lambda doc, identifier: {"top": top, "below": below}[identifier]
    host._writable_color = lambda _: None
    host._node_id = lambda node: node.identifier

    def mutate(doc, callback, result):
        callback()
        return result

    host._editing_mutate = mutate
    if changes_tree:
        assert (
            host._merge_layer_down({"document_id": "doc", "node_id": "top"}, {})["node_id"]
            == "merged"
        )
    else:
        with pytest.raises(BridgeError) as error:
            host._merge_layer_down({"document_id": "doc", "node_id": "top"}, {})
        assert error.value.effect == "unknown"
