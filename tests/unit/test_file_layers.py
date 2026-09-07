"""Pre-dispatch file-layer guards; live compatibility is covered by probe_editing."""

from types import SimpleNamespace

import pytest

from krita6_bridge.editing import EditingMixin
from krita6_bridge.general_editing import GeneralEditingMixin
from krita6_bridge.protocol import BridgeError


class Host(EditingMixin, GeneralEditingMixin):
    pass


@pytest.mark.parametrize("command", ["create_file_layer", "set_file_layer"])
def test_invalid_source_never_reaches_native_mutation(tmp_path, command):
    host = Host()
    calls = []
    node = SimpleNamespace(setProperties=lambda *args: calls.append(args))
    document = SimpleNamespace(
        width=lambda: 64, height=lambda: 64, createFileLayer=lambda *args: calls.append(args)
    )
    host._editing_document = lambda _: document
    host._writable_color = lambda _: None
    host._require_layer_capacity = lambda _: None
    host._editing_parent = lambda *args: (None, None)
    host._structure_node = lambda *args, **kwargs: node
    host.input_roots = {"input": tmp_path}
    params = {"root": "input", "path": "../escape.png", "name": "Ref", "scaling_method": "None"}
    with pytest.raises(BridgeError) as error:
        getattr(host, "_" + command)({"document_id": "doc", "node_id": "node"}, params)
    assert error.value.code == "INVALID_PATH"
    assert error.value.effect == "none"
    assert not calls


@pytest.mark.parametrize("kind,locked", [("paintlayer", False), ("filelayer", True)])
def test_relink_rejects_wrong_type_or_locked_ancestor_before_read(kind, locked):
    host = Host()
    parent = SimpleNamespace(locked=lambda: locked, parentNode=lambda: None)
    node = SimpleNamespace(
        type=lambda: kind, animated=lambda: False, locked=lambda: False, parentNode=lambda: parent
    )
    doc = SimpleNamespace(width=lambda: 64, height=lambda: 64, rootNode=lambda: parent)
    host._editing_document = lambda _: doc
    host._writable_color = lambda _: None
    host._node = lambda *args: node
    with pytest.raises(BridgeError) as error:
        host._set_file_layer({"document_id": "doc", "node_id": "node"}, {})
    assert error.value.code == ("TARGET_LOCKED" if locked else "INVALID_TARGET_TYPE")
