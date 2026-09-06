"""Optional, GUI-only observation of an already loaded AI Diffusion plugin.

This adapter never imports ai_diffusion, constructs its models, or invokes a
connection, generation, selection, or job mutation method. Its private document
access is isolated here because the reviewed upstream revision has no public
native-document accessor.
"""

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
            return dict(result, document_status="tracked", model=detail)
        except Exception:
            return self._failed(result)

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
