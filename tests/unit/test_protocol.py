import copy
import math

import pytest

from krita6_bridge.protocol import BridgeError, COMMANDS, MUTATIONS, validate_request


def request(command="list_documents", **changes):
    value = {
        "bridge_protocol": 1,
        "instance_id": "instance-a",
        "operation_id": "op-1",
        "command": command,
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"bridge_protocol": True}, "PROTOCOL_MISMATCH"),
        ({"bridge_protocol": 2}, "PROTOCOL_MISMATCH"),
        ({"instance_id": "instance-old"}, "INSTANCE_MISMATCH"),
        ({"operation_id": "../escape"}, "INVALID_REQUEST"),
        ({"operation_id": "x" * 129}, "INVALID_REQUEST"),
        ({"command": "run_python"}, "INVALID_REQUEST"),
        ({"command": []}, "INVALID_REQUEST"),
        ({"target": {"extra": 1}}, "INVALID_REQUEST"),
        ({"params": {"code": "print(1)"}}, "INVALID_REQUEST"),
        ({"queue_timeout_ms": True}, "INVALID_REQUEST"),
        ({"queue_timeout_ms": 0}, "INVALID_REQUEST"),
        ({"queue_timeout_ms": 120001}, "INVALID_REQUEST"),
        ({"unknown": True}, "INVALID_REQUEST"),
    ],
)
def test_rejects_ambiguous_or_unsupported_requests(changes, code):
    with pytest.raises(BridgeError) as failure:
        validate_request(request(**changes), "instance-a")
    assert failure.value.code == code
    assert failure.value.effect == "none"


def test_defaults_and_detached_normalization():
    body = request(
        "paint_path",
        target={"document_id": "doc-1", "node_id": "node-1"},
        params={
            "preset_id": "preset-1",
            "size_px": 5,
            "opacity": 1,
            "color": "#ab12cd",
            "points": [[1, 2], [3, 4]],
        },
    )
    original = copy.deepcopy(body)
    normalized = validate_request(body, "instance-a")
    assert normalized["queue_timeout_ms"] == 10000
    assert normalized["params"]["color"] == "#AB12CD"
    normalized["params"]["points"][0][0] = 200
    assert body == original


@pytest.mark.parametrize("bad", [True, math.inf, -math.inf, math.nan, 10**500])
def test_finite_numeric_coordinates(bad):
    body = request(
        "paint_path",
        target={"document_id": "doc-1", "node_id": "node-1"},
        params={
            "preset_id": "preset-1",
            "size_px": 5,
            "opacity": 1,
            "color": "#AB12CD",
            "points": [[1, 2], [bad, 4]],
        },
    )
    with pytest.raises(BridgeError, match="finite numeric"):
        validate_request(body, "instance-a")


@pytest.mark.parametrize(
    "params",
    [
        {"width": 8192, "height": 8192, "name": "too big"},
        {"width": True, "height": 10, "name": "boolean"},
        {"width": 32, "height": 32, "name": ""},
        {"width": 32, "height": 32, "name": "a\x00b"},
    ],
)
def test_document_bounds(params):
    with pytest.raises(BridgeError):
        validate_request(request("create_document", params=params), "instance-a")


def test_catalog_samples_cover_every_command():
    doc = {"document_id": "doc-1"}
    brush = {"preset_id": "brush-1", "size_px": 1, "opacity": 1, "color": "#AABBCC"}
    samples = {
        "list_documents": ({}, {}),
        "inspect_document": (doc, {}),
        "get_preview": (doc, {}),
        "list_brush_presets": ({}, {}),
        "diffusion_status": ({}, {}),
        "inspect_diffusion_document": (doc, {}),
        "list_diffusion_jobs": (doc, {}),
        "list_diffusion_styles": ({}, {}),
        "generate_diffusion": (doc, {"positive_prompt": "A green tree"}),
        "get_diffusion_generation": (doc, {"generation_id": "generate-1"}),
        "get_diffusion_result": (doc, {"generation_id": "generate-1", "result_id": "image-1"}),
        "apply_diffusion_result": (doc, {"generation_id": "generate-1", "result_id": "image-1"}),
        "create_document": ({}, {"width": 4096, "height": 4096, "name": "Scratch"}),
        "create_paint_layer": (doc, {"name": "Paint"}),
        "paint_path": ({**doc, "node_id": "node-1"}, {**brush, "points": [[1, 1], [2, 2]]}),
        "paint_line": ({**doc, "node_id": "node-1"}, {**brush, "start": [1, 1], "end": [2, 2]}),
        "save_document": (doc, {"root": "output", "path": "scratch.kra"}),
        "export_png": (doc, {"root": "output", "path": "scratch.png"}),
    }
    assert set(samples) == COMMANDS
    assert len(MUTATIONS) == 8
    for command, (target, params) in samples.items():
        normalized = validate_request(request(command, target=target, params=params), "instance-a")
        assert normalized["command"] == command


def test_line_requires_integer_coordinates_and_bounded_pressure():
    body = request(
        "paint_line",
        target={"document_id": "doc-1", "node_id": "node-1"},
        params={
            "preset_id": "preset-1",
            "size_px": 5,
            "opacity": 1,
            "color": "#AB12CD",
            "start": [1, 2],
            "end": [3, 4],
        },
    )
    assert validate_request(body, "instance-a")["params"]["pressure_start"] == 1.0
    body["params"]["end"][0] = 3.5
    with pytest.raises(BridgeError):
        validate_request(body, "instance-a")
    body["params"]["end"][0] = 3
    body["params"]["pressure_end"] = 1.01
    with pytest.raises(BridgeError):
        validate_request(body, "instance-a")


@pytest.mark.parametrize("params", [{"max_edge": 1025}, {"max_edge": 31}, {"max_edge": True}])
def test_preview_bounds(params):
    with pytest.raises(BridgeError):
        validate_request(
            request("get_preview", target={"document_id": "doc-1"}, params=params), "instance-a"
        )


@pytest.mark.parametrize(
    "command,target,expected_params",
    [
        ("diffusion_status", {}, {}),
        ("inspect_diffusion_document", {"document_id": "doc-1"}, {}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"offset": 0, "limit": 50}),
    ],
)
def test_diffusion_reads_have_bounded_defaults_and_are_not_mutations(
    command, target, expected_params
):
    result = validate_request(request(command, target=target), "instance-a")
    assert result["target"] == target
    assert result["params"] == expected_params
    assert command not in MUTATIONS


@pytest.mark.parametrize(
    "command,target,params",
    [
        ("diffusion_status", {"document_id": "doc-1"}, {}),
        ("diffusion_status", {}, {"connect": True}),
        ("inspect_diffusion_document", {}, {}),
        ("inspect_diffusion_document", {"document_id": "doc-1"}, {"prompt": "new image"}),
        ("list_diffusion_jobs", {}, {}),
        ("list_diffusion_jobs", {"document_id": "doc-1", "node_id": "node-1"}, {}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"cancel": True}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"offset": True}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"offset": -1}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"offset": 2**31}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"limit": True}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"limit": 0}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"limit": 101}),
        ("list_diffusion_jobs", {"document_id": "doc-1"}, {"limit": 5.0}),
    ],
)
def test_diffusion_reads_reject_mutating_arguments_and_invalid_pagination(command, target, params):
    with pytest.raises(BridgeError) as error:
        validate_request(request(command, target=target, params=params), "instance-a")
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.effect == "none"


def test_diffusion_job_page_accepts_exact_upper_bounds():
    params = {"offset": 2**31 - 1, "limit": 100}
    normalized = validate_request(
        request("list_diffusion_jobs", target={"document_id": "doc-1"}, params=params), "instance-a"
    )
    assert normalized["params"] == params


@pytest.mark.parametrize(
    "changes",
    [
        {"positive_prompt": ""},
        {"positive_prompt": "x" * 4097},
        {"positive_prompt": "bad\x00prompt"},
        {"negative_prompt": "x" * 4097},
        {"strength": 0},
        {"strength": 1.01},
        {"strength": math.nan},
        {"strength": True},
        {"seed": -1},
        {"seed": 2**32},
        {"seed": True},
        {"seed": 1.5},
        {"style_id": "../style.json"},
        {"workflow": {}},
        {"batch_count": 2},
        {"server_url": "https://example.com"},
        {"apply": True},
    ],
)
def test_generation_rejects_unbounded_or_ambient_controls(changes):
    with pytest.raises(BridgeError) as error:
        validate_request(
            request(
                "generate_diffusion",
                target={"document_id": "doc-1"},
                params={"positive_prompt": "A green tree", **changes},
            ),
            "instance-a",
        )
    assert error.value.code == "INVALID_REQUEST"


def test_generation_defaults_are_part_of_retry_identity():
    body = request(
        "generate_diffusion",
        target={"document_id": "doc-1"},
        params={"positive_prompt": "A green tree"},
    )
    normalized = validate_request(body, "instance-a")
    assert normalized["params"] == {
        "positive_prompt": "A green tree",
        "negative_prompt": "",
        "strength": 1.0,
        "seed": 0,
    }
    assert "generate_diffusion" in MUTATIONS
    assert "apply_diffusion_result" in MUTATIONS


@pytest.mark.parametrize(
    "command,params",
    [
        ("get_diffusion_generation", {}),
        ("get_diffusion_generation", {"generation_id": "gen-1", "cancel": True}),
        ("get_diffusion_result", {"generation_id": "gen-1"}),
        ("apply_diffusion_result", {"generation_id": "gen-1", "index": 0}),
        (
            "apply_diffusion_result",
            {"generation_id": "gen-1", "result_id": "img-1", "replace": True},
        ),
        (
            "get_diffusion_result",
            {"generation_id": "gen-1", "result_id": "img-1", "max_edge": 1025},
        ),
    ],
)
def test_generation_handles_and_result_policy_are_explicit(command, params):
    with pytest.raises(BridgeError):
        validate_request(
            request(command, target={"document_id": "doc-1"}, params=params), "instance-a"
        )
