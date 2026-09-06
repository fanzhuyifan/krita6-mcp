"""Optional, GUI-only observation of an already loaded AI Diffusion plugin.

This adapter never imports ai_diffusion, constructs its models, or invokes a
connection, generation, selection, or job mutation method. Its private document
access is isolated here because the reviewed upstream revision has no public
native-document accessor.
"""

import hashlib
import math
import re
import sys
from enum import Enum
from itertools import islice
from types import ModuleType

from PyQt6.QtCore import QObject, QThread


REFERENCE_SOURCE_COMMIT = "dda58d1c63e361207ccec085efbc34dbd32f1654"
MAX_TRACKED_MODELS = 128
MAX_JOBS = 10000
MAX_PROMPT_CHARACTERS = 4096
MAX_LABEL_CHARACTERS = 128
MAX_REGIONS = 32
MAX_CONTROL_LAYERS = 64
MAX_REGION_LINKS = 32

CONTROL_MODES = frozenset(
    {
        "reference",
        "style",
        "composition",
        "face",
        "inpaint",
        "universal",
        "scribble",
        "line_art",
        "soft_edge",
        "canny_edge",
        "depth",
        "normal",
        "pose",
        "segmentation",
        "blur",
        "stencil",
        "hands",
    }
)
INPAINT_MODES = frozenset(
    {"automatic", "fill", "expand", "add_object", "remove_object", "replace_background", "custom"}
)

CONNECTION_STATES = frozenset(
    {
        "disconnected",
        "connecting",
        "connected",
        "error",
        "discover_models",
        "auth_missing",
        "auth_requesting",
        "auth_pending",
        "auth_error",
    }
)
JOB_STATES = frozenset({"queued", "executing", "finished", "cancelled"})
JOB_KINDS = frozenset(
    {
        "diffusion",
        "control_layer",
        "upscaling",
        "live_preview",
        "animation_batch",
        "animation_frame",
        "animation",
    }
)
WORKSPACES = frozenset({"generation", "upscaling", "live", "animation", "custom"})
ERROR_KINDS = frozenset(
    {
        "none",
        "plugin_error",
        "server_error",
        "insufficient_funds",
        "warning",
        "incompatible_lora",
        "validation_warning",
    }
)


class _Incompatible(Exception):
    pass


def _enum_name(value, allowed):
    if not isinstance(value, Enum) or value.name not in allowed:
        raise _Incompatible()
    return value.name


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise _Incompatible()
    return value


def _number(value, low, high):
    if type(value) not in (int, float):
        raise _Incompatible()
    try:
        if not math.isfinite(value) or not low <= value <= high:
            raise _Incompatible()
    except OverflowError:
        raise _Incompatible() from None
    return float(value)


def _text(value, limit, name, truncated):
    if not isinstance(value, str):
        raise _Incompatible()
    if len(value) > limit:
        truncated.append(name)
    return value[:limit]


def _qt_object(value):
    if not isinstance(value, QObject) or value.thread() != QThread.currentThread():
        raise _Incompatible()
    return value


def _boolean(value):
    if type(value) is not bool:
        raise _Incompatible()
    return value


def _node_id(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}",
            value,
        )
        is None
    ):
        raise _Incompatible()
    return value[1:-1]


def _style_id(style):
    filename = style.filename
    if not isinstance(filename, str) or not 1 <= len(filename) <= 4096:
        raise _Incompatible()
    return "style-" + hashlib.sha256(filename.encode("utf-8")).hexdigest()[:32]


def _optional(result, name, read, unavailable, path=None):
    try:
        result[name] = read()
    except Exception:
        result[name] = None
        unavailable.append(path or name)


class DiffusionReader:
    def __init__(self, assert_gui_thread):
        self._assert_gui_thread = assert_gui_thread

    def capabilities(self):
        self._assert_gui_thread()
        return {
            "read_only": True,
            "generation_control": False,
            "connection_control": False,
            "requires_already_loaded_plugin": True,
            "required_qt_major": 6,
            "reference_source_commit": REFERENCE_SOURCE_COMMIT,
            "reference_source": "Krita 6 development revision; version 1.53.0 alone is insufficient",
            "reference_validation": "read_only_probe_passed",
            "session_self_test": False,
            "evidence": "docs/validation.md",
            "compatibility_detection": "Qt6 objects and bounded read interface checks",
        }

    def _context(self):
        plugin = sys.modules.get("ai_diffusion")
        result = {
            "availability": "not_loaded",
            "plugin_version": None,
            "generation_control": False,
            "connection_control": False,
            "backend_health_check": False,
        }
        if plugin is None:
            return result, None
        if not isinstance(plugin, ModuleType):
            raise _Incompatible()
        version = vars(plugin).get("__version__")
        if isinstance(version, str):
            result["plugin_version"] = version[:64]
        module = sys.modules.get("ai_diffusion.model.root")
        if not isinstance(module, ModuleType):
            return dict(
                result, availability="incompatible", reason="unsupported_module_layout"
            ), None
        # The version string does not distinguish the Qt5 stable release from
        # the Qt6 development revision. Validate the loaded binding and objects.
        if vars(module).get("QObject") is not QObject:
            return dict(result, availability="incompatible", reason="requires_pyqt6"), None
        root = _qt_object(vars(module).get("root"))
        if "_models" not in vars(root) or "_connection" not in vars(root):
            return dict(result, availability="not_initialized"), None
        models = root.models
        if type(models) is not list or len(models) > MAX_TRACKED_MODELS:
            raise _Incompatible()
        connection = _qt_object(root.connection)
        state = _enum_name(connection.state, CONNECTION_STATES)
        connection_error = connection.error
        if not isinstance(connection_error, str):
            raise _Incompatible()
        model_count = None
        if state == "connected":
            client = connection.client_if_connected
            if client is None or type(client.models.checkpoints) is not dict:
                raise _Incompatible()
            model_count = _integer(len(client.models.checkpoints), 0, 1000000)
        result.update(
            availability="available",
            connection_state=state,
            connection_error_present=bool(connection_error),
            available_model_count=model_count,
            tracked_document_count=len(models),
        )
        return result, models

    @staticmethod
    def _failed(result=None):
        base = result or {"plugin_version": None}
        return dict(
            base,
            availability="incompatible",
            reason="read_interface_mismatch",
            generation_control=False,
            connection_control=False,
            backend_health_check=False,
        )

    def status(self):
        self._assert_gui_thread()
        try:
            result, _ = self._context()
            return result
        except Exception:
            # Exception messages, connection errors, and diagnostics may contain
            # URLs, credentials or local paths. Do not return any of them.
            return self._failed()

    @staticmethod
    def _find_model(models, document):
        matches = []
        for model in models:
            _qt_object(model)
            wrapper = _qt_object(model.document)
            # Upstream KritaDocument._doc is the existing native wrapper. Public
            # active()/constructors can write annotations and start timers.
            native = vars(wrapper).get("_doc")
            if native is None:
                raise _Incompatible()
            try:
                if native == document:
                    matches.append(model)
            except RuntimeError:
                # A closed upstream model can remain until its normal UI prune.
                continue
        if len(matches) > 1:
            raise _Incompatible()
        return matches[0] if matches else None

    def inspect_document(self, document_id, document):
        self._assert_gui_thread()
        result = {"document_id": document_id}
        try:
            context, models = self._context()
            result.update(context)
            if models is None:
                return result
            model = self._find_model(models, document)
            if model is None:
                return dict(result, document_status="model_not_created")
            truncated = []
            regions = _qt_object(model.regions)
            detail = {
                "workspace": _enum_name(model.workspace, WORKSPACES),
                "prompt_scope": "generation_root",
                "positive_prompt": _text(
                    regions.positive, MAX_PROMPT_CHARACTERS, "positive_prompt", truncated
                ),
                "negative_prompt": _text(
                    regions.negative, MAX_PROMPT_CHARACTERS, "negative_prompt", truncated
                ),
                "style_name": _text(
                    model.style.name, MAX_LABEL_CHARACTERS, "style_name", truncated
                ),
                "strength": _number(model.strength, 0.0, 1.0),
                "batch_count": _integer(model.batch_count, 1, 1000),
                "progress": _number(model.progress, -1.0, 1.0),
                "progress_kind": _enum_name(model.progress_kind, {"generation", "upload"}),
                "error_kind": _enum_name(model.error.kind, ERROR_KINDS),
                "truncated_fields": truncated,
            }
            unavailable = []
            for name, read in (
                ("seed", lambda: _integer(model.seed, 0, 2**64 - 1)),
                ("fixed_seed", lambda: _boolean(model.fixed_seed)),
                ("edit_mode", lambda: _boolean(model.edit_mode)),
                ("region_only", lambda: _boolean(model.region_only)),
                ("resolution_multiplier", lambda: _number(model.resolution_multiplier, 0.25, 2)),
                ("use_inpaint", lambda: _boolean(model.inpaint.use_inpaint)),
                ("use_prompt_focus", lambda: _boolean(model.inpaint.use_prompt_focus)),
                ("style_id", lambda: _style_id(model.style)),
            ):
                _optional(detail, name, read, unavailable)
            detail["unavailable_fields"] = unavailable
            detail["canvas_context"] = self._canvas_context(model, document, detail)
            return dict(result, document_status="tracked", model=detail)
        except Exception:
            return self._failed(result)

    @staticmethod
    def _canvas_context(model, document, detail):
        # Avoid regions.active/name/layers and control.layer: those upstream
        # getters update tracking, prune links, or emit signals even on reads.
        truncated, unavailable = [], []
        context = {
            "snapshot_only": True,
            "truncated_fields": truncated,
            "unavailable_fields": unavailable,
        }

        def selection_bounds():
            selection = document.selection()
            if selection is None:
                return None
            return {
                "x": _integer(selection.x(), -(2**31), 2**31 - 1),
                "y": _integer(selection.y(), -(2**31), 2**31 - 1),
                "width": _integer(selection.width(), 0, 2**31 - 1),
                "height": _integer(selection.height(), 0, 2**31 - 1),
            }

        def active_node_id():
            node = document.activeNode()
            return _node_id(node.uniqueId().toString()) if node is not None else None

        _optional(context, "selection_bounds", selection_bounds, unavailable)
        _optional(context, "active_node_id", active_node_id, unavailable)
        _optional(
            context,
            "inpaint_mode",
            lambda: _enum_name(model.inpaint.mode, INPAINT_MODES),
            unavailable,
        )
        # The same condition selects active_regions upstream, without invoking
        # style resolution or any model initialization accessor.
        editing = detail["edit_mode"] and detail["workspace"] in {"generation", "live"}
        if detail["edit_mode"] is None:
            context["prompt_scope"] = None
            unavailable.append("prompt_scope")
            return context
        context["prompt_scope"] = "edit_root" if editing else "generation_root"
        try:
            root = _qt_object(model.edit_regions if editing else model.regions)
        except Exception:
            unavailable.append("active_regions")
            return context
        for field, attr in (("positive_prompt", "positive"), ("negative_prompt", "negative")):
            _optional(
                context,
                field,
                lambda attr=attr, field=field: _text(
                    getattr(root, attr), MAX_PROMPT_CHARACTERS, field, truncated
                ),
                unavailable,
            )

        remaining_controls = MAX_CONTROL_LAYERS

        def controls(owner, path):
            nonlocal remaining_controls
            queue = _qt_object(owner.control)
            count = _integer(len(queue), 0, 1000000)
            take = min(count, remaining_controls)
            remaining_controls -= take
            if take < count:
                truncated.append(path)
            result = []
            for index, value in enumerate(islice(queue, take)):
                control = {}
                location = f"{path}[{index}]"
                try:
                    _qt_object(value)
                    control = {
                        "node_id": _node_id(value.layer_id.toString()),
                        "mode": _enum_name(value.mode, CONTROL_MODES),
                        "strength": _number(value.strength, 0, 100000) / 50,
                        "start": _number(value.start, 0, 1),
                        "end": _number(value.end, 0, 1),
                        "is_supported": _boolean(value.is_supported),
                    }
                except Exception:
                    unavailable.append(location)
                result.append(dict(control, snapshot_index=index))
            return result

        _optional(context, "control_layers", lambda: controls(root, "control_layers"), unavailable)

        def region_summaries():
            count = _integer(len(root), 0, 1000000)
            if count > MAX_REGIONS:
                truncated.append("regions")
            result = []
            for index, value in enumerate(islice(root, MAX_REGIONS)):
                region = {"snapshot_index": index}
                path = f"regions[{index}]"
                try:
                    _qt_object(value)
                    linked_ids = value.layer_ids
                    if not isinstance(linked_ids, str):
                        raise _Incompatible()
                    ids = linked_ids.split(",", MAX_REGION_LINKS) if linked_ids else []
                    if len(ids) > MAX_REGION_LINKS:
                        truncated.append(path + ".linked_node_ids")
                    region["linked_node_ids"] = [_node_id(i) for i in ids[:MAX_REGION_LINKS]]
                    region["positive_prompt"] = _text(
                        value.positive, MAX_PROMPT_CHARACTERS, path + ".positive_prompt", truncated
                    )
                    _optional(
                        region,
                        "control_layers",
                        lambda: controls(value, path + ".control_layers"),
                        unavailable,
                        path + ".control_layers",
                    )
                except Exception:
                    unavailable.append(path)
                result.append(region)
            return result

        _optional(context, "regions", region_summaries, unavailable)
        return context

    def list_jobs(self, document_id, document, offset=0, limit=50):
        self._assert_gui_thread()
        result = {"document_id": document_id}
        try:
            _integer(offset, 0, 2**31 - 1)
            _integer(limit, 1, 100)
            context, models = self._context()
            result.update(context)
            if models is None:
                return result
            model = self._find_model(models, document)
            if model is None:
                return dict(result, document_status="model_not_created")
            queue = _qt_object(model.jobs)
            total = _integer(len(queue), 0, MAX_JOBS)
            # islice never traverses an unbounded offset; upstream history may be
            # pruned/reindexed between requests, so indices are snapshot-only.
            page = list(islice(queue, min(offset, total), min(offset + limit, total)))
            if len(page) != max(0, min(total - offset, limit)):
                raise _Incompatible()
            jobs = []
            for index, job in enumerate(page, offset):
                identifier = job.id
                if identifier is not None and (
                    not isinstance(identifier, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier) is None
                ):
                    raise _Incompatible()
                jobs.append(
                    {
                        "snapshot_index": index,
                        "job_id": identifier,
                        "kind": _enum_name(job.kind, JOB_KINDS),
                        "state": _enum_name(job.state, JOB_STATES),
                        "result_count": _integer(len(job.results), 0, 100000),
                    }
                )
            next_offset = offset + len(jobs)
            return dict(
                result,
                document_status="tracked",
                jobs=jobs,
                total=total,
                offset=offset,
                next_offset=next_offset if next_offset < total else None,
                identity="upstream_plugin_job_id_nullable; indices_are_snapshot_only",
                state_semantics="upstream_state; cancelled_can_include_failures",
            )
        except Exception:
            return self._failed(result)
