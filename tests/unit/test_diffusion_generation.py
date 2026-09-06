"""Synthetic adapter tests; live add-on/backend evidence is recorded separately."""

import asyncio
import gc
import hashlib
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace
from typing import NamedTuple
import weakref

import pytest

from krita6_bridge import diffusion_generation as generation
from krita6_bridge.protocol import BridgeError
from krita6_bridge.transport import ArtifactStore


@pytest.fixture
def loaded(monkeypatch):
    JobState = Enum("JobState", "queued executing finished cancelled")
    JobKind = Enum("JobKind", "diffusion live_preview")
    ClientEvent = Enum("ClientEvent", "finished interrupted error")
    WorkflowKind = Enum("WorkflowKind", "generate refine inpaint refine_region custom")
    Workspace = Enum("Workspace", "generation live custom")
    ApplyBehavior = Enum("ApplyBehavior", "layer replace")
    ApplyRegionBehavior = Enum("ApplyRegionBehavior", "none layer_group")

    class Extent(NamedTuple):
        width: int
        height: int

    class Image:
        def __init__(self, width=512, height=512):
            self.extent = Extent(width, height)
            self.encoded = b"\x89PNG\r\n\x1a\ntest"

        @staticmethod
        def scale(image, extent):
            result = Image(*extent)
            result.encoded = image.encoded
            return result

        def to_bytes(self, format):
            return self.encoded

    class Node:
        def __init__(self, identifier):
            self.identifier = identifier

        def uniqueId(self):
            return SimpleNamespace(toString=lambda: "{" + self.identifier + "}")

    class Document:
        def __init__(self):
            self.dimensions = (512, 512)
            self.nodes = [Node("existing")]
            self.refreshes = 0

        def width(self):
            return self.dimensions[0]

        def height(self):
            return self.dimensions[1]

        def xOffset(self):
            return 0

        def yOffset(self):
            return 0

        def refreshProjection(self):
            self.refreshes += 1

        def rootNode(self):
            return SimpleNamespace(childNodes=lambda: list(self.nodes))

    class Job:
        def __init__(self, kind, params):
            self.kind, self.params = kind, params
            self.state = JobState.queued
            self.id = None
            self.results = []

    class Queue(list):
        def __init__(self):
            super().__init__()
            self.finished = []
            self.used = []

        def add(self, kind, params):
            job = Job(kind, params)
            self.append(job)
            return job

        def notify_finished(self, job):
            job.state = JobState.finished
            self.finished.append(job.id)

        def notify_cancelled(self, job):
            job.state = JobState.cancelled

        def notify_used(self, job_id, index):
            self.used.append((job_id, index))

    class ComfyClient:
        url = "http://127.0.0.1:8188"

    client = ComfyClient()
    connection = SimpleNamespace(client_if_connected=client, error="")
    style = SimpleNamespace(filename="built-in/digital-artwork.json", name="Digital Artwork")
    layer = SimpleNamespace(is_visible=True)
    document = Document()

    class DocumentModel:
        def __init__(self):
            self.document = SimpleNamespace(
                _doc=document,
                is_valid=True,
                is_active=True,
                check_color_mode=lambda: (True, None),
            )
            self._connection = connection
            self.workspace = Workspace.generation
            self.edit_mode = False
            self.live = SimpleNamespace(is_active=False)
            self.custom = SimpleNamespace(is_live=False)
            self.regions = SimpleNamespace(
                positive="original positive", negative="original negative"
            )
            self.style = style
            self.strength = 0.5
            self.seed = 42
            self.fixed_seed = False
            self.batch_count = 9
            self.layers = SimpleNamespace(all=[layer])
            self.layers.updated = lambda: self.layers
            self.jobs = Queue()
            self.progress = 0
            self.prepared = []
            self.submitted = []
            self.applied = []
            self.automatic = []
            self.prepare_error = False
            self.enqueue_error = False
            self.apply_error = False
            self.work_transform = lambda work, params: None

        def _prepare_workflow(self):
            self.prepared.append(
                (
                    self.regions.positive,
                    self.regions.negative,
                    self.strength,
                    self.seed,
                    self.fixed_seed,
                    self.style,
                )
            )
            if self.prepare_error:
                layer.is_visible = False
                raise RuntimeError("SECRET_TOKEN")
            extent = Extent(*document.dimensions)
            work = SimpleNamespace(
                kind=WorkflowKind.generate,
                extent=SimpleNamespace(input=extent, initial=extent, desired=extent, target=extent),
                sampling=SimpleNamespace(seed=self.seed),
                images=SimpleNamespace(hires_mask=None),
                custom_workflow=None,
                crop_upscale_extent=None,
                batch_count=9,
            )
            params = SimpleNamespace(bounds=(0, 0, *extent), resize_canvas=False, is_layered=False)
            self.work_transform(work, params)
            return work, params, None

        async def _enqueue_job(self, job, work, front=False):
            self.submitted.append((job, work, front))
            job.id = "upstream-" + str(len(self.submitted))
            if self.enqueue_error:
                raise RuntimeError("SECRET_SERVER_URL")

        def _finish_job(self, job, event):
            if event is ClientEvent.finished:
                self.jobs.notify_finished(job)
                self.automatic.append(job)
            else:
                self.jobs.notify_cancelled(job)

        def apply_result(self, image, params, behavior, region_behavior, prefix):
            self.applied.append((image, params, behavior, region_behavior, prefix))
            document.nodes.append(Node("generated-" + str(len(self.applied))))
            if self.apply_error:
                raise RuntimeError("SECRET_LOCAL_PATH")

    model = DocumentModel()
    root = SimpleNamespace(models=[model], connection=connection)
    status = {
        "availability": "available",
        "connection_state": "connected",
    }
    reader = SimpleNamespace(
        _context=lambda: (status, root.models),
        _find_model=lambda models, native: next(
            (m for m in models if m.document._doc == native), None
        ),
    )
    loop = asyncio.new_event_loop()
    styles = [style]

    def module(name, **values):
        result = ModuleType(name)
        vars(result).update(values)
        monkeypatch.setitem(sys.modules, name, result)
        return result

    modules = {
        "root": module("ai_diffusion.model.root", root=root),
        "model": module("ai_diffusion.model.model", DocumentModel=DocumentModel),
        "jobs": module("ai_diffusion.model.jobs", JobKind=JobKind),
        "comfy": module("ai_diffusion.backend.comfy_client", ComfyClient=ComfyClient),
        "client": module(
            "ai_diffusion.backend.client",
            ClientEvent=ClientEvent,
            filter_supported_styles=lambda styles, client: styles,
        ),
        "style": module("ai_diffusion.style", Styles=SimpleNamespace(_instance=styles)),
        "eventloop": module("ai_diffusion.eventloop", _loop=loop),
        "image": module("ai_diffusion.image", Image=Image, Extent=Extent),
        "settings": module(
            "ai_diffusion.settings",
            ApplyBehavior=ApplyBehavior,
            ApplyRegionBehavior=ApplyRegionBehavior,
            ImageFileFormat=SimpleNamespace(png="PNG"),
            settings=SimpleNamespace(generation_finished_action="apply"),
        ),
    }
    for name in generation.SOURCE_HASHES:
        if name not in {value.__name__ for value in modules.values()}:
            module(name)
    gui_calls = []
    controller = generation.DiffusionGenerator(
        lambda: gui_calls.append(True), reader, ArtifactStore()
    )
    monkeypatch.setattr(controller, "_verify_sources", lambda: None)

    def pump():
        loop.run_until_complete(asyncio.sleep(0))

    fixture = SimpleNamespace(
        controller=controller,
        document=document,
        Document=Document,
        model=model,
        reader=reader,
        root=root,
        connection=connection,
        client=client,
        style=style,
        styles=styles,
        status=status,
        layer=layer,
        modules=modules,
        loop=loop,
        pump=pump,
        Image=Image,
        Extent=Extent,
        events=ClientEvent,
        states=JobState,
        kinds=JobKind,
        Workspace=Workspace,
        ApplyBehavior=ApplyBehavior,
        ApplyRegionBehavior=ApplyRegionBehavior,
        gui_calls=gui_calls,
    )
    yield fixture
    for task in asyncio.all_tasks(loop):
        task.cancel()
    pump()
    loop.close()


def submit(loaded, **params):
    return loaded.controller.generate(
        "document-one", loaded.document, {"positive_prompt": "mountain", **params}, "request-one"
    )


def finish(loaded, *images):
    submit(loaded)
    loaded.pump()
    job = loaded.model.jobs[0]
    job.results = list(images or [loaded.Image()])
    loaded.model._finish_job(job, loaded.events.finished)
    snapshot = loaded.controller.get_generation("document-one", loaded.document, "request-one")
    return job, snapshot


def test_submit_captures_request_and_restores_ui_before_scheduling(loaded):
    result = submit(loaded, negative_prompt="fog", strength=0.6, seed=123)
    assert result["state"] == "submitting"
    assert loaded.model.submitted == []
    assert loaded.model.prepared[0][:5] == ("mountain", "fog", 0.6, 123, True)
    assert (loaded.model.regions.positive, loaded.model.regions.negative) == (
        "original positive",
        "original negative",
    )
    assert (loaded.model.strength, loaded.model.seed, loaded.model.fixed_seed) == (0.5, 42, False)
    assert loaded.model.batch_count == 9
    loaded.pump()
    job, work, front = loaded.model.submitted[0]
    assert work.batch_count == 1 and front is False
    assert job.params.seed == 123 and job.params.workflow_kind is work.kind
    assert job.params.has_mask is False


def test_repeated_generation_identity_never_submits_twice(loaded):
    first = submit(loaded)
    assert submit(loaded) == first
    loaded.pump()
    assert submit(loaded)["submission_state"] == "local_queued"
    assert len(loaded.model.submitted) == 1
    with pytest.raises(BridgeError, match="another request") as error:
        submit(loaded, strength=0.2)
    assert error.value.code == "OPERATION_ID_CONFLICT"


def test_preparation_failure_restores_excluded_visibility_and_prompts(loaded):
    loaded.model.prepare_error = True
    with pytest.raises(BridgeError) as error:
        submit(loaded)
    assert error.value.code == "DIFFUSION_PREPARATION_FAILED"
    assert "SECRET" not in str(error.value)
    assert loaded.layer.is_visible is True
    assert loaded.document.refreshes == 1
    assert loaded.model.regions.positive == "original positive"
    assert loaded.model.jobs == []


def test_owned_completion_does_not_apply_or_change_global_setting(loaded):
    job, result = finish(loaded)
    assert job.state is loaded.states.finished
    assert result["state"] == "finished" and result["result_count"] == 1
    assert loaded.model.jobs.finished == [job.id]
    assert loaded.model.automatic == [] and loaded.model.applied == []
    assert loaded.modules["settings"].settings.generation_finished_action == "apply"
    assert "_finish_job" not in vars(loaded.model)
    assert len(loaded.document.nodes) == 1


def test_unowned_completion_delegates_while_owned_guard_is_active(loaded):
    submit(loaded)
    unowned = loaded.model.jobs.add(loaded.kinds.diffusion, SimpleNamespace())
    loaded.model._finish_job(unowned, loaded.events.finished)
    assert loaded.model.automatic == [unowned]
    assert "_finish_job" in vars(loaded.model)


def test_guard_survives_controller_replacement_and_restores_after_last_job(loaded):
    submit(loaded)
    guard = loaded.model._krita6_mcp_finish_guard
    second = generation.DiffusionGenerator(lambda: None, loaded.reader, ArtifactStore())
    second._verify_sources = lambda: None
    second.generate("document-one", loaded.document, {"positive_prompt": "river"}, "request-two")
    assert loaded.model._krita6_mcp_finish_guard is guard
    loaded.pump()
    first, last = loaded.model.jobs
    loaded.model._finish_job(first, loaded.events.finished)
    assert "_finish_job" in vars(loaded.model)
    loaded.model._finish_job(last, loaded.events.finished)
    assert "_finish_job" not in vars(loaded.model)
    assert loaded.model.automatic == []


@pytest.mark.parametrize("change", ["backend", "document", "closed", "model", "connection_error"])
def test_context_change_before_dispatch_does_not_send(loaded, change):
    submit(loaded)
    if change == "backend":
        loaded.connection.client_if_connected = type(loaded.client)()
    elif change == "document":
        loaded.model.document._doc = loaded.Document()
    elif change == "closed":
        loaded.model.document.is_valid = False
    elif change == "model":
        loaded.root.models = []
    else:
        loaded.connection.error = "Disconnected SECRET_URL"
    loaded.pump()
    assert loaded.model.submitted == []
    assert loaded.controller._records["request-one"]["submission_state"] == "failed"
    assert "_finish_job" not in vars(loaded.model)


def test_uncertain_submission_keeps_identity_and_never_replays(loaded):
    loaded.model.enqueue_error = True
    submit(loaded)
    loaded.pump()
    result = submit(loaded)
    assert result["submission_state"] == "unknown"
    assert result["error_code"] == "DIFFUSION_SUBMISSION_UNKNOWN"
    assert result["upstream_job_id"] == "upstream-1"
    assert len(loaded.model.submitted) == 1
    assert "SECRET" not in str(result)
    assert "_finish_job" in vars(loaded.model)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8188",
        "https://cloud.example",
        "file:///tmp/a",
        "http://user:secret@127.0.0.1:8188",
        "http://127.0.0.1.evil/",
    ],
)
def test_nonlocal_and_credential_urls_are_rejected(loaded, url):
    loaded.client.url = url
    with pytest.raises(BridgeError) as error:
        submit(loaded)
    assert error.value.code == "DIFFUSION_LOCAL_BACKEND_REQUIRED"
    assert loaded.model.jobs == []


def test_capabilities_fail_closed_when_plugin_or_backend_is_unavailable(loaded):
    assert loaded.controller.capabilities()["generation_control"] is True
    loaded.status["availability"] = "not_loaded"
    result = loaded.controller.capabilities()
    assert result == {
        "generation_control": False,
        "generation_unavailable_reason": "DIFFUSION_UNAVAILABLE",
    }


def test_styles_use_opaque_stable_ids_and_exact_selection(loaded):
    first = loaded.controller.list_styles()["styles"][0]
    assert first["name"] == "Digital Artwork" and "/" not in first["style_id"]
    assert first == loaded.controller.list_styles()["styles"][0]
    assert submit(loaded, style_id=first["style_id"])["style_id"] == first["style_id"]


@pytest.mark.parametrize(
    "change", ["large_canvas", "large_workflow", "resize", "layered", "custom", "live", "edit_mode"]
)
def test_unsupported_or_oversized_work_is_rejected_before_enqueue(loaded, change):
    if change == "large_canvas":
        loaded.document.dimensions = (4096, 4096)
    elif change == "large_workflow":
        loaded.model.work_transform = lambda work, params: setattr(
            work.extent, "desired", loaded.Extent(8192, 8192)
        )
    elif change in {"resize", "layered"}:
        loaded.model.work_transform = lambda work, params: setattr(
            params, "resize_canvas" if change == "resize" else "is_layered", True
        )
    elif change == "custom":
        loaded.model.work_transform = lambda work, params: setattr(
            work, "custom_workflow", object()
        )
    elif change == "live":
        loaded.model.live.is_active = True
    else:
        loaded.model.edit_mode = True
    with pytest.raises(BridgeError):
        submit(loaded)
    assert loaded.model.jobs == [] and loaded.model.submitted == []


def test_result_handles_follow_image_identity_after_reordering(loaded):
    one, two = loaded.Image(), loaded.Image()
    job, snapshot = finish(loaded, one, two)
    one_id, two_id = [item["result_id"] for item in snapshot["results"]]
    job.results.reverse()
    newer = loaded.controller.get_generation("document-one", loaded.document, "request-one")
    assert [item["result_id"] for item in newer["results"]] == [two_id, one_id]
    loaded.controller.apply_result("document-one", loaded.document, "request-one", one_id)
    assert loaded.model.applied[0][0] is one
    assert loaded.model.jobs.used == [(job.id, 1)]


@pytest.mark.parametrize("prune", ["image", "job"])
def test_pruned_results_cannot_be_read_or_applied(loaded, prune):
    job, snapshot = finish(loaded)
    result_id = snapshot["results"][0]["result_id"]
    if prune == "image":
        job.results.clear()
    else:
        loaded.model.jobs.remove(job)
    for method, extra in (
        (loaded.controller.get_result, (1024,)),
        (loaded.controller.apply_result, ()),
    ):
        with pytest.raises(BridgeError) as error:
            method("document-one", loaded.document, "request-one", result_id, *extra)
        assert error.value.code == "DIFFUSION_RESULT_NOT_FOUND"
    assert loaded.model.applied == []


def test_ownership_does_not_keep_pruned_images_alive(loaded):
    job, snapshot = finish(loaded)
    reference = weakref.ref(job.results[0])
    job.results.clear()
    gc.collect()
    assert reference() is None
    assert snapshot["result_count"] == 1


def test_result_preview_is_bounded_and_does_not_apply(loaded):
    _, snapshot = finish(loaded, loaded.Image(2048, 1024))
    result_id = snapshot["results"][0]["result_id"]
    result = loaded.controller.get_result(
        "document-one", loaded.document, "request-one", result_id, 1024
    )
    assert (result["preview_width"], result["preview_height"]) == (1024, 512)
    assert result["scale"] == {"x": 0.5, "y": 0.5}
    assert loaded.controller.artifacts.get(result["artifact_id"])[1] == "image/png"
    assert loaded.model.applied == []


def test_oversized_encoded_result_is_rejected_by_artifact_limit(loaded):
    image = loaded.Image()
    image.encoded = b"x" * (2 * 1024 * 1024 + 1)
    _, snapshot = finish(loaded, image)
    with pytest.raises(BridgeError) as error:
        loaded.controller.get_result(
            "document-one", loaded.document, "request-one", snapshot["results"][0]["result_id"]
        )
    assert error.value.code == "ARTIFACT_TOO_LARGE"


def test_apply_uses_explicit_new_layer_policy_and_retains_existing_nodes(loaded):
    _, snapshot = finish(loaded)
    result = loaded.controller.apply_result(
        "document-one", loaded.document, "request-one", snapshot["results"][0]["result_id"]
    )
    assert result["new_node_ids"] == ["generated-1"]
    assert loaded.model.applied[0][2:] == (
        loaded.ApplyBehavior.layer,
        loaded.ApplyRegionBehavior.none,
        "[MCP] ",
    )
    assert [node.identifier for node in loaded.document.nodes] == ["existing", "generated-1"]
    assert loaded.model.automatic == []


@pytest.mark.parametrize("change", ["canvas", "bounds", "image", "resize", "layered", "inactive"])
def test_apply_rechecks_document_and_bounds(loaded, change):
    job, snapshot = finish(loaded)
    if change == "canvas":
        loaded.document.dimensions = (640, 512)
    elif change == "bounds":
        job.params.bounds = (1, 0, 512, 512)
    elif change == "image":
        job.results[0].extent = loaded.Extent(128, 128)
    elif change in {"resize", "layered"}:
        setattr(job.params, "resize_canvas" if change == "resize" else "is_layered", True)
    else:
        loaded.model.document.is_active = False
    with pytest.raises(BridgeError) as error:
        loaded.controller.apply_result(
            "document-one", loaded.document, "request-one", snapshot["results"][0]["result_id"]
        )
    assert error.value.effect == "none"
    assert loaded.model.applied == []


def test_apply_exception_preserves_unknown_effect_after_attachment(loaded):
    _, snapshot = finish(loaded)
    loaded.model.apply_error = True
    with pytest.raises(BridgeError) as error:
        loaded.controller.apply_result(
            "document-one", loaded.document, "request-one", snapshot["results"][0]["result_id"]
        )
    assert error.value.code == "DIFFUSION_APPLY_UNKNOWN"
    assert error.value.effect == "unknown"
    assert len(loaded.document.nodes) == 2
    assert "SECRET" not in str(error.value)


def test_generation_capacity_keeps_old_identity_instead_of_eviction(loaded, monkeypatch):
    monkeypatch.setattr(generation, "MAX_GENERATIONS", 1)
    submit(loaded)
    with pytest.raises(BridgeError) as error:
        loaded.controller.generate(
            "document-one", loaded.document, {"positive_prompt": "river"}, "request-two"
        )
    assert error.value.code == "DIFFUSION_GENERATION_LIMIT"
    assert submit(loaded)["generation_id"] == "request-one"


def test_private_hook_requires_verified_source_hashes(loaded, tmp_path, monkeypatch):
    expected = {}
    for name in generation.SOURCE_HASHES:
        source = tmp_path / (name + ".py")
        source.write_bytes(name.encode())
        sys.modules[name].__file__ = str(source)
        expected[name] = hashlib.sha256(name.encode()).hexdigest()
    monkeypatch.setattr(generation, "SOURCE_HASHES", expected)
    generation.DiffusionGenerator._verify_sources(loaded.controller)
    loaded.controller._verified_modules = None
    source.write_text("changed")
    with pytest.raises(BridgeError) as error:
        generation.DiffusionGenerator._verify_sources(loaded.controller)
    assert error.value.code == "DIFFUSION_INCOMPATIBLE"


def test_existing_third_party_completion_override_is_not_replaced(loaded):
    def original(job, event):
        return None

    loaded.model._finish_job = original
    with pytest.raises(BridgeError) as error:
        submit(loaded)
    assert error.value.code == "DIFFUSION_INCOMPATIBLE"
    assert loaded.model._finish_job is original
    assert loaded.model.jobs == []
