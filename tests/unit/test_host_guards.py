"""Pure guard/executor checks; these deliberately establish no Krita compatibility."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from krita6_bridge.operations import OperationLedger
from krita6_bridge.protocol import BridgeError, validate_request


@pytest.fixture
def host_modules(monkeypatch):
    """Load host helpers with inert imports without installing Qt into test Python."""
    core = types.ModuleType("PyQt6.QtCore")
    for name in (
        "QBuffer",
        "QByteArray",
        "QIODevice",
        "QPoint",
        "QPointF",
        "QThread",
        "QUuid",
        "QObject",
        "QTimer",
    ):
        setattr(core, name, type(name, (), {}))
    core.PYQT_VERSION_STR = core.QT_VERSION_STR = "test-stub"
    gui = types.ModuleType("PyQt6.QtGui")
    gui.QColor = gui.QPainterPath = object
    widgets = types.ModuleType("PyQt6.QtWidgets")
    widgets.QApplication = object
    native = types.ModuleType("krita")
    native.InfoObject = native.Krita = native.ManagedColor = native.Preset = object
    for name, value in {
        "PyQt6": types.ModuleType("PyQt6"),
        "PyQt6.QtCore": core,
        "PyQt6.QtGui": gui,
        "PyQt6.QtWidgets": widgets,
        "krita": native,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    modules = []
    root = Path(__file__).parents[2] / "plugin" / "krita6_bridge"
    for name in ("host", "executor"):
        spec = importlib.util.spec_from_file_location(
            "krita6_bridge." + name, root / (name + ".py")
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


@pytest.mark.parametrize(
    "relative", ["../outside.kra", "/tmp/outside.kra", "wrong.png", "folder/../../outside.kra"]
)
def test_output_path_rejects_escaping_and_wrong_format(host_modules, tmp_path, relative):
    host, _ = host_modules
    with pytest.raises(BridgeError) as error:
        host.resolve_output_path({"scratch": tmp_path}, "scratch", relative, ".kra", False)
    assert error.value.code == "INVALID_PATH"


def test_output_path_requires_overwrite_and_rejects_symlink_escape(host_modules, tmp_path):
    host, _ = host_modules
    root = tmp_path / "root"
    root.mkdir()
    output = root / "drawing.kra"
    output.write_bytes(b"existing")
    with pytest.raises(BridgeError) as error:
        host.resolve_output_path({"scratch": root}, "scratch", "drawing.kra", ".kra", False)
    assert error.value.code == "FILE_EXISTS"
    assert (
        host.resolve_output_path({"scratch": root}, "scratch", "drawing.kra", ".kra", True)
        == output
    )
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BridgeError) as error:
        host.resolve_output_path({"scratch": root}, "scratch", "escape/drawing.kra", ".kra", True)
    assert error.value.code == "INVALID_PATH"
    assert output.read_bytes() == b"existing"
    (root / "misleading.kra").symlink_to(root / "wrong.txt")
    with pytest.raises(BridgeError) as error:
        host.resolve_output_path({"scratch": root}, "scratch", "misleading.kra", ".kra", True)
    assert error.value.code == "INVALID_PATH"


def test_output_path_rejects_directory_and_missing_parent(host_modules, tmp_path):
    host, _ = host_modules
    (tmp_path / "directory.kra").mkdir()
    for relative in ("directory.kra", "missing/output.kra"):
        with pytest.raises(BridgeError) as error:
            host.resolve_output_path({"scratch": tmp_path}, "scratch", relative, ".kra", True)
        assert error.value.code == "INVALID_PATH"


def test_pending_retains_gate_while_busy_and_resolves_fresh_document(host_modules):
    host, _ = host_modules
    calls = []

    class FakeHost:
        ready = False

        def _assert_gui_thread(self):
            pass

        def _document(self, document_id):
            calls.append(document_id)
            return object()

        def _is_ready(self, document):
            return self.ready

        def _completed(self, result, mutation):
            assert mutation
            return result

    fake = FakeHost()
    pending = host.Pending(fake, "doc-one", {"ok": True})
    assert pending.poll() is None
    assert pending.poll() is None
    fake.ready = True
    assert pending.poll() == {"ok": True}
    assert calls == ["doc-one"] * 3


def test_pending_closed_document_reports_unknown_effect(host_modules):
    host, _ = host_modules

    class FakeHost:
        def _assert_gui_thread(self):
            pass

        def _document(self, document_id):
            raise BridgeError("TARGET_NOT_FOUND", "closed")

    with pytest.raises(BridgeError) as error:
        host.Pending(FakeHost(), "doc-one", {}).poll()
    assert error.value.code == "OUTCOME_UNKNOWN"
    assert error.value.effect == "unknown"


@pytest.mark.parametrize("result", [{"wrapper": object()}, {"large": "x" * (1024 * 1024)}])
def test_executor_records_invalid_result_instead_of_stranding_operation(host_modules, result):
    _, executor_module = host_modules
    ledger = OperationLedger("instance-one")
    request = validate_request(
        {
            "bridge_protocol": 1,
            "instance_id": "instance-one",
            "operation_id": "create-one",
            "command": "create_document",
            "params": {"width": 16, "height": 16, "name": "scratch"},
        },
        "instance-one",
    )
    ledger.admit(request)
    running = ledger.take_next()
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.ledger = ledger
    executor._current = running
    executor._pending = object()
    executor._finish(result=result)
    snapshot = ledger.get("create-one")
    assert snapshot["state"] == "failed"
    assert snapshot["effect"] == "unknown"
    assert snapshot["error"]["code"] == "HOST_RESULT_INVALID"
    assert executor._current is executor._pending is None
    assert ledger.is_idle()


def test_bridge_preset_changes_preserve_handle_but_external_changes_fail(host_modules):
    host_module, _ = host_modules
    resource = types.SimpleNamespace(signature="initial")
    resources = {"bundled-brush": resource}
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host.app = types.SimpleNamespace(resources=lambda kind: resources)
    host._presets = {"preset-one": ("bundled-brush", "initial")}
    host._preset_signature = lambda value: (value.signature, '<Preset paintopid="paintbrush"/>')
    assert host._preset("preset-one") is resource

    # Changing brush controls and restoring the originating view mutates Krita's
    # working preset XML synchronously, while the source resource stays the same.
    resource.signature = "bridge-settings"
    host._refresh_preset_after_bridge_change("preset-one", resource)
    assert host._preset("preset-one") is resource

    resource.signature = "later-external-edit"
    with pytest.raises(BridgeError) as error:
        host._preset("preset-one")
    assert error.value.code == "RESOURCE_CHANGED"

    resources["bundled-brush"] = types.SimpleNamespace(signature="replacement")
    host._refresh_preset_after_bridge_change("preset-one", resource)
    assert host._presets["preset-one"] == ("bundled-brush", "bridge-settings")
    with pytest.raises(BridgeError) as error:
        host._preset("preset-one")
    assert error.value.code == "RESOURCE_CHANGED"


def test_create_document_enforces_open_document_limit_before_native_creation(host_modules):
    host_module, _ = host_modules
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host.app = types.SimpleNamespace(documents=lambda: [object()] * host_module.MAX_OPEN_DOCUMENTS)
    with pytest.raises(BridgeError) as error:
        host._create_document({}, {"width": 16, "height": 16, "name": "scratch"})
    assert error.value.code == "DOCUMENT_LIMIT"


def test_create_layer_counts_nested_nodes_before_native_creation(host_modules):
    host_module, _ = host_modules
    leaf = types.SimpleNamespace(childNodes=lambda: [])
    group = types.SimpleNamespace(childNodes=lambda: [leaf] * (host_module.MAX_LAYER_NODES - 1))
    document = types.SimpleNamespace(topLevelNodes=lambda: [group])
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._document = lambda handle: document
    host._require_ready = lambda document: None
    with pytest.raises(BridgeError) as error:
        host._create_paint_layer({"document_id": "doc-one"}, {"name": "scratch"})
    assert error.value.code == "LAYER_LIMIT"
    group.childNodes = lambda: [leaf] * (host_module.MAX_LAYER_NODES - 2)
    host._require_layer_capacity(document)


@pytest.mark.parametrize("failure_phase", ["native", "restore"])
def test_post_dispatch_failure_retains_gate_until_native_completion(
    host_modules, monkeypatch, failure_phase
):
    host_module, executor_module = host_modules
    preset = object()
    document = types.SimpleNamespace(
        ready=False, activeNode=lambda: None, setActiveNode=lambda node: None
    )
    node = types.SimpleNamespace(paintAbility=lambda: "PAINT")
    getters = {
        "currentBrushPreset": preset,
        "eraserMode": False,
        "globalAlphaLock": False,
        "disablePressure": False,
        "currentBlendingMode": "normal",
        "paintingFlow": 1.0,
        "brushRotation": 0.0,
        "brushSize": 5.0,
        "paintingOpacity": 1.0,
    }
    view = types.SimpleNamespace(
        **{key: lambda value=value: value for key, value in getters.items()}
    )
    for setter in (
        "setCurrentBrushPreset",
        "setBrushSize",
        "setPaintingOpacity",
        "setPaintingFlow",
        "setBrushRotation",
        "setCurrentBlendingMode",
        "setEraserMode",
        "setGlobalAlphaLock",
        "setDisablePressure",
        "setForeGroundColor",
    ):
        setattr(view, setter, lambda value: None)
    monkeypatch.setattr(host_module, "QColor", lambda value: value)
    monkeypatch.setattr(
        host_module,
        "ManagedColor",
        types.SimpleNamespace(
            fromQColor=lambda color: types.SimpleNamespace(setColorSpace=lambda *args: True)
        ),
    )
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._paint_targets = lambda *args: (document, node, view, preset)
    host._view_snapshot = lambda view: {}
    host._refresh_preset_after_bridge_change = lambda *args: None
    host._assert_gui_thread = lambda: None
    host._document = lambda handle: document
    host._is_ready = lambda document: document.ready

    def restore(view, snapshot):
        if failure_phase == "restore":
            raise RuntimeError("injected restoration failure")

    calls = []

    def native_call(node):
        calls.append(node)
        if failure_phase == "native":
            raise RuntimeError("injected exception after native dispatch")

    host._restore_view = restore
    params = {
        "preset_id": "preset-one",
        "size_px": 5.0,
        "opacity": 1.0,
        "color": "#ff0000",
        "start": [1, 1],
        "end": [5, 5],
    }
    target = {"document_id": "doc-one", "node_id": "node-one"}
    pending = host._paint(target, params, [params["start"], params["end"]], native_call)
    assert isinstance(pending, host_module.Pending)
    assert calls == [node]

    ledger = OperationLedger("instance-one")
    ledger.admit(
        validate_request(
            {
                "bridge_protocol": 1,
                "instance_id": "instance-one",
                "operation_id": "paint-one",
                "command": "paint_line",
                "target": target,
                "params": params,
            },
            "instance-one",
        )
    )
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host, executor.ledger = host, ledger
    executor._current, executor._pending = ledger.take_next(), pending
    executor._disposed, executor._draining = False, True
    ledger.drain()
    executor.tick()
    assert ledger.get("paint-one")["state"] == "running"
    assert not ledger.is_idle()
    assert executor._pending is pending
    document.ready = True
    executor.tick()
    outcome = ledger.get("paint-one")
    assert outcome["state"] == "failed"
    assert outcome["effect"] == "unknown"
    assert outcome["error"]["code"] == (
        "NATIVE_PAINT_FAILED" if failure_phase == "native" else "RESTORE_FAILED"
    )
    assert ledger.is_idle()


def test_executor_disposal_disconnects_qt_and_releases_cached_state(host_modules):
    _, executor_module = host_modules
    events = []
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host = types.SimpleNamespace(_assert_gui_thread=lambda: events.append("gui"))
    executor.ledger = object()
    executor.on_drained = object()
    executor._current = executor._pending = None
    executor._disposed = False
    executor.timer = types.SimpleNamespace(
        stop=lambda: events.append("stop"),
        timeout=types.SimpleNamespace(disconnect=lambda callback: events.append("disconnect")),
    )
    executor.deleteLater = lambda: events.append("deleteLater")
    executor.dispose()
    assert events == ["gui", "stop", "disconnect", "deleteLater"]
    assert executor.host is executor.ledger is executor.on_drained is None
    executor.dispose()
    assert len(events) == 4
    executor.tick()


def test_executor_disposal_cannot_release_native_gate_early(host_modules):
    _, executor_module = host_modules
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host = types.SimpleNamespace(_assert_gui_thread=lambda: None)
    executor._disposed = False
    executor._current, executor._pending = object(), object()
    with pytest.raises(RuntimeError, match="dispatched native work"):
        executor.dispose()


@pytest.mark.parametrize("finish_method", ["_startup_failed", "_check_stopped"])
def test_extension_disposes_executor_on_failed_start_and_completed_stop(
    host_modules, monkeypatch, finish_method
):
    monkeypatch.setattr(sys.modules["krita"], "Extension", object, raising=False)
    monkeypatch.setattr(sys.modules["PyQt6.QtWidgets"], "QMessageBox", object, raising=False)
    path = Path(__file__).parents[2] / "plugin" / "krita6_bridge" / "extension.py"
    spec = importlib.util.spec_from_file_location("krita6_bridge._extension_guard_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []
    extension = module.KritaBridgeExtension.__new__(module.KritaBridgeExtension)
    extension.executor = types.SimpleNamespace(dispose=lambda: events.append("dispose"))
    extension.ledger = types.SimpleNamespace(drain=lambda: events.append("drain"))
    extension.host = object()
    extension.server = None
    extension._shutdown_thread = None
    getattr(extension, finish_method)()
    assert events == (["drain", "dispose"] if finish_method == "_startup_failed" else ["dispose"])
    assert extension.executor is extension.ledger is extension.host is extension.server is None
    assert extension._state == "stopped"
