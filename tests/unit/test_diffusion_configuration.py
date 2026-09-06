"""Configuration validation, retry safety, and partial-failure semantics."""

from types import SimpleNamespace

import pytest

from krita6_bridge.diffusion_configuration import DiffusionConfigurator
from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError, validate_request


def request(command, params):
    return dict(
        bridge_protocol=1,
        instance_id="test",
        operation_id="configuration-1",
        command=command,
        target={"document_id": "document-1"},
        params=params,
    )


@pytest.mark.parametrize(
    "command,params",
    [
        ("configure_diffusion", {}),
        ("configure_diffusion", {"seed": True}),
        ("configure_diffusion", {"fixed_seed": 1}),
        ("configure_diffusion", {"batch_count": 17}),
        ("configure_diffusion", {"strength": float("nan")}),
        ("configure_diffusion", {"resolution_multiplier": 3}),
        ("configure_diffusion", {"positive_prompt": "x" * 4097}),
        ("configure_diffusion", {"workspace": "live"}),
        ("configure_diffusion", {"inpaint_mode": "execute"}),
        ("set_diffusion_region", {"node_id": "layer"}),
        ("set_diffusion_region", {"node_id": "layer", "remove": True, "positive_prompt": "x"}),
        ("set_diffusion_controls", {"controls": [{}] * 65}),
        ("set_diffusion_controls", {"controls": [{"node_id": "layer", "mode": "unknown"}]}),
        (
            "set_diffusion_controls",
            {"controls": [{"node_id": "layer", "mode": "pose", "strength": 0.333}]},
        ),
        (
            "set_diffusion_controls",
            {"controls": [{"node_id": "layer", "mode": "pose", "start": 0.9, "end": 0.1}]},
        ),
        (
            "set_diffusion_controls",
            {"controls": [{"node_id": "layer", "mode": "pose", "python": "x"}]},
        ),
    ],
)
def test_rejects_unbounded_or_ambiguous_configuration(command, params):
    with pytest.raises(BridgeError) as error:
        validate_request(request(command, params), "test")
    assert error.value.code == "INVALID_REQUEST"
    assert error.value.effect == "none"


@pytest.mark.parametrize(
    "command,params",
    [
        ("configure_diffusion", {"seed": 42}),
        ("set_diffusion_region", {"node_id": "layer", "positive_prompt": "subject"}),
        ("set_diffusion_controls", {"controls": [{"node_id": "layer", "mode": "pose"}]}),
    ],
)
def test_configuration_deduplication_and_cancellation(command, params):
    body = request(command, params)
    ledger = OperationLedger("test")
    ledger.admit(body)
    assert ledger.cancel(body["operation_id"])["state"] == "cancelled"
    assert ledger.take_next() is None
    assert ledger.admit(body)["state"] == "cancelled"
    body["operation_id"] = "configuration-2"
    ledger.admit(body)
    assert ledger.take_next()["command"] == command
    ledger.finish(body["operation_id"], result={"configured": True})
    assert ledger.admit(body)["state"] == "succeeded"
    assert ledger.take_next() is None
    changed = (
        {**body, "params": {**params, "seed": 24}}
        if command == "configure_diffusion"
        else {**body, "target": {"document_id": "other"}}
    )
    with pytest.raises(BridgeError) as error:
        ledger.admit(changed)
    assert error.value.code == "OPERATION_ID_CONFLICT"


def test_settings_failure_reports_partial_without_leaking_exception():
    owner = SimpleNamespace(seed=1)

    class Broken:
        @property
        def fixed_seed(self):
            return False

        @fixed_seed.setter
        def fixed_seed(self, value):
            raise RuntimeError("secret path or backend URL")

    changes = [(owner, "seed", 42), (Broken(), "fixed_seed", True)]
    with pytest.raises(BridgeError) as error:
        DiffusionConfigurator._apply("doc", lambda: DiffusionConfigurator._settings(changes))
    assert owner.seed == 42
    assert error.value.effect == "partial"
    assert "secret" not in error.value.message


def test_ambiguous_region_identity_rejected():
    layer = SimpleNamespace(id=SimpleNamespace(toString=lambda: "{layer}"))
    model = SimpleNamespace(
        regions=[SimpleNamespace(layer_ids="{layer}"), SimpleNamespace(layer_ids="{layer}")]
    )
    with pytest.raises(BridgeError) as error:
        DiffusionConfigurator._region(model, layer)
    assert error.value.code == "DIFFUSION_REGION_AMBIGUOUS"


class RegionRoot:
    def __init__(self, controls=()):
        self.control = list(controls)
        self.positive = "original"

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


def configurator(model):
    generator = SimpleNamespace(
        _model=lambda doc: (None, model),
        _generation_guard=lambda *args: None,
    )
    return DiffusionConfigurator(generator)


def test_invalid_later_control_target_preserves_existing_list(monkeypatch):
    old = SimpleNamespace(has_active_job=False)
    model = SimpleNamespace(
        regions=RegionRoot([old]), layers=SimpleNamespace(updated=lambda: SimpleNamespace(all=[]))
    )
    controller = configurator(model)
    calls = []

    def layer(model, identifier):
        calls.append(identifier)
        if identifier == "missing":
            raise BridgeError("NODE_NOT_FOUND", "No layer")
        return object()

    monkeypatch.setattr(controller, "_layer", layer)
    with pytest.raises(BridgeError) as error:
        controller.configure(
            "set_diffusion_controls",
            "doc",
            object(),
            {
                "controls": [
                    {"node_id": "valid", "mode": "pose"},
                    {"node_id": "missing", "mode": "pose"},
                ]
            },
        )
    assert error.value.effect == "none"
    assert calls == ["valid", "missing"]
    assert model.regions.control == [old]


def test_active_preprocessor_rejects_clear():
    old = SimpleNamespace(has_active_job=True)
    model = SimpleNamespace(
        regions=RegionRoot([old]), layers=SimpleNamespace(updated=lambda: SimpleNamespace(all=[]))
    )
    with pytest.raises(BridgeError) as error:
        configurator(model).configure("set_diffusion_controls", "doc", object(), {"controls": []})
    assert error.value.code == "DOCUMENT_BUSY"
    assert model.regions.control == [old]


def test_invalid_style_does_not_apply_earlier_settings():
    model = SimpleNamespace(
        regions=RegionRoot(), layers=SimpleNamespace(updated=lambda: SimpleNamespace(all=[]))
    )
    controller = configurator(model)
    controller.generator._context = lambda **kwargs: (None, [])
    controller.generator._styles = lambda root: []
    with pytest.raises(BridgeError) as error:
        controller.configure(
            "configure_diffusion",
            "doc",
            object(),
            {"positive_prompt": "changed", "style_id": "missing"},
        )
    assert error.value.code == "DIFFUSION_STYLE_NOT_FOUND"
    assert model.regions.positive == "original"
