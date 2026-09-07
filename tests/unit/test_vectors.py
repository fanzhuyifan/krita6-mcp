"""Vector schema, state addressing, admission and merge failure regressions.

These plain-data/fake-host tests do not establish native Krita compatibility.
"""

from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest
from pydantic import TypeAdapter, ValidationError

from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError, validate_request
from krita6_bridge.vector_protocol import (
    VECTOR_COMMANDS,
    VECTOR_MUTATIONS,
    validate_vector,
    shape_svg,
)
from krita6_bridge.vector_editing import VectorEditingMixin
from krita6_mcp.vector_tools import Geometry

GEOMETRY = {"kind": "rectangle", "x": 1, "y": 2, "width": 10, "height": 20}
VALID = {
    "create_vector_layer": {"name": "Vectors"},
    "inspect_vector_layer": {},
    "add_vector_shape": {"geometry": GEOMETRY},
    "edit_vector_shape": {"snapshot_id": "snapshot", "shape_index": 0, "translate_x": 4},
    "delete_vector_shape": {"snapshot_id": "snapshot", "shape_index": 0},
    "merge_vector_layer_down": {},
}


def request(command, params=None, operation_id="vector-1"):
    return {
        "bridge_protocol": 1,
        "instance_id": "test",
        "operation_id": operation_id,
        "command": command,
        "target": {
            "document_id": "doc",
            **({} if command == "create_vector_layer" else {"node_id": "layer"}),
        },
        "params": VALID[command] if params is None else params,
    }


@pytest.mark.parametrize("command", sorted(VECTOR_COMMANDS))
def test_vector_contracts(command):
    assert validate_request(request(command), "test")["command"] == command


@pytest.mark.parametrize("command", sorted(VECTOR_MUTATIONS))
def test_vector_cancellation_duplicate_conflict_and_retry(command):
    ledger = OperationLedger("test")
    body = request(command)
    ledger.admit(body)
    ledger.cancel(body["operation_id"])
    assert ledger.take_next() is None
    assert ledger.admit(body)["state"] == "cancelled"
    body = request(command, operation_id="vector-2")
    ledger.admit(body)
    assert ledger.take_next()["command"] == command
    ledger.finish("vector-2", result={"node_id": "result"})
    assert ledger.admit(body)["state"] == "succeeded"
    assert ledger.take_next() is None
    with pytest.raises(BridgeError):
        ledger.admit({**body, "target": {**body["target"], "document_id": "other"}})


@pytest.mark.parametrize(
    "command,params",
    [
        ("add_vector_shape", {"svg": "<svg/>"}),
        ("add_vector_shape", {"geometry": GEOMETRY, "fill": "url(file:///tmp/art.svg)"}),
        ("add_vector_shape", {"geometry": GEOMETRY, "fill": "none", "stroke": "none"}),
        ("add_vector_shape", {"geometry": {**GEOMETRY, "width": True}}),
        ("add_vector_shape", {"geometry": {**GEOMETRY, "x": float("nan")}}),
        ("add_vector_shape", {"geometry": {**GEOMETRY, "x": -1}}),
        ("add_vector_shape", {"geometry": {"kind": "polygon", "points": [[0, 0]] * 257}}),
        (
            "add_vector_shape",
            {"geometry": {"kind": "bezier", "start": [0, 0], "segments": [[[1, 1]]]}},
        ),
        ("edit_vector_shape", {"snapshot_id": "snap", "shape_index": True, "visible": False}),
        ("edit_vector_shape", {"snapshot_id": "snap", "shape_index": 0}),
        ("edit_vector_shape", {"snapshot_id": "snap", "shape_index": 0, "scale_x": 0}),
        (
            "edit_vector_shape",
            {"snapshot_id": "snap", "shape_index": 0, "translate_y": float("inf")},
        ),
        ("merge_vector_layer_down", {"flatten": True}),
    ],
)
def test_invalid_vector_payloads_fail_before_dispatch(command, params):
    with pytest.raises(BridgeError) as exc:
        validate_request(request(command, params), "test")
    assert exc.value.code == "INVALID_REQUEST" and exc.value.effect == "none"


@pytest.mark.parametrize(
    "geometry",
    [
        GEOMETRY,
        {**GEOMETRY, "kind": "ellipse"},
        {"kind": "polygon", "points": [[0, 0], [1, 1], [2, 0]]},
        {"kind": "bezier", "start": [0, 0], "segments": [[[1, 0], [1, 1], [2, 2]]], "closed": True},
    ],
)
def test_typed_svg_has_only_fixed_geometry_and_color_attributes(geometry):
    model = TypeAdapter(Geometry).validate_python(geometry)
    params = validate_vector(
        "add_vector_shape",
        {"geometry": model.model_dump(mode="json"), "name": '<image href="file:///secret"/>'},
    )
    svg = shape_svg(params, 128, 96, 144, 144)
    assert "file:" not in svg and "image" not in svg
    root = ET.fromstring(svg)
    assert root.attrib["width"] == "64pt"
    assert root.attrib["height"] == "48pt"
    assert len(root) == 1


def test_mcp_geometry_rejects_unknown_and_boolean_fields():
    for value in ({**GEOMETRY, "script": "x"}, {**GEOMETRY, "width": True}):
        with pytest.raises(ValidationError):
            TypeAdapter(Geometry).validate_python(value)


def shape():
    return Mock(
        **{
            "type.return_value": "KoPathShape",
            "name.return_value": "Path",
            "visible.return_value": True,
            "geometryProtected.return_value": False,
            "zIndex.return_value": 0,
            "boundingBox.return_value": SimpleNamespace(
                x=lambda: 1, y=lambda: 2, width=lambda: 10, height=lambda: 20
            ),
        }
    )


def test_snapshot_includes_target_svg_visibility_protection_and_resolution():
    host = VectorEditingMixin()
    item = shape()
    node = Mock(**{"shapes.return_value": [item], "toSvg.return_value": "<svg/>"})
    doc = Mock(
        **{
            "width.return_value": 128,
            "height.return_value": 96,
            "xRes.return_value": 144,
            "yRes.return_value": 144,
        }
    )
    target = {"document_id": "doc", "node_id": "layer"}

    def inspect(t=target):
        return host._vector_snapshot(t, doc, node)[1]

    first = inspect()
    assert first == inspect()
    assert first["shapes"][0]["bounds_px"] == [2, 4, 20, 40]
    assert inspect({**target, "document_id": "reopened"})["snapshot_id"] != first["snapshot_id"]
    for obj, method, value in [
        (item, "visible", False),
        (item, "geometryProtected", True),
        (doc, "xRes", 72),
        (node, "toSvg", "<svg><path/></svg>"),
    ]:
        getattr(obj, method).return_value = value
        current = inspect()
        assert current["snapshot_id"] != first["snapshot_id"]
        first = current


@pytest.mark.parametrize("change", ["stale", "range", "locked", "group"])
def test_shape_resolution_rejects_stale_out_of_range_protected_and_group(change):
    host = VectorEditingMixin()
    item = shape()
    host._vector_target = Mock(return_value=(object(), object()))
    host._vector_snapshot = Mock(return_value=([item], {"snapshot_id": "current"}))
    params = {
        "snapshot_id": "old" if change == "stale" else "current",
        "shape_index": 1 if change == "range" else 0,
    }
    if change == "locked":
        item.geometryProtected.return_value = True
    if change == "group":
        item.type.return_value = "groupshape"
    with pytest.raises(BridgeError):
        host._resolve_vector_shape({}, params)
    item.remove.assert_not_called()


def test_merge_partial_copy_never_removes_source():
    host = VectorEditingMixin()
    doc = Mock()
    parent = Mock(**{"parentNode.return_value": None, "type.return_value": "root"})
    source, destination = Mock(), Mock()
    for node in (source, destination):
        node.parentNode.return_value = parent
        node.visible.return_value = True
        node.opacity.return_value = 255
        node.blendingMode.return_value = "normal"
        node.inheritAlpha.return_value = False
        node.alphaLocked.return_value = False
        node.toSvg.return_value = "<svg/>"
        node.layerStyleToAsl.return_value = ""
        node.isAntialiased.return_value = True
    parent.childNodes.return_value = [destination, source]
    host._vector_target = Mock(side_effect=[(doc, source), (doc, destination)])
    host._node_id = Mock(return_value="destination")
    host._vector_snapshot = Mock(side_effect=[([shape()], {}), ([], {})])
    host._editing_mutate = lambda document_id, callback, result: callback()
    destination.addShapesFromSvg.return_value = []
    with pytest.raises(BridgeError) as exc:
        host._merge_vector_layer_down({"document_id": "doc", "node_id": "source"}, {})
    assert exc.value.effect == "partial"
    source.remove.assert_not_called()


def test_add_reserves_serialization_headroom_before_native_dispatch():
    host = VectorEditingMixin()
    doc = Mock(
        **{
            "width.return_value": 128,
            "height.return_value": 96,
            "xRes.return_value": 72,
            "yRes.return_value": 72,
        }
    )
    node = Mock(**{"toSvg.return_value": "x" * 210000})
    host._vector_target = Mock(return_value=(doc, node))
    host._vector_snapshot = Mock(return_value=([], {}))
    params = validate_vector("add_vector_shape", {"geometry": GEOMETRY})
    with pytest.raises(BridgeError) as exc:
        host._add_vector_shape({"document_id": "doc", "node_id": "layer"}, params)
    assert exc.value.code == "SIZE_LIMIT" and exc.value.effect == "none"
    node.addShapesFromSvg.assert_not_called()
