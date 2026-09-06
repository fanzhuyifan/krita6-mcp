"""Bounded generation through the already loaded Krita AI Diffusion add-on.

All methods and continuations run on Krita's GUI thread. Private upstream calls
are restricted to the source revision tested with this bridge. Backend work is
owned independently from bridge operation completion and is never resubmitted.
"""

import asyncio
import hashlib
import ipaddress
import json
import secrets
import sys
import weakref
from types import ModuleType
from urllib.parse import urlsplit

from .protocol import BridgeError


MAX_GENERATIONS = 64
MAX_PIXELS = 16_000_000
MAX_RESULTS = 16
SOURCE_HASHES = {
    "ai_diffusion.model.model": "e662dc70fe54c7f561467b7081874095cea41d146d19b4e67bac807c20966f9d",
    "ai_diffusion.model.jobs": "f7d282cafa44bfe4524cf4559f49dc87291a0338d7b31e7e9f8dd78450a79cae",
    "ai_diffusion.backend.comfy_client": "fb582fb522013c78faa7bf8cf36070fcfcb01526f3a75195a586eb85123bd805",
    "ai_diffusion.document": "d3fab9a0f67456a033b543d82e577b17e52bb4c3a59001ea9f66877bbd520364",
    "ai_diffusion.model.root": "7449ddc8cb465767f57d52092d2917c45ead8ab5d34633272f5d2fc969d1471d",
    "ai_diffusion.model.connection": "629665d8587e46b88bd58a8dc34f993f774b7ef8e4bfc3f6c031d436c8fde162",
    "ai_diffusion.model.region": "638639314451eab3e223a3290fe161b9efd2a0bd2992708ded9b7d4e865c05a3",
    "ai_diffusion.model.control": "bb91016baf82fed5f07803a613c78cd5458732f619b766e0dbac90dc44242b73",
    "ai_diffusion.layer": "89268fa51a08290b0e5443a5875b9a28c199efaba1fb00071f563cd5104465a3",
    "ai_diffusion.image": "4ef8fa101aaeb0ce4bc9b15bf270b04fd94b883e439d586337402b2acb22b6f4",
    "ai_diffusion.settings": "61c6692d10fec866b6c34a9c25f8fc6dccf6f7b9d62b35ffce5d739ffbecc727",
    "ai_diffusion.backend.workflow": "b7b8d7742a3a2e0c6a7a4fee3c6f3039185fb55f0a8f1ffb0b03f4582dd074ec",
    "ai_diffusion.style": "1532113823147e803efbff8932c9adf81ee5edd50735804867e1c1bfa7739e8b",
    "ai_diffusion.model.custom_workflow": "b070734985da8b536ddd3a80405103d0ed063f9525026f2ac8444495449d90e2",
    "ai_diffusion.persistence": "0538984052db93bf20a963cfe95040b316008048cf1b6e44720abdcb18bf27b5",
}
_GUARD_ATTRIBUTE = "_krita6_mcp_finish_guard"


def _module(name):
    module = sys.modules.get(name)
    if not isinstance(module, ModuleType):
        raise BridgeError("DIFFUSION_INCOMPATIBLE", "Required add-on modules are not loaded.")
    return module


def _extent(value):
    width, height = value
    if (
        type(width) is not int
        or type(height) is not int
        or min(width, height) <= 0
        or width * height > MAX_PIXELS
    ):
        raise BridgeError("DIFFUSION_SIZE_LIMIT", "Generation images must be at most 16 MP.")
    return width, height


def _bounds(value):
    x, y, width, height = value
    _extent((width, height))
    if type(x) is not int or type(y) is not int or min(x, y) < 0:
        raise BridgeError("DIFFUSION_BOUNDS_CHANGED", "Result bounds are not supported.")
    return {"x": x, "y": y, "width": width, "height": height}


class _CompletionGuard:
    """Suppress automatic canvas application for these jobs only.

    The model retains this guard across bridge stop/start while owned jobs run.
    Upstream's normal completion signals and history remain intact. Unowned
    jobs always use the original method and the user's current preferences.
    """

    def __init__(self, model, events):
        self.model = weakref.ref(model)
        self.original = weakref.WeakMethod(model._finish_job)
        self.finished_event = events.finished
        self.jobs = weakref.WeakSet()
        setattr(model, _GUARD_ATTRIBUTE, self)
        model._finish_job = self.finish

    def finish(self, job, event):
        if job not in self.jobs:
            return self.original()(job, event)
        model = self.model()
        try:
            if event is self.finished_event:
                model.jobs.notify_finished(job)
                model.progress = 1
            else:
                model.jobs.notify_cancelled(job)
                model.progress = 0
        finally:
            self.release(job)

    def release(self, job):
        self.jobs.discard(job)
        model = self.model()
        if not self.jobs and model is not None:
            if getattr(model, _GUARD_ATTRIBUTE, None) is self:
                # Do not overwrite another extension's later replacement.
                if model._finish_job == self.finish:
                    del model._finish_job
                delattr(model, _GUARD_ATTRIBUTE)


class DiffusionGenerator:
    def __init__(self, assert_gui_thread, reader, artifacts):
        self._assert_gui_thread = assert_gui_thread
        self.reader = reader
        self.artifacts = artifacts
        self._records = {}
        self._verified_modules = None

    def capabilities(self):
        try:
            self._context(connected=True)
        except BridgeError as error:
            return {"generation_control": False, "generation_unavailable_reason": error.code}
        return {
            "read_only": False,
            "generation_control": True,
            "generation_backend": "connected_loopback_comfyui",
            "generation_limit": MAX_GENERATIONS,
            "generation_max_pixels": MAX_PIXELS,
            "generation_batch_count": 1,
            "automatic_application": False,
            "generation_cancellation": False,
        }

    def _verify_sources(self):
        modules = tuple(_module(name) for name in SOURCE_HASHES)
        if self._verified_modules == modules:
            return
        try:
            for module, expected in zip(modules, SOURCE_HASHES.values()):
                with open(module.__file__, "rb") as source:
                    data = source.read(1024 * 1024 + 1)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError("source mismatch")
        except (OSError, AttributeError, ValueError):
            raise BridgeError(
                "DIFFUSION_INCOMPATIBLE",
                "Generation requires the tested Qt6 add-on source revision; see the integration guide.",
            ) from None
        self._verified_modules = modules

    def _context(self, connected=False):
        self._assert_gui_thread()
        try:
            status, models = self.reader._context()
            if status["availability"] != "available" or models is None:
                raise BridgeError("DIFFUSION_UNAVAILABLE", "AI Diffusion is not ready.")
            self._verify_sources()
            root = _module("ai_diffusion.model.root").root
            if connected:
                connection = root.connection
                if status["connection_state"] != "connected" or connection.error:
                    raise BridgeError(
                        "DIFFUSION_NOT_CONNECTED", "Connect AI Diffusion to its local server first."
                    )
                client = connection.client_if_connected
                comfy = _module("ai_diffusion.backend.comfy_client").ComfyClient
                if type(client) is not comfy or not self._local_url(client.url):
                    raise BridgeError(
                        "DIFFUSION_LOCAL_BACKEND_REQUIRED",
                        "Generation requires the add-on's connected loopback ComfyUI backend.",
                    )
            return root, models
        except BridgeError:
            raise
        except Exception:
            raise BridgeError("DIFFUSION_INCOMPATIBLE", "The add-on interface changed.") from None

    @staticmethod
    def _local_url(url):
        if not isinstance(url, str):
            return False
        try:
            parsed = urlsplit(url if "://" in url else "http://" + url)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                return False
            if parsed.hostname == "localhost":
                return True
            return ipaddress.ip_address(parsed.hostname).is_loopback
        except (ValueError, TypeError):
            return False

    def _model(self, document, connected=False):
        root, models = self._context(connected)
        try:
            model = self.reader._find_model(models, document)
            if model is None:
                raise BridgeError(
                    "DIFFUSION_MODEL_REQUIRED", "Open AI Diffusion for the target document first."
                )
            if type(model) is not _module("ai_diffusion.model.model").DocumentModel:
                raise BridgeError("DIFFUSION_INCOMPATIBLE", "Unsupported add-on document model.")
            if not model.document.is_valid or model.document._doc != document:
                raise BridgeError("DOCUMENT_NOT_FOUND", "The diffusion document is no longer open.")
            return root, model
        except BridgeError:
            raise
        except Exception:
            raise BridgeError(
                "DIFFUSION_INCOMPATIBLE", "Cannot resolve the diffusion document."
            ) from None

    @staticmethod
    def _styles(root):
        styles = _module("ai_diffusion.style").Styles._instance
        if styles is None or len(styles) > 1024:
            raise BridgeError("DIFFUSION_INCOMPATIBLE", "The loaded style catalog is unavailable.")
        return _module("ai_diffusion.backend.client").filter_supported_styles(
            list(styles), root.connection.client_if_connected
        )

    def list_styles(self):
        root, _ = self._context(connected=True)
        return {
            "styles": [
                {"style_id": self._style_id(style), "name": style.name[:128]}
                for style in self._styles(root)
                if isinstance(style.filename, str) and 0 < len(style.filename) <= 4096
            ],
            "backend": "local_comfyui",
        }

    @staticmethod
    def _style_id(style):
        return "style-" + hashlib.sha256(style.filename.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _generation_guard(model, document):
        if (
            model.workspace.name != "generation"
            or model.edit_mode
            or model.live.is_active
            or model.custom.is_live
        ):
            raise BridgeError(
                "DIFFUSION_WORKSPACE_UNSUPPORTED",
                "Use the Generate workspace with edit mode and live generation disabled.",
            )
        if not model.document.is_active:
            raise BridgeError("TARGET_NOT_ACTIVE", "The generation document must be active.")
        _extent((document.width(), document.height()))
        if document.xOffset() != 0 or document.yOffset() != 0:
            raise BridgeError(
                "DIFFUSION_BOUNDS_CHANGED", "Generation requires a zero-offset canvas."
            )
        ok, _ = model.document.check_color_mode()
        if not ok:
            raise BridgeError("UNSUPPORTED_COLORSPACE", "AI Diffusion requires an RGBA/U8 canvas.")

    def _prepare(self, model, document, params, style):
        values = {
            "style": model.style,
            "strength": model.strength,
            "seed": model.seed,
            "fixed_seed": model.fixed_seed,
            "edit_mode": model.edit_mode,
        }
        positive, negative = model.regions.positive, model.regions.negative
        layers = model.layers.updated().all
        if len(layers) > 10000:
            raise BridgeError("DIFFUSION_SIZE_LIMIT", "The document has too many layers.")
        visibility = [(layer, layer.is_visible) for layer in layers]
        try:
            model.style = style
            self._generation_guard(model, document)
            model.regions.positive = params["positive_prompt"]
            model.regions.negative = params.get("negative_prompt", "")
            model.strength = params.get("strength", 1.0)
            model.seed = params.get("seed", 0)
            model.fixed_seed = True
            work, job_params, _ = model._prepare_workflow()
        except BridgeError:
            raise
        except Exception:
            raise BridgeError(
                "DIFFUSION_PREPARATION_FAILED",
                "AI Diffusion could not prepare the request; check its style, controls, and models.",
            ) from None
        finally:
            # Upstream get_image() does not restore excluded layers on failure.
            # No event loop is pumped while capturing/restoring this snapshot.
            restore_failed = False
            for name, value in values.items():
                try:
                    setattr(model, name, value)
                except Exception:
                    restore_failed = True
            try:
                model.regions.positive, model.regions.negative = positive, negative
            except Exception:
                restore_failed = True
            visibility_changed = False
            for layer, visible in visibility:
                try:
                    if layer.is_visible != visible:
                        layer.is_visible = visible
                        visibility_changed = True
                except Exception:
                    restore_failed = True
            if visibility_changed:
                try:
                    document.refreshProjection()
                except Exception:
                    restore_failed = True
            if restore_failed:
                raise BridgeError(
                    "DIFFUSION_RESTORE_FAILED",
                    "Could not restore temporary add-on settings.",
                    "partial",
                )
        if work.kind.name not in {"generate", "refine", "inpaint", "refine_region"}:
            raise BridgeError("DIFFUSION_WORKFLOW_UNSUPPORTED", "Unsupported generated workflow.")
        if job_params.resize_canvas or job_params.is_layered or work.custom_workflow is not None:
            raise BridgeError(
                "DIFFUSION_WORKFLOW_UNSUPPORTED", "Layered/custom/resize output is unsupported."
            )
        for name in ("input", "initial", "desired", "target"):
            _extent(getattr(work.extent, name))
        if work.crop_upscale_extent is not None:
            _extent(work.crop_upscale_extent)
        _bounds(job_params.bounds)
        work.batch_count = 1
        job_params.seed = work.sampling.seed
        job_params.has_mask = work.images is not None and work.images.hires_mask is not None
        job_params.workflow_kind = work.kind
        return work, job_params

    @staticmethod
    def _completion_guard(model):
        guard = getattr(model, _GUARD_ATTRIBUTE, None)
        if guard is not None:
            if not isinstance(guard, _CompletionGuard) or model._finish_job != guard.finish:
                raise BridgeError(
                    "DIFFUSION_INCOMPATIBLE", "Another extension changed completion handling."
                )
            return guard
        original = _module("ai_diffusion.model.model").DocumentModel._finish_job
        if getattr(model._finish_job, "__func__", None) is not original:
            raise BridgeError("DIFFUSION_INCOMPATIBLE", "Unsupported completion handler override.")
        return _CompletionGuard(model, _module("ai_diffusion.backend.client").ClientEvent)

    def generate(self, document_id, document, params, generation_id):
        self._assert_gui_thread()
        identity = json.dumps([document_id, params], sort_keys=True, separators=(",", ":"))
        if generation_id in self._records:
            if self._records[generation_id]["identity"] != identity:
                raise BridgeError(
                    "OPERATION_ID_CONFLICT", "The generation ID belongs to another request."
                )
            return self.get_generation(document_id, document, generation_id)
        if len(self._records) >= MAX_GENERATIONS:
            raise BridgeError(
                "DIFFUSION_GENERATION_LIMIT", "This bridge session has reached 64 generations."
            )
        root, model = self._model(document, connected=True)
        self._generation_guard(model, document)
        styles = self._styles(root)
        style = model.style
        if params.get("style_id") is not None:
            style = next((s for s in styles if self._style_id(s) == params["style_id"]), None)
        if style is None or style not in styles:
            raise BridgeError(
                "DIFFUSION_STYLE_UNAVAILABLE", "Choose a style from the connected style list."
            )
        work, job_params = self._prepare(model, document, params, style)
        loop = _module("ai_diffusion.eventloop")._loop
        if loop.is_closed():
            raise BridgeError("DIFFUSION_UNAVAILABLE", "The add-on event loop has stopped.")
        guard = self._completion_guard(model)
        job = model.jobs.add(_module("ai_diffusion.model.jobs").JobKind.diffusion, job_params)
        guard.jobs.add(job)
        record = {
            "identity": identity,
            "document_id": document_id,
            "generation_id": generation_id,
            "model": weakref.ref(model),
            "job": weakref.ref(job),
            "task": None,
            "submission_state": "pending",
            "error_code": None,
            "results": {},
            "canvas": (document.width(), document.height()),
            "source_bounds": _bounds(job_params.bounds),
            "workflow": work.kind.name,
            "style_id": self._style_id(style),
        }
        self._records[generation_id] = record
        coroutine = self._submit(
            record, model, document, job, work, root.connection.client_if_connected, guard
        )
        try:
            record["task"] = loop.create_task(coroutine)
        except Exception:
            coroutine.close()
            record["submission_state"] = "failed"
            record["error_code"] = "DIFFUSION_SCHEDULING_FAILED"
            self._remove_unsubmitted(model, job, guard)
        return self._snapshot(record, model)

    @staticmethod
    def _remove_unsubmitted(model, job, guard):
        try:
            if any(item is job for item in model.jobs):
                model.jobs.remove(job)
        except Exception:
            # The document may have closed. Preserve the failed identity even
            # when its now-invalid upstream queue cannot be cleaned up.
            pass
        finally:
            guard.release(job)

    async def _submit(self, record, model, document, job, work, client, guard):
        dispatched = False
        try:
            self._assert_gui_thread()
            root, models = self._context(connected=True)
            if (
                model not in models
                or not model.document.is_valid
                or model.document._doc != document
                or root.connection.client_if_connected is not client
                or model._connection.client_if_connected is not client
            ):
                raise BridgeError(
                    "DIFFUSION_CONTEXT_CHANGED", "The generation target or backend changed."
                )
            dispatched = True
            await model._enqueue_job(job, work, front=False)
            if not isinstance(job.id, str) or not job.id or len(job.id) > 128:
                raise RuntimeError("missing local job identity")
            record["submission_state"] = "local_queued"
        except (Exception, asyncio.CancelledError):
            record["submission_state"] = "unknown" if dispatched else "failed"
            record["error_code"] = (
                "DIFFUSION_SUBMISSION_UNKNOWN" if dispatched else "DIFFUSION_CONTEXT_CHANGED"
            )
            if not dispatched:
                self._remove_unsubmitted(model, job, guard)

    def _record(self, document_id, document, generation_id):
        self._assert_gui_thread()
        record = self._records.get(generation_id)
        if record is None or record["document_id"] != document_id:
            raise BridgeError(
                "DIFFUSION_GENERATION_NOT_FOUND", "This bridge does not own that generation."
            )
        _, model = self._model(document)
        if record["model"]() is not model:
            raise BridgeError("DIFFUSION_CONTEXT_CHANGED", "The generation document model changed.")
        return record, model

    @staticmethod
    def _job(record, model):
        job = record["job"]()
        return job if job is not None and any(item is job for item in model.jobs) else None

    def _snapshot(self, record, model):
        task = record["task"]
        if task is not None and task.done():
            record["task"] = None
        job = self._job(record, model)
        state = job.state.name if job is not None else "unavailable"
        if record["submission_state"] == "pending":
            state = "submitting"
        elif record["submission_state"] in {"failed", "unknown"} and (job is None or not job.id):
            state = "submission_" + record["submission_state"]
        results = []
        if job is not None and job.state.name == "finished":
            if len(job.results) > MAX_RESULTS:
                raise BridgeError("DIFFUSION_RESULT_LIMIT", "The add-on returned too many images.")
            for image in job.results:
                width, height = _extent(image.extent)
                identifier = next(
                    (key for key, ref in record["results"].items() if ref() is image), None
                )
                if identifier is None:
                    if len(record["results"]) >= MAX_RESULTS:
                        raise BridgeError(
                            "DIFFUSION_RESULT_LIMIT", "Generation result identities are full."
                        )
                    identifier = secrets.token_hex(16)
                    record["results"][identifier] = weakref.ref(image)
                results.append({"result_id": identifier, "width": width, "height": height})
        return {
            "document_id": record["document_id"],
            "generation_id": record["generation_id"],
            "submission_state": record["submission_state"],
            "state": state,
            "upstream_job_id": job.id if job is not None else None,
            "workflow": record["workflow"],
            "style_id": record["style_id"],
            "source_bounds": record["source_bounds"],
            "results": results,
            "result_count": len(results),
            "error_code": record["error_code"],
            "automatic_application": False,
            "backend_admission": "not_separately_observed",
            "state_semantics": "upstream_cancelled_can_include_failures; missing_history_is_not_cancellation",
        }

    def get_generation(self, document_id, document, generation_id):
        record, model = self._record(document_id, document, generation_id)
        return self._snapshot(record, model)

    def _result(self, document_id, document, generation_id, result_id):
        record, model = self._record(document_id, document, generation_id)
        job = self._job(record, model)
        image = record["results"].get(result_id, lambda: None)()
        if job is None or job.state.name != "finished" or image is None:
            raise BridgeError(
                "DIFFUSION_RESULT_NOT_FOUND", "The owned result is absent or expired."
            )
        index = next((i for i, item in enumerate(job.results) if item is image), None)
        if index is None:
            raise BridgeError(
                "DIFFUSION_RESULT_NOT_FOUND", "The owned result was removed from history."
            )
        if (
            type(image) is not _module("ai_diffusion.image").Image
            or job.kind is not _module("ai_diffusion.model.jobs").JobKind.diffusion
        ):
            raise BridgeError("DIFFUSION_INCOMPATIBLE", "The owned result type changed.")
        return record, model, job, image, index

    def get_result(self, document_id, document, generation_id, result_id, max_edge=1024):
        record, _, _, image, _ = self._result(document_id, document, generation_id, result_id)
        if type(max_edge) is not int or not 32 <= max_edge <= 1024:
            raise BridgeError("INVALID_REQUEST", "Preview max_edge must be between 32 and 1024.")
        width, height = _extent(image.extent)
        module = _module("ai_diffusion.image")
        if type(image) is not module.Image:
            raise BridgeError("DIFFUSION_INCOMPATIBLE", "Unsupported result image type.")
        scale = min(1, max_edge / max(width, height))
        extent = module.Extent(max(1, round(width * scale)), max(1, round(height * scale)))
        preview = module.Image.scale(image, extent)
        encoded = bytes(preview.to_bytes(_module("ai_diffusion.settings").ImageFileFormat.png))
        artifact = self.artifacts.put(encoded, "image/png")
        return dict(
            artifact,
            document_id=document_id,
            generation_id=generation_id,
            result_id=result_id,
            source_bounds=record["source_bounds"],
            preview_width=extent.width,
            preview_height=extent.height,
            scale={"x": extent.width / width, "y": extent.height / height},
            color={"conversion": "AI Diffusion result PNG", "preview_profile": "unspecified"},
        )

    def apply_result(self, document_id, document, generation_id, result_id):
        record, model, job, image, index = self._result(
            document_id, document, generation_id, result_id
        )
        self._generation_guard(model, document)
        width, height = _extent(image.extent)
        bounds = _bounds(job.params.bounds)
        if (
            (document.width(), document.height()) != record["canvas"]
            or bounds != record["source_bounds"]
            or (width, height) != (bounds["width"], bounds["height"])
            or bounds["x"] + width > document.width()
            or bounds["y"] + height > document.height()
            or job.params.resize_canvas
            or job.params.is_layered
        ):
            raise BridgeError(
                "DIFFUSION_BOUNDS_CHANGED", "Canvas or result bounds changed since generation."
            )
        settings = _module("ai_diffusion.settings")
        before = {
            node.uniqueId().toString().strip("{}") for node in document.rootNode().childNodes()
        }
        try:
            model.apply_result(
                image,
                job.params,
                settings.ApplyBehavior.layer,
                settings.ApplyRegionBehavior.none,
                "[MCP] ",
            )
            document.refreshProjection()
            created = [
                node.uniqueId().toString().strip("{}")
                for node in document.rootNode().childNodes()
                if node.uniqueId().toString().strip("{}") not in before
            ]
            model.jobs.notify_used(job.id, index)
            if len(created) != 1:
                raise RuntimeError("new layer identity unavailable")
        except Exception:
            raise BridgeError(
                "DIFFUSION_APPLY_UNKNOWN",
                "Result application started but its outcome needs inspection.",
                "unknown",
            ) from None
        return {
            "document_id": document_id,
            "generation_id": generation_id,
            "result_id": result_id,
            "new_node_ids": created,
            "application": "new_layer_on_top",
            "effect": "applied",
        }
