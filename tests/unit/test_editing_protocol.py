"""Reference-editing contracts and ledger safety; no Krita compatibility claims."""

import copy
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from krita6_bridge.editing_protocol import validate_editing
from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError, MUTATIONS, validate_request


DOCUMENT = {"document_id": "doc-1"}
LAYER = {**DOCUMENT, "node_id": "node-1"}
BRUSH = {"preset_id": "brush-1", "size_px": 5, "opacity": 1, "color": "#abcdef"}
SAMPLES = {
    "create_file_layer": (DOCUMENT, {"root": "input", "path": "ref.png", "name": "Ref"}),
    "set_file_layer": ({**DOCUMENT, "node_id": "node-1"}, {"root": "input", "path": "ref.png"}),
    "activate_document": (DOCUMENT, {}),
    "clear_selection": (DOCUMENT, {}),
    "get_region_preview": (DOCUMENT, {"x": 0, "y": 1, "width": 64, "height": 32}),
    "set_layer_properties": (LAYER, {"name": "Overlay", "visible": False, "opacity": 0.5}),
    "copy_layer": (LAYER, {"destination_document_id": "doc-2", "name": "Copy"}),
    "transform_layer": (LAYER, {"pivot": [10, 20]}),
    "move_layer": (LAYER, {"parent_node_id": "group-1", "above_node_id": "node-2"}),
    "open_document": ({}, {"root": "input", "path": "reference.kra"}),
    "import_image_layer": (
        DOCUMENT,
        {"root": "input", "path": "reference.png", "name": "Reference", "x": 0, "y": 0},
    ),
    "set_selection": (DOCUMENT, {"shape": "polygon", "points": [[0, 0], [10, 10], [20, 0]]}),
    "paint_bezier_path": (
        LAYER,
        {**BRUSH, "start": [1, 1], "segments": [[[2, 3], [4, 3], [5, 1]]]},
    ),
}
MUTATING_COMMANDS = sorted(set(SAMPLES) - {"get_region_preview"})


def request(command, **changes):
    target, params = copy.deepcopy(SAMPLES[command])
    body = {
        "bridge_protocol": 1,
        "instance_id": "instance-a",
        "operation_id": "edit-1",
        "command": command,
        "target": target,
        "params": params,
    }
    body.update(changes)
    return body


def test_editing_module_import_is_independent_of_import_order():
    plugin = str(Path(__file__).resolve().parents[2] / "plugin")
    env = {**os.environ, "PYTHONPATH": plugin}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import krita6_bridge.editing_protocol; import krita6_bridge.protocol",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", sorted(SAMPLES))
def test_editing_contracts_are_strict_detached_and_classified(command):
    body = request(command)
    original = copy.deepcopy(body)
    normalized = validate_request(body, "instance-a")
    assert normalized["command"] == command
    assert (command in MUTATIONS) == (command != "get_region_preview")
    assert body == original
    normalized["target"]["new"] = "unrelated"
    normalized["params"]["new"] = "unrelated"
    assert body == original
    for field in ("target", "params"):
        invalid = request(command)
        invalid[field]["arbitrary_action"] = "trigger"
        with pytest.raises(BridgeError) as failure:
            validate_request(invalid, "instance-a")
        assert failure.value.code == "INVALID_REQUEST"
        assert failure.value.effect == "none"
    for key in original["target"]:
        missing = request(command)
        del missing["target"][key]
        with pytest.raises(BridgeError):
            validate_request(missing, "instance-a")


def test_editing_defaults_are_explicit_for_retry_hashing():
    transform = validate_request(request("transform_layer"), "instance-a")["params"]
    assert transform == {
        "pivot": [10.0, 20.0],
        "translate_x": 0.0,
        "translate_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation_degrees": 0.0,
    }
    preview = validate_request(request("get_region_preview"), "instance-a")["params"]
    assert preview["max_edge"] == 1024
    bezier = request("paint_bezier_path")
    normalized = validate_request(bezier, "instance-a")
    assert normalized["params"]["color"] == "#ABCDEF"
    normalized["params"]["segments"][0][0][0] = 99
    assert bezier["params"]["segments"][0][0][0] == 2


@pytest.mark.parametrize(
    "command,required",
    [
        ("get_region_preview", {"x", "y", "width", "height"}),
        ("copy_layer", {"destination_document_id", "name"}),
        ("transform_layer", {"pivot"}),
        ("open_document", {"root", "path"}),
        ("import_image_layer", {"root", "path", "name", "x", "y"}),
        ("set_selection", {"shape", "points"}),
        ("paint_bezier_path", {"preset_id", "size_px", "opacity", "color", "start", "segments"}),
    ],
)
def test_editing_required_fields_cannot_be_omitted(command, required):
    for field in required:
        body = request(command)
        del body["params"][field]
        with pytest.raises(BridgeError):
            validate_request(body, "instance-a")


@pytest.mark.parametrize("changes", [{"x": -1}, {"y": True}, {"x": 0.5}, {"y": 2**31}])
def test_import_requires_bounded_integer_placement(changes):
    body = request("import_image_layer")
    body["params"].update(changes)
    with pytest.raises(BridgeError):
        validate_request(body, "instance-a")


@pytest.mark.parametrize("max_edge", [31, 1025, True, 512.5])
def test_region_preview_has_bounded_integer_output_edge(max_edge):
    body = request("get_region_preview")
    body["params"]["max_edge"] = max_edge
    with pytest.raises(BridgeError):
        validate_request(body, "instance-a")


@pytest.mark.parametrize(
    "command,params",
    [
        ("activate_document", {"document_id": "doc-2"}),
        ("clear_selection", {"preserve": True}),
        ("set_layer_properties", {}),
        ("set_layer_properties", {"name": ""}),
        ("set_layer_properties", {"name": "x" * 129}),
        ("set_layer_properties", {"name": "bad\x00name"}),
        ("set_layer_properties", {"visible": 1}),
        ("set_layer_properties", {"opacity": True}),
        ("set_layer_properties", {"opacity": -0.1}),
        ("set_layer_properties", {"opacity": 1.01}),
        ("set_layer_properties", {"opacity": math.nan}),
        ("copy_layer", {"name": "Copy"}),
        ("copy_layer", {"destination_document_id": "doc-2"}),
        ("copy_layer", {"destination_document_id": "../doc-2", "name": "Copy"}),
        ("move_layer", {"destination_document_id": "doc-2"}),
        ("move_layer", {"above_node_id": None}),
        ("move_layer", {"parent_node_id": "../group"}),
        ("transform_layer", {}),
        ("transform_layer", {"pivot": [0]}),
        ("transform_layer", {"pivot": [32769, 0]}),
        ("transform_layer", {"pivot": [0, 0], "translate_y": -32769}),
        ("transform_layer", {"pivot": [0, 0], "scale_x": 0}),
        ("transform_layer", {"pivot": [0, 0], "scale_x": -1}),
        ("transform_layer", {"pivot": [0, 0], "scale_y": 16.01}),
        ("transform_layer", {"pivot": [0, 0], "rotation_degrees": 361}),
        ("open_document", {"root": "input", "path": ""}),
        ("open_document", {"root": "input", "path": "x" * 4097}),
        ("open_document", {"root": "input", "path": "bad\x00.kra"}),
        ("open_document", {"root": "../input", "path": "reference.kra"}),
        ("set_selection", {"shape": "ellipse"}),
        ("set_selection", {"shape": []}),
        ("set_selection", {"shape": "polygon", "points": [[0, 0], [1, 1]]}),
        ("set_selection", {"shape": "polygon", "points": [[0, 0]] * 257}),
        ("set_selection", {"shape": "polygon", "points": [[0, 0], [1.5, 1], [2, 0]]}),
        ("set_selection", {"shape": "polygon", "points": [[0, 0], [True, 1], [2, 0]]}),
        ("set_selection", {"shape": "rectangle", "points": [[0, 0], [1, 1], [2, 0]]}),
        ("set_selection", {"shape": "polygon", "points": [[0, 0]] * 3, "x": 0}),
    ],
)
def test_editing_rejects_invalid_controls(command, params):
    with pytest.raises(BridgeError) as failure:
        validate_request(request(command, params=params), "instance-a")
    assert failure.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("command", ["get_region_preview", "set_selection"])
@pytest.mark.parametrize(
    "changes",
    [
        {"x": -1},
        {"y": 2**31},
        {"x": True},
        {"y": 0.5},
        {"width": 0},
        {"height": 8193},
        {"width": 8192, "height": 8192},
    ],
)
def test_regions_have_integer_dimensions_and_area_bound(command, changes):
    params = {"x": 0, "y": 0, "width": 16, "height": 16, **changes}
    if command == "set_selection":
        params["shape"] = "rectangle"
    with pytest.raises(BridgeError):
        validate_request(request(command, params=params), "instance-a")


@pytest.mark.parametrize("bad", [True, math.nan, math.inf, -math.inf, 10**500])
def test_transform_and_bezier_reject_nonfinite_geometry(bad):
    for command, params in (
        ("transform_layer", {"pivot": [bad, 0]}),
        ("transform_layer", {"pivot": [0, 0], "scale_x": bad}),
        ("paint_bezier_path", {**BRUSH, "start": [0, bad], "segments": [[[1, 1]] * 3]}),
        ("paint_bezier_path", {**BRUSH, "start": [0, 0], "segments": [[[1, bad]] * 3]}),
    ):
        with pytest.raises(BridgeError):
            validate_request(request(command, params=params), "instance-a")


@pytest.mark.parametrize("segments", [[], [[[1, 1]] * 3] * 257, [[1, 1]], [[[1, 1]] * 4], "cubic"])
def test_bezier_requires_bounded_cubic_segments(segments):
    body = request("paint_bezier_path")
    body["params"]["segments"] = segments
    with pytest.raises(BridgeError):
        validate_request(body, "instance-a")


def test_unknown_editing_validator_command_is_not_a_noop():
    with pytest.raises(BridgeError):
        validate_editing("run_python", {})


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_each_new_mutation_preserves_queued_running_and_terminal_retry_identity(command):
    clock = [10.0]
    ledger = OperationLedger("instance-a", clock=lambda: clock[0], result_ttl=1)
    body = request(command)
    assert ledger.admit(body)["state"] == "queued"
    assert ledger.admit({**body, "queue_timeout_ms": 100})["state"] == "queued"
    assert ledger.status()["queued"] == ledger.status()["mutations"] == 1
    assert ledger.take_next()["command"] == command
    assert ledger.admit(body)["state"] == "running"
    assert ledger.admit(body)["effect"] == "unknown"
    ledger.finish("edit-1", result={"verified": True})
    assert ledger.admit(body)["result"] == {"verified": True}
    assert ledger.take_next() is None
    clock[0] += 2
    tombstone = ledger.admit(body)
    assert tombstone["state"] == "succeeded"
    assert tombstone["effect"] == "applied"
    assert tombstone["error"]["code"] == "RESULT_EXPIRED"
    assert ledger.take_next() is None
    conflict = request(command)
    if conflict["target"]:
        conflict["target"]["document_id"] = "doc-changed"
    else:
        conflict["params"]["path"] = "other.kra"
    with pytest.raises(BridgeError) as failure:
        ledger.admit(conflict)
    assert failure.value.code == "OPERATION_ID_CONFLICT"


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_each_new_mutation_cancelled_or_expired_before_dispatch_never_runs(command):
    clock = [10.0]
    ledger = OperationLedger("instance-a", clock=lambda: clock[0])
    cancelled = request(command, operation_id="cancelled")
    expired = request(command, operation_id="expired", queue_timeout_ms=10)
    ledger.admit(cancelled)
    ledger.admit(expired)
    assert ledger.cancel("cancelled")["state"] == "cancelled"
    assert ledger.admit(cancelled)["state"] == "cancelled"
    clock[0] += 0.02
    assert ledger.take_next() is None
    assert ledger.get("expired")["state"] == "expired"
    assert ledger.get("expired")["effect"] == "none"
    assert ledger.get("cancelled")["effect"] == "none"
    assert ledger.status()["bridge_sequence"] == 0


def test_explicit_transform_defaults_deduplicate_omitted_defaults():
    ledger = OperationLedger("instance-a")
    body = request("transform_layer")
    ledger.admit(body)
    explicit = validate_request(body, "instance-a")
    assert ledger.admit(explicit)["state"] == "queued"
    assert ledger.status()["mutations"] == 1


@pytest.mark.parametrize("command", ["create_file_layer", "set_file_layer"])
@pytest.mark.parametrize("scaling", ["ToImagePPI", "bogus", True, None, [], {}])
def test_file_layer_scaling_is_bounded(command, scaling):
    body = request(command)
    body["params"]["scaling_method"] = scaling
    with pytest.raises(BridgeError) as error:
        validate_request(body, "instance-a")
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("command", ["create_file_layer", "set_file_layer"])
def test_file_layer_default_scaling_has_same_retry_identity(command):
    ledger = OperationLedger("instance-a")
    body = request(command)
    ledger.admit(body)
    body["params"]["scaling_method"] = "None"
    assert ledger.admit(body)["state"] == "queued"
    body["params"]["scaling_method"] = "ToImageSize"
    with pytest.raises(BridgeError) as error:
        ledger.admit(body)
    assert error.value.code == "OPERATION_ID_CONFLICT"
