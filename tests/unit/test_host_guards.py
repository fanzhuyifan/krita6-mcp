"""Pure guard/executor checks; these deliberately establish no Krita compatibility."""

import importlib.util
import sys
import types
import weakref
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


@pytest.mark.parametrize(
    "completion, pending_error",
    [
        ("partial", True),
        ("closed", True),
        ("unexpected", True),
        ("closed", False),
        ("unexpected", False),
    ],
)
def test_executor_preserves_pending_recovery_handles_with_terminal_error(
    host_modules, completion, pending_error
):
    host_module, executor_module = host_modules
    ledger = OperationLedger("instance-one")
    request = validate_request(
        {
            "bridge_protocol": 1,
            "instance_id": "instance-one",
            "operation_id": "open-once",
            "command": "open_document" if pending_error else "apply_diffusion_result",
            "target": {} if pending_error else {"document_id": "opened-document"},
            "params": {"root": "scratch", "path": "reference.kra"}
            if pending_error
            else {
                "generation_id": "generation-one",
                "result_id": "result-one",
            },
        },
        "instance-one",
    )
    ledger.admit(request)
    recovery_handles = {
        "document_id": "opened-document",
        "node_id": "created-node",
        "source_document_id": "source-document",
        "source_node_id": "source-node",
        "generation_id": "generation-one",
        "result_id": "result-one",
        "new_node_ids": ["generated-node-one", "generated-node-two"],
    }
    known_result = {
        **recovery_handles,
        "native_strokes": 1,
        "settings_restored": True,
        "active_view": True,
        "width": 128,
        "application": "new_layer_on_top",
        "effect": "applied",
    }
    document = object()
    dispatched = []

    class FakeHost:
        completed = False

        def _assert_gui_thread(self):
            pass

        def _document(self, document_id):
            assert document_id == "opened-document"
            if self.completed and completion == "closed":
                raise BridgeError("TARGET_NOT_FOUND", "Document closed")
            return document

        def _is_ready(self, current):
            assert current is document
            if self.completed and completion == "unexpected":
                raise RuntimeError("Private native exception details")
            return self.completed

        def execute(self, current):
            dispatched.append(current)
            return host_module.Pending(
                self,
                "opened-document",
                known_result,
                error=BridgeError("OPEN_FAILED", "The view could not be initialized.", "partial")
                if pending_error
                else None,
            )

    host = FakeHost()
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host, executor.ledger = host, ledger
    executor._current = executor._pending = None
    executor._disposed = executor._draining = False
    executor.tick()
    executor.tick()
    running = ledger.get("open-once")
    assert running["state"] == "running"
    assert running["result"] is None
    assert len(dispatched) == 1
    assert not ledger.is_idle()

    host.completed = True
    executor.tick()
    outcome = ledger.get("open-once")
    assert outcome["state"] == "failed"
    assert outcome["result"] == recovery_handles
    assert outcome["effect"] == ("partial" if completion == "partial" else "unknown")
    assert (
        outcome["error"]["code"]
        == {
            "partial": "OPEN_FAILED",
            "closed": "OUTCOME_UNKNOWN",
            "unexpected": "HOST_ERROR",
        }[completion]
    )
    assert "Private native" not in str(outcome)
    assert executor._current is executor._pending is None
    assert ledger.is_idle()
    # The ledger retains a plain-data copy for reconciliation and duplicate IDs.
    known_result["document_id"] = "later-change"
    known_result["new_node_ids"].append("later-node")
    assert ledger.admit(request) == outcome
    assert ledger.take_next() is None
    assert len(dispatched) == 1


def test_diffusion_read_results_report_current_generation_availability(host_modules):
    host_module, _ = host_modules
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._document = lambda identifier: object()
    host._diffusion = types.SimpleNamespace(
        status=lambda: {"generation_control": False},
        inspect_document=lambda *args: {"generation_control": False, "document_status": "tracked"},
        list_jobs=lambda *args: {"generation_control": False, "jobs": []},
    )
    host._diffusion_generator = types.SimpleNamespace(
        capabilities=lambda: {"generation_control": True, "read_only": False}
    )
    for read in (
        host._diffusion_status,
        host._inspect_diffusion_document,
        host._list_diffusion_jobs,
    ):
        assert read({"document_id": "doc-one"}, {})["generation_control"] is True


@pytest.mark.parametrize("command", ["generate", "apply_result"])
@pytest.mark.parametrize("effect", ["none", "partial", "unknown"])
def test_diffusion_native_errors_keep_gate_after_possible_canvas_work(
    host_modules, command, effect
):
    host_module, _ = host_modules
    document = types.SimpleNamespace(ready=False, rootNode=lambda: None)
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._assert_gui_thread = lambda: None
    host._diffusion_target = host._document = lambda handle: document
    host._require_layer_capacity = host._unlocked_ancestry = lambda value: None
    host._is_ready = lambda value: value.ready

    def fail(*args):
        raise BridgeError("DIFFUSION_TEST_ERROR", "Injected failure", effect)

    host._diffusion_generator = types.SimpleNamespace(**{command: fail})
    target = {"document_id": "doc-one"}

    def dispatch():
        if command == "generate":
            return host._generate_diffusion(target, {"positive_prompt": "Tree"}, "generate-one")
        return host._apply_diffusion_result(
            target,
            {
                "generation_id": "generate-one",
                "result_id": "result-one",
            },
        )

    if effect == "none" and command == "apply_result":
        with pytest.raises(BridgeError):
            dispatch()
    else:
        pending = dispatch()
        assert isinstance(pending, host_module.Pending)
        assert pending.poll() is None
        document.ready = True
        with pytest.raises(BridgeError) as error:
            pending.poll()
        assert error.value.effect == effect


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


@pytest.mark.parametrize(
    "command, failure_phase",
    [
        (command, phase)
        for command in ("create_document", "open_document")
        for phase in (
            "add_view_none",
            "add_view_exception",
            "show_view",
            "metadata",
            "metadata_bridge_error",
            "batch_restore",
        )
        if command == "open_document" or phase != "batch_restore"
    ],
)
def test_native_document_failure_preserves_owner_handle_and_completion_gate(
    host_modules, monkeypatch, command, failure_phase
):
    host_module, executor_module = host_modules
    natives, owner_refs, calls = [], [], []

    class DocumentWrapper:
        def __init__(self, native):
            self.native = native

        def __eq__(self, other):
            return isinstance(other, DocumentWrapper) and self.native is other.native

    def native_document(*args):
        native = types.SimpleNamespace(ready=False, open=True)
        natives.append(native)
        owner = DocumentWrapper(native)
        owner_refs.append(weakref.ref(owner))
        calls.append(command)
        return owner

    def add_view(document):
        calls.append("add_view")
        # Register before view initialization: failed initialization must still
        # expose the exact created/opened document and retain its original wrapper.
        assert list(host._owned_documents.values()) == [document]
        if failure_phase == "add_view_exception":
            raise RuntimeError("Injected view creation failure")
        if failure_phase == "add_view_none":
            return None
        return object()

    def show_view(view):
        calls.append("show_view")
        if failure_phase == "show_view":
            raise RuntimeError("Injected view activation failure")

    def metadata(handle, document):
        calls.append("metadata")
        if failure_phase == "metadata_bridge_error":
            raise BridgeError("METADATA_FAILED", "Injected metadata failure")
        raise RuntimeError("Injected metadata failure")

    def set_batchmode(value):
        if failure_phase == "batch_restore" and value is False:
            raise RuntimeError("Injected batch mode restoration failure")

    window = types.SimpleNamespace(addView=add_view, showView=show_view)
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._assert_gui_thread = lambda: None
    host._is_ready = lambda document: document.native.ready
    host._documents, host._owned_documents = {}, {}
    host._metadata = metadata
    host.input_roots = {}
    host._read_input_image = lambda path: None
    # File containment and decoding have separate tests; these cases isolate
    # errors after the native file/document creation has already succeeded.
    monkeypatch.setattr(
        "krita6_bridge.editing.resolve_input_path", lambda *args, **kwargs: Path("reference.png")
    )
    host.app = types.SimpleNamespace(
        # Like LibKis, each enumeration returns fresh nonowning wrappers.
        documents=lambda: [DocumentWrapper(native) for native in natives if native.open],
        activeWindow=lambda: window,
        profiles=lambda *args: [host_module.SRGB_PROFILE],
        createDocument=native_document,
        openDocument=native_document,
        batchmode=lambda: False,
        setBatchmode=set_batchmode,
    )
    host.execute = lambda request: getattr(host, "_" + command)(
        request["target"], request["params"]
    )
    ledger = OperationLedger("instance-one")
    request = validate_request(
        {
            "bridge_protocol": 1,
            "instance_id": "instance-one",
            "operation_id": "create-once",
            "command": command,
            "params": {"width": 16, "height": 16, "name": "scratch"}
            if command == "create_document"
            else {"root": "scratch", "path": "reference.png"},
        },
        "instance-one",
    )
    ledger.admit(request)
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host, executor.ledger = host, ledger
    executor._current = executor._pending = None
    executor._disposed = executor._draining = False
    executor.tick()
    pending = executor._pending
    assert isinstance(pending, host_module.Pending)
    handle = pending.document_id
    assert pending.result == {"document_id": handle}
    assert pending.error.effect == "partial"
    assert host._owned_documents[handle] is owner_refs[0]()
    executor.tick()
    executor.tick()
    assert owner_refs[0]() is not None
    assert host._documents[handle] is not owner_refs[0]()
    assert ledger.admit(request)["state"] == "running"
    assert executor._pending is pending
    assert not ledger.is_idle()

    natives[0].ready = True
    executor.tick()
    outcome = ledger.get("create-once")
    assert outcome["state"] == "failed"
    assert outcome["effect"] == "partial"
    assert outcome["error"]["code"] == (
        "CREATE_FAILED" if command == "create_document" else "OPEN_FAILED"
    )
    assert outcome["result"] == {"document_id": handle}
    assert ledger.admit(request) == outcome
    assert ledger.take_next() is None
    assert calls.count(command) == 1
    assert ledger.is_idle()
    # Closing the native document releases only the bridge-created owner; the
    # normal reconciliation registry continues to use fresh wrappers.
    natives[0].open = False
    assert host._reconcile_documents() == {}
    assert host._owned_documents == {}
    assert owner_refs[0]() is None


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


@pytest.mark.parametrize(
    "failure_phase", ["attach_exception", "attach_false", "activation", "refresh", "uuid", "name"]
)
@pytest.mark.parametrize("drain", [False, True])
def test_layer_failure_retains_gate_without_duplicate_layer(host_modules, failure_phase, drain):
    host_module, executor_module = host_modules
    created, attached, drained = [], [], []
    document = types.SimpleNamespace(ready=True)

    def create_node(name, kind):
        node = types.SimpleNamespace(index=len(created), label=name)

        def node_name():
            if node.index == 0 and failure_phase == "name":
                raise RuntimeError("injected metadata failure")
            return node.label

        node.name = node_name
        created.append(node)
        return node

    def attach_node(node, above):
        document.ready = False
        attached.append(node)
        if node.index == 0 and failure_phase == "attach_exception":
            raise RuntimeError("injected exception after scheduling attachment")
        return not (node.index == 0 and failure_phase == "attach_false")

    def refresh():
        if len(created) == 1 and failure_phase == "refresh":
            raise RuntimeError("injected exception after scheduling projection")

    def activate(node):
        if node.index == 0 and failure_phase == "activation":
            raise RuntimeError("injected layer activation failure")

    def node_id(node):
        if node.index == 0 and failure_phase == "uuid":
            raise RuntimeError("injected UUID metadata failure")
        return "node-" + str(node.index)

    parent = types.SimpleNamespace(type=lambda: "grouplayer", addChildNode=attach_node)
    document.rootNode = lambda: parent
    document.createNode = create_node
    document.setActiveNode = activate
    document.refreshProjection = refresh
    host = host_module.KritaHost.__new__(host_module.KritaHost)
    host._assert_gui_thread = lambda: None
    host._document = lambda handle: document
    host._require_ready = lambda document: None
    host._require_layer_capacity = lambda document: None
    host._writable_color = lambda document: None
    host._unlocked_ancestry = lambda node: None
    host._node_id = node_id
    host._is_ready = lambda document: document.ready
    host.execute = lambda request: host._create_paint_layer(request["target"], request["params"])
    ledger = OperationLedger("instance-one")

    def request(identifier):
        return validate_request(
            {
                "bridge_protocol": 1,
                "instance_id": "instance-one",
                "operation_id": identifier,
                "command": "create_paint_layer",
                "target": {"document_id": "doc-one"},
                "params": {"name": identifier},
            },
            "instance-one",
        )

    first, following = request("layer-first"), request("layer-following")
    ledger.admit(first)
    ledger.admit(following)
    executor = executor_module.GuiExecutor.__new__(executor_module.GuiExecutor)
    executor.host, executor.ledger = host, ledger
    executor._current = executor._pending = None
    executor._disposed = executor._draining = False
    executor.on_drained = lambda: drained.append(True)
    executor.timer = types.SimpleNamespace(isActive=lambda: True, stop=lambda: None)
    executor.tick()
    assert len(created) == len(attached) == 1
    assert ledger.admit(first)["state"] == "running"
    if drain:
        executor.drain()
    executor.tick()
    executor.tick()
    assert ledger.get("layer-first")["state"] == "running"
    assert ledger.get("layer-following")["state"] == ("cancelled" if drain else "queued")
    assert len(created) == len(attached) == 1
    assert not ledger.is_idle()
    assert drained == []

    document.ready = True
    executor.tick()
    outcome = ledger.get("layer-first")
    assert outcome["state"] == "failed"
    assert outcome["effect"] == ("unknown" if failure_phase.startswith("attach") else "partial")
    assert outcome["error"]["code"] == "CREATE_FAILED"
    assert outcome["result"] == {
        "document_id": "doc-one",
        **({"node_id": "node-0"} if failure_phase in {"activation", "refresh", "name"} else {}),
    }
    assert ledger.admit(first)["state"] == "failed"
    executor.tick()
    if drain:
        assert drained == [True]
        assert len(created) == len(attached) == 1
    else:
        assert ledger.get("layer-following")["state"] == "running"
        assert len(created) == len(attached) == 2
        document.ready = True
        executor.tick()
        assert ledger.get("layer-following")["state"] == "succeeded"
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


def test_editing_mutation_failure_keeps_barrier_until_completion(host_modules):
    host, _ = host_modules
    changes = []
    document = types.SimpleNamespace(
        setModified=lambda value: changes.append(("dirty", value)),
        refreshProjection=lambda: changes.append(("refresh",)),
    )
    fake = host.KritaHost.__new__(host.KritaHost)
    fake._document = lambda handle: document
    fake._assert_gui_thread = lambda: None
    fake._is_ready = lambda doc: False

    def fail_after_dispatch():
        raise RuntimeError("native change may already have happened")

    pending = fake._editing_mutate("doc-one", fail_after_dispatch, {"node_id": "node-one"})
    assert isinstance(pending, host.Pending)
    assert changes == [("dirty", True), ("refresh",)]
    assert pending.poll() is None
    fake._is_ready = lambda doc: True
    with pytest.raises(BridgeError) as error:
        pending.poll()
    assert error.value.code == "EDIT_FAILED"
    assert error.value.effect == "unknown"


@pytest.mark.parametrize(
    "rect", [(0, 0, 0, 1), (0, 0, 8193, 1), (0, 0, 8192, 8192), (-1, 0, 1, 1), (90, 0, 20, 1)]
)
def test_editing_region_guards_precede_pixel_allocation(host_modules, rect):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    doc = types.SimpleNamespace(width=lambda: 100, height=lambda: 100)
    with pytest.raises(BridgeError):
        fake._editing_rect(doc, *rect)


def test_hidden_paint_layer_properties_can_be_changed_but_locks_are_respected(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    node = types.SimpleNamespace(
        type=lambda: "paintlayer",
        animated=lambda: False,
        childNodes=lambda: [],
        locked=lambda: False,
        visible=lambda: False,
        parentNode=lambda: None,
    )
    fake._node = lambda doc, handle: node
    assert fake._editing_node(object(), "node") is node
    node.locked = lambda: True
    with pytest.raises(BridgeError) as error:
        fake._editing_node(object(), "node")
    assert error.value.code == "TARGET_LOCKED"


def test_editing_rejects_masked_layers_before_pixel_access(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    node = types.SimpleNamespace(
        type=lambda: "paintlayer", animated=lambda: False, childNodes=lambda: [object()]
    )
    fake._node = lambda doc, handle: node
    with pytest.raises(BridgeError) as error:
        fake._editing_node(object(), "node", pixels=True)
    assert error.value.code == "INVALID_TARGET_TYPE"


def test_activation_waits_for_actual_application_view(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    fake._assert_gui_thread = lambda: None
    wanted_doc, old_doc = object(), object()
    wanted_view = types.SimpleNamespace(document=lambda: wanted_doc)
    old_view = types.SimpleNamespace(document=lambda: old_doc)
    old_window = types.SimpleNamespace(views=lambda: [old_view], activeView=lambda: old_view)
    wanted_window = types.SimpleNamespace(
        views=lambda: [wanted_view],
        activeView=lambda: wanted_view,
        activate=lambda: None,
        showView=lambda view: None,
    )
    fake.app = types.SimpleNamespace(
        activeWindow=lambda: old_window, windows=lambda: [old_window, wanted_window]
    )
    fake._document = lambda handle: wanted_doc
    fake._require_ready = lambda doc: None
    fake._is_ready = lambda doc: True
    pending = fake._activate_document({"document_id": "doc-wanted"}, {})
    assert pending.poll() is None
    fake.app.activeWindow = lambda: wanted_window
    assert pending.poll()["active_view"] is True
    fake.app.activeWindow = lambda: old_window
    pending.deadline = 0
    with pytest.raises(BridgeError) as error:
        pending.poll()
    assert error.value.code == "ACTIVATION_FAILED"
    assert error.value.effect == "unknown"


def test_editing_refresh_failure_retains_pending_partial_effect(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    fake._assert_gui_thread = lambda: None

    def refresh():
        raise RuntimeError("projection failed")

    fake._document = lambda handle: types.SimpleNamespace(
        setModified=lambda value: None, refreshProjection=refresh
    )
    fake._is_ready = lambda doc: True
    pending = fake._editing_mutate("doc", lambda: None, {})
    with pytest.raises(BridgeError) as error:
        pending.poll()
    assert error.value.effect == "partial"


def test_move_detaches_before_insert_and_checks_position(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    calls = []
    node, sibling = object(), object()
    children = [node, sibling]

    class Parent:
        def childNodes(self):
            return children

        def removeChildNode(self, child):
            calls.append("detach")
            children.remove(child)
            return True

        def addChildNode(self, child, above):
            calls.append("insert")
            children.insert(children.index(above) + 1, child)
            return True

    parent = Parent()

    class Node:
        def parentNode(self):
            return parent

    node = Node()
    children[0] = node
    fake._editing_document = lambda handle: object()
    fake._editing_node = lambda doc, handle: node
    fake._editing_parent = lambda doc, params: (parent, sibling)

    def mutate(handle, callback, result):
        callback()
        return result

    fake._editing_mutate = mutate
    result = fake._move_layer(
        {"document_id": "doc", "node_id": "layer"}, {"above_node_id": "sibling"}
    )
    assert calls == ["detach", "insert"]
    assert children == [sibling, node]
    assert result["node_id"] == "layer"


@pytest.mark.parametrize("completed,effect", [(0, "unknown"), (1, "partial"), (2, "partial")])
def test_editing_steps_report_completed_phases(host_modules, completed, effect):
    host, _ = host_modules
    changes = []

    def write():
        changes.append("written")
        return True

    with pytest.raises(BridgeError) as error:
        host.KritaHost._editing_steps([write] * completed + [lambda: False])
    assert len(changes) == completed
    assert error.value.effect == effect


def test_partial_pixel_edit_survives_refresh_failure(host_modules):
    host, _ = host_modules
    fake = host.KritaHost.__new__(host.KritaHost)
    fake._assert_gui_thread = lambda: None
    cleared = []

    def clear():
        cleared.append(True)
        return True

    def failed_refresh():
        raise RuntimeError("refresh failed")

    document = types.SimpleNamespace(
        setModified=lambda value: None, refreshProjection=failed_refresh
    )
    fake._document = lambda handle: document
    fake._is_ready = lambda doc: True
    pending = fake._editing_mutate(
        "doc", lambda: fake._editing_steps([clear, lambda: False]), {"node_id": "node"}
    )
    assert cleared == [True]
    with pytest.raises(BridgeError) as error:
        pending.poll()
    assert error.value.effect == "partial"
