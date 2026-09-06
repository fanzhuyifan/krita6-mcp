"""Contract tests only; synthetic objects do not establish upstream compatibility."""

import importlib.abc
import importlib.util
import json
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def adapter(monkeypatch):
    gui_thread = object()

    class QObject:
        def __init__(self, **fields):
            self.__dict__.update(fields)

        def thread(self):
            return gui_thread

    qt = ModuleType("PyQt6.QtCore")
    qt.QObject = QObject
    qt.QThread = SimpleNamespace(currentThread=lambda: gui_thread)
    monkeypatch.setitem(sys.modules, "PyQt6", ModuleType("PyQt6"))
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qt)
    for name in list(sys.modules):
        if name == "ai_diffusion" or name.startswith("ai_diffusion."):
            monkeypatch.delitem(sys.modules, name)

    class NoPluginImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname == "ai_diffusion" or fullname.startswith("ai_diffusion."):
                raise AssertionError("The adapter attempted to import the AI plugin")

    monkeypatch.setattr(sys, "meta_path", [NoPluginImport(), *sys.meta_path])
    path = Path(__file__).parents[2] / "plugin" / "krita6_bridge" / "diffusion.py"
    spec = importlib.util.spec_from_file_location("diffusion_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    reader = module.DiffusionReader(lambda: calls.append("gui"))
    return SimpleNamespace(module=module, reader=reader, QObject=QObject, calls=calls)


@pytest.fixture
def loaded(adapter, monkeypatch):
    QObject = adapter.QObject
    ConnectionState = Enum("ConnectionState", ["disconnected", "connected", "auth_error"])
    Workspace = Enum("Workspace", ["generation", "live", "upscaling"])
    ProgressKind = Enum("ProgressKind", ["generation", "upload"])
    ErrorKind = Enum("ErrorKind", ["none", "server_error"])
    JobState = Enum("JobState", ["queued", "executing", "finished", "cancelled"])
    JobKind = Enum("JobKind", ["diffusion", "upscaling"])
    forbidden = []

    def forbidden_call(name):
        forbidden.append(name)
        raise AssertionError("Forbidden side effect or secret access: " + name)

    class NativeDocument:
        def __init__(self, identity):
            self.identity = identity

        def __eq__(self, other):
            return isinstance(other, NativeDocument) and self.identity == other.identity

        def selection(self):
            return None

        def activeNode(self):
            return None

    class Queue(QObject):
        def __init__(self, entries):
            super().__init__()
            self.entries = entries
            self.visited = 0

        def __len__(self):
            return len(self.entries)

        def __iter__(self):
            for entry in self.entries:
                self.visited += 1
                yield entry

        def select(self, *args):
            forbidden_call("select")

        @property
        def selection(self):
            return forbidden_call("selection")

    class Connection(QObject):
        state = ConnectionState.disconnected
        error = "SECRET_URL_TOKEN"

        @property
        def client_if_connected(self):
            if self.state is not ConnectionState.connected:
                return forbidden_call("disconnected_client")
            return self._client

        def connect(self, *args):
            forbidden_call("connect")

    class Root(QObject):
        @property
        def models(self):
            return self._models

        @property
        def connection(self):
            return self._connection

        @property
        def active_model(self):
            return forbidden_call("active_model")

        def model_for_active_document(self):
            return forbidden_call("model_for_active_document")

        def create_model(self, *args):
            return forbidden_call("create_model")

    native = NativeDocument("document-one")
    queue = Queue(
        [
            SimpleNamespace(id=None, kind=JobKind.diffusion, state=JobState.queued, results=[]),
            SimpleNamespace(
                id="job-one", kind=JobKind.diffusion, state=JobState.finished, results=[object()]
            ),
            SimpleNamespace(
                id="job-two", kind=JobKind.upscaling, state=JobState.cancelled, results=[]
            ),
        ]
    )
    model = QObject(
        document=QObject(_doc=native),
        regions=QObject(positive="sunrise", negative="fog"),
        workspace=Workspace.generation,
        style=SimpleNamespace(name="Watercolor", filename="watercolor.json"),
        seed=42,
        fixed_seed=True,
        edit_mode=False,
        region_only=False,
        strength=0.75,
        batch_count=2,
        progress=-1,
        progress_kind=ProgressKind.generation,
        error=SimpleNamespace(
            kind=ErrorKind.server_error,
            message="SECRET_URL_TOKEN",
            data={"token": "SECRET_URL_TOKEN"},
        ),
        jobs=queue,
    )
    connection = Connection(
        _client=SimpleNamespace(
            models=SimpleNamespace(
                checkpoints={
                    "secret/local/path.safetensors": object(),
                    "SECRET_URL_TOKEN": object(),
                }
            )
        )
    )
    root = Root(_models=[model], _connection=connection)
    plugin = ModuleType("ai_diffusion")
    plugin.__version__ = "1.53.0"
    root_module = ModuleType("ai_diffusion.model.root")
    root_module.QObject = QObject
    root_module.root = root
    monkeypatch.setitem(sys.modules, "ai_diffusion", plugin)
    monkeypatch.setitem(sys.modules, "ai_diffusion.model.root", root_module)
    return SimpleNamespace(
        root=root,
        model=model,
        connection=connection,
        queue=queue,
        root_module=root_module,
        NativeDocument=NativeDocument,
        states=ConnectionState,
        workspaces=Workspace,
        forbidden=forbidden,
    )


@pytest.fixture
def canvas(adapter, loaded):
    class Collection(adapter.QObject):
        def __init__(self, entries=(), **fields):
            super().__init__(**fields)
            self.entries = list(entries)
            self.visited = 0

        def __len__(self):
            return len(self.entries)

        def __iter__(self):
            for entry in self.entries:
                self.visited += 1
                yield entry

        @property
        def active(self):
            raise AssertionError("Reading active regions updates the model")

    class Region(adapter.QObject):
        @property
        def layers(self):
            raise AssertionError("Reading region layers prunes links")

        @property
        def name(self):
            raise AssertionError("Reading the region name prunes links")

    class Control(adapter.QObject):
        @property
        def layer(self):
            raise AssertionError("Reading the control layer updates tracking")

    uid = "{00000000-0000-0000-0000-000000000042}"
    control = Control(
        layer_id=SimpleNamespace(toString=lambda: uid),
        mode=Enum("ControlMode", ["reference"]).reference,
        strength=75,
        start=0.1,
        end=0.9,
        is_supported=True,
    )
    region = Region(layer_ids=uid, positive="red flowers", control=Collection([control]))
    root = Collection([region], positive="garden", negative="fog", control=Collection([control]))
    loaded.model.regions = root
    loaded.model.edit_regions = Collection(
        positive="make it snowy", negative="", control=Collection()
    )
    loaded.model.inpaint = SimpleNamespace(mode=Enum("InpaintMode", ["automatic"]).automatic)
    document = loaded.model.document._doc
    document.selection = lambda: SimpleNamespace(
        x=lambda: 10, y=lambda: 20, width=lambda: 80, height=lambda: 60
    )
    document.activeNode = lambda: SimpleNamespace(uniqueId=lambda: control.layer_id)
    return SimpleNamespace(
        document=document,
        root=root,
        region=region,
        control=control,
        Collection=Collection,
        uid=uid,
    )


def test_absent_plugin_is_not_imported_or_claimed_uninstalled(adapter):
    result = adapter.reader.status()
    assert result["availability"] == "not_loaded"
    assert "installed" not in result
    assert "ai_diffusion" not in sys.modules
    assert adapter.calls == ["gui"]


def test_version_alone_does_not_admit_qt5_plugin(adapter, loaded):
    loaded.root_module.QObject = object
    result = adapter.reader.status()
    assert result["availability"] == "incompatible"
    assert result["reason"] == "requires_pyqt6"
    assert result["plugin_version"] == "1.53.0"


def test_uninitialized_root_does_not_create_models_or_connect(adapter, loaded):
    del loaded.root._models
    assert adapter.reader.status()["availability"] == "not_initialized"
    assert loaded.forbidden == []


def test_connection_status_is_bounded_and_does_not_read_stale_client_or_secrets(adapter, loaded):
    result = adapter.reader.status()
    assert result["availability"] == "available"
    assert result["connection_state"] == "disconnected"
    assert result["connection_error_present"] is True
    assert result["backend_health_check"] is False
    assert result["available_model_count"] is None
    loaded.connection.state = loaded.states.connected
    result = adapter.reader.status()
    assert result["available_model_count"] == 2
    assert result["connection_error_present"] is True
    assert "SECRET_URL_TOKEN" not in json.dumps(result)
    assert "safetensors" not in json.dumps(result)
    assert loaded.forbidden == []


def test_inspection_matches_fresh_document_wrappers_and_bounds_prompt_text(adapter, loaded):
    loaded.model.regions.positive = "x" * (adapter.module.MAX_PROMPT_CHARACTERS + 5)
    document = loaded.NativeDocument("document-one")
    assert document is not loaded.model.document._doc
    result = adapter.reader.inspect_document("doc-handle", document)
    assert result["document_status"] == "tracked"
    detail = result["model"]
    assert len(detail["positive_prompt"]) == adapter.module.MAX_PROMPT_CHARACTERS
    assert detail["truncated_fields"] == ["positive_prompt"]
    assert detail["style_name"] == "Watercolor"
    assert detail["strength"] == 0.75
    assert detail["progress"] == -1.0
    assert detail["error_kind"] == "server_error"
    assert "SECRET_URL_TOKEN" not in json.dumps(result)
    assert loaded.forbidden == []


def test_canvas_metadata_is_plain_bounded_observation(adapter, loaded, canvas):
    detail = adapter.reader.inspect_document("doc", canvas.document)["model"]
    assert detail["seed"] == 42
    assert detail["fixed_seed"] is True
    assert detail["style_id"] == adapter.module._style_id(loaded.model.style)
    assert "watercolor.json" not in json.dumps(detail)
    context = detail["canvas_context"]
    assert context["selection_bounds"] == {"x": 10, "y": 20, "width": 80, "height": 60}
    assert context["active_node_id"] == canvas.uid[1:-1]
    assert context["prompt_scope"] == "generation_root"
    assert context["inpaint_mode"] == "automatic"
    assert context["positive_prompt"] == "garden"
    assert context["regions"][0]["linked_node_ids"] == [canvas.uid[1:-1]]
    assert context["regions"][0]["positive_prompt"] == "red flowers"
    control = context["control_layers"][0]
    assert control["mode"] == "reference"
    assert control["node_id"] == canvas.uid[1:-1]
    assert control["strength"] == 1.5
    assert context["unavailable_fields"] == []
    assert context["truncated_fields"] == []
    assert loaded.forbidden == []
    assert json.loads(json.dumps(detail)) == detail


@pytest.mark.parametrize(
    "workspace,expected",
    [("generation", "edit_root"), ("live", "edit_root"), ("upscaling", "generation_root")],
)
def test_canvas_prompt_scope_matches_existing_edit_workspace(
    adapter, loaded, canvas, workspace, expected
):
    loaded.model.edit_mode = True
    loaded.model.workspace = loaded.workspaces[workspace]
    result = adapter.reader.inspect_document("doc", canvas.document)
    context = result["model"]["canvas_context"]
    assert context["prompt_scope"] == expected
    assert context["positive_prompt"] == ("make it snowy" if expected == "edit_root" else "garden")


def test_optional_metadata_failure_preserves_basic_inspection_without_details(
    adapter, loaded, canvas
):
    loaded.model.seed = True
    loaded.model.style.filename = None
    loaded.model.inpaint = None
    canvas.region.layer_ids = "not a node id SECRET_URL_TOKEN"
    canvas.control.end = float("nan")

    def stale_selection():
        raise RuntimeError("SECRET_URL_TOKEN")

    canvas.document.selection = stale_selection
    result = adapter.reader.inspect_document("doc", canvas.document)
    assert result["availability"] == "available"
    assert result["document_status"] == "tracked"
    assert result["model"]["positive_prompt"] == "garden"
    assert result["model"]["seed"] is None
    assert set(result["model"]["unavailable_fields"]) == {"seed", "style_id"}
    context = result["model"]["canvas_context"]
    assert context["selection_bounds"] is None
    assert set(context["unavailable_fields"]) == {
        "selection_bounds",
        "inpaint_mode",
        "control_layers[0]",
        "regions[0]",
    }
    assert "SECRET_URL_TOKEN" not in json.dumps(result)


def test_region_and_control_snapshot_limits_bound_total_work(adapter, loaded, canvas):
    canvas.root.entries = [canvas.region] * 100
    canvas.root.control.entries = [canvas.control] * 40
    canvas.region.control.entries = [canvas.control] * 40
    canvas.region.layer_ids = ",".join([canvas.uid] * 80)
    canvas.region.positive = "p" * 5000
    context = adapter.reader.inspect_document("doc", canvas.document)["model"]["canvas_context"]
    assert len(context["regions"]) == adapter.module.MAX_REGIONS
    controls = context["control_layers"] + [
        c for region in context["regions"] for c in region["control_layers"]
    ]
    assert len(controls) == adapter.module.MAX_CONTROL_LAYERS
    assert canvas.root.visited == adapter.module.MAX_REGIONS
    assert (
        canvas.root.control.visited + canvas.region.control.visited
        == adapter.module.MAX_CONTROL_LAYERS
    )
    assert len(context["regions"][0]["linked_node_ids"]) == adapter.module.MAX_REGION_LINKS
    assert len(context["regions"][0]["positive_prompt"]) == adapter.module.MAX_PROMPT_CHARACTERS
    assert "regions" in context["truncated_fields"]
    assert "regions[0].control_layers" in context["truncated_fields"]
    assert "regions[0].linked_node_ids" in context["truncated_fields"]


def test_absent_optional_edit_flag_does_not_guess_canvas_prompt_scope(adapter, loaded, canvas):
    del loaded.model.edit_mode
    result = adapter.reader.inspect_document("doc", canvas.document)
    assert result["document_status"] == "tracked"
    context = result["model"]["canvas_context"]
    assert context["prompt_scope"] is None
    assert "prompt_scope" in context["unavailable_fields"]


def test_untracked_document_is_reported_without_creating_model(adapter, loaded):
    doc = loaded.NativeDocument("document-two")
    for result in (
        adapter.reader.inspect_document("doc-other", doc),
        adapter.reader.list_jobs("doc-other", doc),
    ):
        assert result["document_status"] == "model_not_created"
        assert "model" not in result
        assert "jobs" not in result
    assert len(loaded.root._models) == 1
    assert loaded.forbidden == []


def test_jobs_keep_upstream_states_and_nullable_ids_with_bounded_live_pagination(adapter, loaded):
    document = loaded.NativeDocument("document-one")
    first = adapter.reader.list_jobs("doc-handle", document, limit=1)
    assert first["jobs"] == [
        {
            "snapshot_index": 0,
            "job_id": None,
            "kind": "diffusion",
            "state": "queued",
            "result_count": 0,
        }
    ]
    assert first["next_offset"] == 1
    second = adapter.reader.list_jobs("doc-handle", document, offset=1, limit=2)
    assert [job["state"] for job in second["jobs"]] == ["finished", "cancelled"]
    assert second["next_offset"] is None
    assert "cancelled_can_include_failures" in second["state_semantics"]
    loaded.queue.visited = 0
    last = adapter.reader.list_jobs("doc-handle", document, offset=2**31 - 1)
    assert last["jobs"] == []
    assert loaded.queue.visited <= len(loaded.queue)
    assert loaded.forbidden == []


@pytest.mark.parametrize(
    "name,value",
    [
        ("strength", float("nan")),
        ("batch_count", True),
        ("progress", float("inf")),
        ("style", None),
    ],
)
def test_changed_document_schema_fails_closed_without_error_dump(adapter, loaded, name, value):
    setattr(loaded.model, name, value)
    result = adapter.reader.inspect_document("doc-one", loaded.NativeDocument("document-one"))
    assert result["availability"] == "incompatible"
    assert result["reason"] == "read_interface_mismatch"
    assert "model" not in result
    assert "SECRET_URL_TOKEN" not in json.dumps(result)


def test_ambiguous_tracking_does_not_choose_arbitrary_model(adapter, loaded):
    loaded.root._models.append(loaded.model)
    result = adapter.reader.inspect_document("doc-one", loaded.NativeDocument("document-one"))
    assert result["availability"] == "incompatible"


def test_wrong_qt_object_thread_is_incompatible(adapter, loaded):
    loaded.root.thread = lambda: object()
    assert adapter.reader.status()["availability"] == "incompatible"


@pytest.mark.parametrize(
    "method,args",
    [
        ("capabilities", ()),
        ("status", ()),
        ("inspect_document", ("doc", object())),
        ("list_jobs", ("doc", object())),
    ],
)
def test_every_public_read_requires_gui_thread(adapter, method, args):
    def wrong_thread():
        raise RuntimeError("GUI thread required")

    reader = adapter.module.DiffusionReader(wrong_thread)
    with pytest.raises(RuntimeError, match="GUI thread required"):
        getattr(reader, method)(*args)
