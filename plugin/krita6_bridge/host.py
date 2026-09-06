"""GUI-thread-only implementation of the bounded Krita command catalog.

Import this module only inside Krita. The bridge's network modules never import
Qt or retain objects returned by this module, except for plain result data.
"""

from __future__ import annotations

import hashlib
import math
import platform
import uuid
from pathlib import Path
from xml.etree import ElementTree

from PyQt6.QtCore import (
    PYQT_VERSION_STR,
    QT_VERSION_STR,
    QBuffer,
    QByteArray,
    QIODevice,
    QPoint,
    QPointF,
    QThread,
    QUuid,
)
from PyQt6.QtGui import QColor, QPainterPath
from PyQt6.QtWidgets import QApplication
from krita import InfoObject, Krita, ManagedColor, Preset

from .protocol import BridgeError, COMMANDS
from .diffusion import DiffusionReader
from .diffusion_generation import DiffusionGenerator
from .output_paths import resolve_output_path
from .editing import EditingMixin


SRGB_PROFILE = "sRGB-elle-V2-srgbtrc.icc"
# Creation caps apply to all currently open documents and every layer/mask node,
# including documents/nodes created by the user outside the bridge.
MAX_OPEN_DOCUMENTS = 32
MAX_LAYER_NODES = 4096


class Pending:
    """Nonblocking native completion; contains no document or view wrappers."""

    def __init__(self, host, document_id, result, error=None):
        self.host = host
        self.document_id = document_id
        self.result = result
        self.error = error

    def poll(self):
        self.host._assert_gui_thread()
        try:
            document = self.host._document(self.document_id)
        except BridgeError:
            raise BridgeError(
                "OUTCOME_UNKNOWN", "The document closed before native completion.", effect="unknown"
            ) from None
        if self.host._is_ready(document):
            if self.error is not None:
                raise self.error
            return self.result
        # A caller's polling timeout is not native cancellation. Retain the
        # execution gate until completion (or closure), including while draining.
        return None


class KritaHost(EditingMixin):
    def __init__(self, artifacts, output_roots, input_roots=None):
        self._assert_gui_thread()
        self.app = Krita.instance()
        if self.app is None or self.app.version().split(".")[0] != "6":
            raise BridgeError("UNSUPPORTED_HOST", "This plugin requires Krita 6 and PyQt6.")
        self.artifacts = artifacts
        self.output_roots = {name: Path(path) for name, path in output_roots.items()}
        self.input_roots = {name: Path(path) for name, path in (input_roots or {}).items()}
        self._documents = {}
        # createDocument/openDocument return owning wrappers until addView
        # transfers native ownership. Retain only our originating wrappers so
        # failed initialization cannot delete a document during reconciliation.
        self._owned_documents = {}
        self._presets = {}
        self._diffusion = DiffusionReader(self._assert_gui_thread)
        self._diffusion_generator = DiffusionGenerator(
            self._assert_gui_thread, self._diffusion, self.artifacts
        )

    @staticmethod
    def _assert_gui_thread():
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError("Krita bridge host access must run on the GUI thread")

    def session_info(self):
        self._assert_gui_thread()
        return {
            "krita_version": self.app.version(),
            "qt_version": QT_VERSION_STR,
            "pyqt_version": PYQT_VERSION_STR,
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "display_backend": QApplication.platformName(),
            "capabilities": {
                "commands": sorted(COMMANDS),
                "host_validation": {
                    "reference_platform": "Linux",
                    "reference_krita_version": "6.0.3",
                    "reference_workflow": "passed",
                    "session_self_test": False,
                    "evidence": "docs/validation.md",
                },
                "painting": {
                    "model": "RGBA",
                    "depth": "U8",
                    "profile": SRGB_PROFILE,
                    "preset_engines": ["paintbrush"],
                    "requires_active_view": True,
                    "path_pressure_samples": False,
                    "nonzero_origin": False,
                    "completion": "nonblocking_tryBarrierLock",
                    "settings_restoration": "immediate_without_gui_yield",
                    "undo": "one_native_stroke_on_reference_build",
                },
                "preview": {
                    "max_edge": 1024,
                    "encoding": "image/png",
                    "color_conversion": "Krita_thumbnail_profile_unspecified",
                },
                "creation_limits": {
                    "open_documents": MAX_OPEN_DOCUMENTS,
                    "nodes_per_document": MAX_LAYER_NODES,
                },
                "input_roots": sorted(self.input_roots),
                "reference_editing": {
                    "paint_layers_only": True,
                    "max_region_pixels": 16777216,
                    "transform_undo": "not_guaranteed",
                    "evidence": "docs/validation.md",
                },
                "output_roots": sorted(self.output_roots),
                "ai_diffusion": {
                    **self._diffusion.capabilities(),
                    **self._diffusion_generator.capabilities(),
                },
            },
        }

    def execute(self, request):
        self._assert_gui_thread()
        if QApplication.activeModalWidget() is not None:
            raise BridgeError(
                "HOST_MODAL", "Close the active Krita modal dialog before using the bridge."
            )
        command = request["command"]
        if command == "generate_diffusion":
            return self._generate_diffusion(
                request["target"], request["params"], request["operation_id"]
            )
        # This map is intentionally fixed. There is no arbitrary method dispatch.
        handlers = {
            "activate_document": self._activate_document,
            "clear_selection": self._clear_selection,
            "get_region_preview": self._get_region_preview,
            "set_layer_properties": self._set_layer_properties,
            "copy_layer": self._copy_layer,
            "transform_layer": self._transform_layer,
            "move_layer": self._move_layer,
            "open_document": self._open_document,
            "import_image_layer": self._import_image_layer,
            "set_selection": self._set_selection,
            "paint_bezier_path": self._paint_bezier_path,
            "list_documents": self._list_documents,
            "inspect_document": self._inspect_document,
            "get_preview": self._get_preview,
            "list_brush_presets": self._list_brush_presets,
            "create_document": self._create_document,
            "create_paint_layer": self._create_paint_layer,
            "paint_path": self._paint_path,
            "paint_line": self._paint_line,
            "save_document": self._save_document,
            "export_png": self._export_png,
            "diffusion_status": self._diffusion_status,
            "inspect_diffusion_document": self._inspect_diffusion_document,
            "list_diffusion_jobs": self._list_diffusion_jobs,
            "list_diffusion_styles": self._list_diffusion_styles,
            "get_diffusion_generation": self._get_diffusion_generation,
            "get_diffusion_result": self._get_diffusion_result,
            "apply_diffusion_result": self._apply_diffusion_result,
        }
        if command not in handlers:
            raise BridgeError("UNKNOWN_COMMAND", "This host does not support the command.")
        return handlers[command](request.get("target", {}), request.get("params", {}))

    def _diffusion_status(self, target, params):
        return {**self._diffusion.status(), **self._diffusion_generator.capabilities()}

    def _list_diffusion_styles(self, target, params):
        return self._diffusion_generator.list_styles()

    def _diffusion_target(self, document_id):
        document = self._document(document_id)
        self._require_ready(document)
        self._writable_color(document)
        window = self.app.activeWindow()
        view = window.activeView() if window is not None else None
        if view is None or view.document() != document:
            raise BridgeError(
                "TARGET_NOT_ACTIVE", "Activate the requested document in Krita first."
            )
        if document.xOffset() != 0 or document.yOffset() != 0:
            raise BridgeError(
                "UNSUPPORTED_DOCUMENT", "AI generation requires a zero-offset canvas."
            )
        return document

    def _generate_diffusion(self, target, params, operation_id):
        document = self._diffusion_target(target["document_id"])
        try:
            result = self._diffusion_generator.generate(
                target["document_id"], document, params, operation_id
            )
        except BridgeError as exc:
            # Failed preparation may have restored hidden control layers and
            # scheduled a projection refresh even when its final effect is none.
            return Pending(self, target["document_id"], None, error=exc)
        # Preparing canvas input can hide/restore control layers and refresh the
        # projection. Settle that work; the separate generation may keep running.
        return Pending(self, target["document_id"], result)

    def _get_diffusion_generation(self, target, params):
        document = self._document(target["document_id"])
        return self._diffusion_generator.get_generation(
            target["document_id"], document, params["generation_id"]
        )

    def _get_diffusion_result(self, target, params):
        document = self._document(target["document_id"])
        return self._diffusion_generator.get_result(
            target["document_id"],
            document,
            params["generation_id"],
            params["result_id"],
            params["max_edge"],
        )

    def _apply_diffusion_result(self, target, params):
        document = self._diffusion_target(target["document_id"])
        self._require_layer_capacity(document)
        self._unlocked_ancestry(document.rootNode())
        try:
            result = self._diffusion_generator.apply_result(
                target["document_id"], document, params["generation_id"], params["result_id"]
            )
        except BridgeError as exc:
            if exc.effect == "none":
                raise
            return Pending(self, target["document_id"], None, error=exc)
        return Pending(self, target["document_id"], result)

    def _inspect_diffusion_document(self, target, params):
        document = self._document(target["document_id"])
        return {
            **self._diffusion.inspect_document(target["document_id"], document),
            **self._diffusion_generator.capabilities(),
        }

    def _list_diffusion_jobs(self, target, params):
        document = self._document(target["document_id"])
        return {
            **self._diffusion.list_jobs(
                target["document_id"], document, params.get("offset", 0), params.get("limit", 50)
            ),
            **self._diffusion_generator.capabilities(),
        }

    def _reconcile_documents(self):
        fresh = self.app.documents()
        registry = {}
        for document in fresh:
            handle = None
            for old_id, old in self._documents.items():
                try:
                    if old == document:
                        handle = old_id
                        break
                except RuntimeError:
                    pass
            registry[handle or ("doc-" + uuid.uuid4().hex)] = document
        self._documents = registry
        self._owned_documents = {
            handle: document
            for handle, document in self._owned_documents.items()
            if handle in registry
        }
        return registry

    def _register_owned_document(self, document):
        """Anchor a wrapper returned directly by our native create/open call."""
        handle = "doc-" + uuid.uuid4().hex
        self._documents[handle] = document
        self._owned_documents[handle] = document
        return handle

    def _document(self, document_id):
        document = self._reconcile_documents().get(document_id)
        if document is None:
            raise BridgeError(
                "TARGET_NOT_FOUND", "The document is no longer open in this bridge instance."
            )
        return document

    def _document_id(self, document):
        for handle, current in self._reconcile_documents().items():
            if current == document:
                return handle
        raise BridgeError(
            "TARGET_NOT_FOUND", "The document could not be registered.", effect="partial"
        )

    @staticmethod
    def _node(document, node_id):
        identifier = QUuid(node_id)
        node = None if identifier.isNull() else document.nodeByUniqueID(identifier)
        if node is None:
            raise BridgeError(
                "TARGET_NOT_FOUND", "The node does not belong to the requested document."
            )
        return node

    @staticmethod
    def _node_id(node):
        return node.uniqueId().toString(QUuid.StringFormat.WithoutBraces)

    @staticmethod
    def _is_ready(document):
        if not document.tryBarrierLock():
            return False
        document.unlock()
        return True

    def _require_ready(self, document):
        if not self._is_ready(document):
            raise BridgeError(
                "DOCUMENT_BUSY", "Krita is still processing this document; retry inspection later."
            )

    @staticmethod
    def _bounds(rect):
        return {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}

    def _metadata(self, handle, document):
        window = self.app.activeWindow()
        view = window.activeView() if window is not None else None
        return {
            "document_id": handle,
            "name": document.name(),
            "filename": document.fileName(),
            "width": document.width(),
            "height": document.height(),
            "bounds": self._bounds(document.bounds()),
            "offset": {"x": document.xOffset(), "y": document.yOffset()},
            "color_model": document.colorModel(),
            "color_depth": document.colorDepth(),
            "color_profile": document.colorProfile(),
            "modified": document.modified(),
            "active_view": view is not None and view.document() == document,
        }

    def _list_documents(self, target, params):
        return {
            "documents": [
                self._metadata(handle, doc) for handle, doc in self._reconcile_documents().items()
            ]
        }

    def _inspect_document(self, target, params):
        document = self._document(target["document_id"])
        self._require_ready(document)
        result = self._metadata(target["document_id"], document)
        selection = document.selection()
        result["selection"] = (
            None
            if selection is None
            else {
                "x": selection.x(),
                "y": selection.y(),
                "width": selection.width(),
                "height": selection.height(),
            }
        )
        result["current_frame"] = document.currentTime()
        active = document.activeNode()
        result["active_node_id"] = self._node_id(active) if active else None
        nodes = []
        stack = [(node, None) for node in reversed(document.topLevelNodes())]
        while stack:
            node, parent = stack.pop()
            if len(nodes) == MAX_LAYER_NODES:
                raise BridgeError(
                    "RESULT_TOO_LARGE", "This document exceeds the layer inspection limit."
                )
            handle = self._node_id(node)
            nodes.append(
                {
                    "node_id": handle,
                    "parent_node_id": parent,
                    "name": node.name(),
                    "type": node.type(),
                    "visible": node.visible(),
                    "locked": node.locked(),
                    "opacity": node.opacity(),
                    "animated": node.animated(),
                    "alpha_locked": node.alphaLocked(),
                    "inherit_alpha": node.inheritAlpha(),
                    "bounds": self._bounds(node.bounds()),
                }
            )
            stack.extend((child, handle) for child in reversed(node.childNodes()))
        result["layers"] = nodes
        return result

    def _get_preview(self, target, params):
        document = self._document(target["document_id"])
        self._require_ready(document)
        width, height = document.width(), document.height()
        if width <= 0 or height <= 0:
            raise BridgeError("INVALID_DOCUMENT", "Document dimensions are empty.")
        scale = min(1.0, params.get("max_edge", 1024) / max(width, height))
        preview_width, preview_height = max(1, round(width * scale)), max(1, round(height * scale))
        image = document.thumbnail(preview_width, preview_height)
        if image.isNull() or image.width() * image.height() > 1024 * 1024:
            raise BridgeError("PREVIEW_FAILED", "Krita did not return a bounded preview image.")
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise BridgeError("PREVIEW_FAILED", "Could not open the PNG memory buffer.")
        try:
            if not image.save(buffer, "PNG"):
                raise BridgeError("PREVIEW_FAILED", "Krita could not encode the preview PNG.")
        finally:
            buffer.close()
        artifact = self.artifacts.put(bytes(encoded), "image/png")
        return dict(
            artifact,
            document_id=target["document_id"],
            source_bounds=self._bounds(document.bounds()),
            source_offset={"x": document.xOffset(), "y": document.yOffset()},
            preview_width=image.width(),
            preview_height=image.height(),
            scale={"x": image.width() / width, "y": image.height() / height},
            color={
                "source_profile": document.colorProfile(),
                "source_model": document.colorModel(),
                "source_depth": document.colorDepth(),
                "preview_profile": image.colorSpace().description() or "unspecified",
                "conversion": "Krita thumbnail API",
                "alpha": "Krita thumbnail API",
            },
        )

    @staticmethod
    def _preset_signature(resource):
        xml = Preset(resource).toXML()
        return hashlib.sha256(
            (resource.filename() + "\0" + resource.name() + "\0" + xml).encode()
        ).hexdigest(), xml

    @staticmethod
    def _preset_engine(xml):
        try:
            root = ElementTree.fromstring(xml)
            return root.attrib.get("paintopid") or next(
                (node.text for node in root.iter() if node.attrib.get("name") == "paintopid"),
                "unknown",
            )
        except ElementTree.ParseError:
            return "unknown"

    def _list_brush_presets(self, target, params):
        resources = self.app.resources("preset")
        query = params.get("query", "").casefold()
        matching = sorted(
            ((key, value) for key, value in resources.items() if query in value.name().casefold()),
            key=lambda pair: (pair[1].name(), pair[0]),
        )
        offset, limit = params.get("offset", 0), params.get("limit", 50)
        presets = []
        for key, resource in matching[offset : offset + limit]:
            signature, xml = self._preset_signature(resource)
            existing = next(
                (handle for handle, record in self._presets.items() if record == (key, signature)),
                None,
            )
            if existing is None:
                if len(self._presets) >= 10000:
                    raise BridgeError(
                        "RESOURCE_LIMIT", "Preset identity limit reached; restart the bridge."
                    )
                existing = "preset-" + uuid.uuid4().hex
                self._presets[existing] = (key, signature)
            engine = self._preset_engine(xml)
            presets.append(
                {
                    "preset_id": existing,
                    "name": resource.name(),
                    "engine": engine,
                    "supported_for_painting": engine == "paintbrush",
                }
            )
        next_offset = offset + len(presets)
        return {
            "presets": presets,
            "total": len(matching),
            "offset": offset,
            "next_offset": next_offset if next_offset < len(matching) else None,
        }

    def _preset(self, preset_id):
        record = self._presets.get(preset_id)
        if record is None:
            raise BridgeError(
                "TARGET_NOT_FOUND", "List presets first and use a handle from this bridge instance."
            )
        key, signature = record
        resource = self.app.resources("preset").get(key)
        if resource is None:
            raise BridgeError(
                "TARGET_NOT_FOUND", "The requested brush preset is no longer installed."
            )
        actual_signature, xml = self._preset_signature(resource)
        if signature != actual_signature:
            raise BridgeError("RESOURCE_CHANGED", "The brush preset changed; list presets again.")
        if self._preset_engine(xml) != "paintbrush":
            raise BridgeError(
                "UNSUPPORTED_CAPABILITY",
                "Initial native painting supports the pixel brush engine only.",
            )
        return resource

    def _refresh_preset_after_bridge_change(self, preset_id, original_resource):
        """Account for synchronous setting writes to the preset we just validated.

        Krita's working Resource XML includes brush size/opacity. Selecting and
        restoring view controls mutates that XML even without a saved preset
        edit. This refresh is only called before yielding the GUI thread; a
        later external change still fails the next ordinary signature check.
        """
        key, _ = self._presets[preset_id]
        current = self.app.resources("preset").get(key)
        if current is None or current != original_resource:
            return
        signature, _ = self._preset_signature(current)
        self._presets[preset_id] = (key, signature)

    def _create_document(self, target, params):
        if len(self.app.documents()) >= MAX_OPEN_DOCUMENTS:
            raise BridgeError(
                "DOCUMENT_LIMIT",
                "Close an open document before creating another through the bridge.",
            )
        window = self.app.activeWindow()
        if window is None:
            raise BridgeError("NO_ACTIVE_WINDOW", "Open a Krita window before creating a document.")
        if SRGB_PROFILE not in self.app.profiles("RGBA", "U8"):
            raise BridgeError(
                "UNSUPPORTED_COLORSPACE", "The required standard sRGB profile is unavailable."
            )
        document = self.app.createDocument(
            params["width"], params["height"], params["name"], "RGBA", "U8", SRGB_PROFILE, 72.0
        )
        if document is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create the requested document.")
        # Native creation already registers the document, before it has a view.
        # Establish its handle and retain its owner before any fallible view call.
        handle = self._register_owned_document(document)
        result = {"document_id": handle}
        try:
            view = window.addView(document)
            if view is None:
                raise RuntimeError("The document was created without a view")
            window.showView(view)
            result = self._metadata(handle, document)
        except Exception:
            return Pending(
                self,
                handle,
                result,
                error=BridgeError(
                    "CREATE_FAILED",
                    "The document was created but initialization failed.",
                    effect="partial",
                ),
            )
        return Pending(self, handle, result)

    @staticmethod
    def _writable_color(document):
        if (document.colorModel(), document.colorDepth(), document.colorProfile()) != (
            "RGBA",
            "U8",
            SRGB_PROFILE,
        ):
            raise BridgeError(
                "UNSUPPORTED_COLORSPACE",
                "Painting requires RGBA/U8 with the standard sRGB profile.",
            )

    @staticmethod
    def _unlocked_ancestry(node):
        count = 0
        while node is not None:
            if node.locked():
                raise BridgeError("TARGET_LOCKED", "The target node or an ancestor is locked.")
            if not node.visible():
                raise BridgeError("TARGET_HIDDEN", "The target node or an ancestor is hidden.")
            node = node.parentNode()
            count += 1
            if count > MAX_LAYER_NODES:
                raise BridgeError(
                    "INVALID_DOCUMENT", "The layer hierarchy exceeds supported limits."
                )

    @staticmethod
    def _require_layer_capacity(document):
        stack = list(document.topLevelNodes())
        visited = 0
        while stack:
            if visited + len(stack) >= MAX_LAYER_NODES:
                raise BridgeError(
                    "LAYER_LIMIT", "This document has reached the bridge's layer and mask limit."
                )
            node = stack.pop()
            visited += 1
            stack.extend(node.childNodes())

    def _create_paint_layer(self, target, params):
        document = self._document(target["document_id"])
        self._require_ready(document)
        self._require_layer_capacity(document)
        self._writable_color(document)
        parent = (
            self._node(document, params["parent_node_id"])
            if params.get("parent_node_id")
            else document.rootNode()
        )
        if parent is None or parent.type() != "grouplayer":
            raise BridgeError(
                "INVALID_TARGET_TYPE", "Paint layers must be added to a group or document root."
            )
        self._unlocked_ancestry(parent)
        node = document.createNode(params["name"], "paintlayer")
        if node is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create the paint layer.")
        attached = False
        result = {"document_id": target["document_id"]}
        try:
            attached = parent.addChildNode(node, None)
            if not attached:
                return Pending(
                    self,
                    target["document_id"],
                    result,
                    error=BridgeError(
                        "CREATE_FAILED",
                        "Krita could not confirm attachment of the new paint layer.",
                        effect="unknown",
                    ),
                )
            # Record the attached node before activation, refresh, or metadata
            # can fail, so recovery can address the layer without recreating it.
            result["node_id"] = self._node_id(node)
            document.setActiveNode(node)
            document.refreshProjection()
            return Pending(
                self,
                target["document_id"],
                {
                    **result,
                    "name": node.name(),
                    "undo": "not_guaranteed",
                },
            )
        except Exception:
            # Attachment/refresh can start native work before an exception is
            # raised, including a later metadata failure. Retain the dispatch
            # and shutdown gate until the fresh document's barrier settles.
            return Pending(
                self,
                target["document_id"],
                result,
                error=BridgeError(
                    "CREATE_FAILED",
                    "The layer was attached but initialization failed."
                    if attached
                    else "Krita could not confirm attachment of the new paint layer.",
                    effect="partial" if attached else "unknown",
                ),
            )

    def _paint_targets(self, target, params, coordinates):
        document = self._document(target["document_id"])
        self._require_ready(document)
        window = self.app.activeWindow()
        view = window.activeView() if window is not None else None
        if view is None or view.document() != document:
            raise BridgeError(
                "TARGET_NOT_ACTIVE", "Activate the target document's view before painting."
            )
        self._writable_color(document)
        bounds = document.bounds()
        if bounds.x() or bounds.y() or document.xOffset() or document.yOffset():
            raise BridgeError(
                "UNSUPPORTED_ORIGIN", "Native painting currently requires a zero-offset canvas."
            )
        selection = document.selection()
        if selection is not None and selection.width() > 0 and selection.height() > 0:
            raise BridgeError(
                "UNSUPPORTED_SELECTION", "Clear the active selection before painting."
            )
        node = self._node(document, target["node_id"])
        if node.type() != "paintlayer" or node.animated():
            raise BridgeError(
                "INVALID_TARGET_TYPE", "Native painting requires a nonanimated paint layer."
            )
        self._writable_color(node)
        self._unlocked_ancestry(node)
        if node.alphaLocked() or node.inheritAlpha():
            raise BridgeError(
                "TARGET_LOCKED",
                "Disable the layer alpha lock and alpha inheritance before painting.",
            )
        for x, y in coordinates:
            if not (
                math.isfinite(x)
                and math.isfinite(y)
                and 0 <= x < document.width()
                and 0 <= y < document.height()
            ):
                raise BridgeError(
                    "OUT_OF_BOUNDS", "All painting coordinates must lie within the canvas."
                )
        preset = self._preset(params["preset_id"])
        return document, node, view, preset

    @staticmethod
    def _view_snapshot(view):
        return {
            "preset": view.currentBrushPreset(),
            "size": view.brushSize(),
            "opacity": view.paintingOpacity(),
            "flow": view.paintingFlow(),
            "rotation": view.brushRotation(),
            "blending": view.currentBlendingMode(),
            "eraser": view.eraserMode(),
            "alpha_lock": view.globalAlphaLock(),
            "disable_pressure": view.disablePressure(),
            "foreground": view.foregroundColor(),
        }

    @staticmethod
    def _restore_view(view, snapshot):
        view.setCurrentBrushPreset(snapshot["preset"])
        view.setBrushSize(snapshot["size"])
        view.setPaintingOpacity(snapshot["opacity"])
        view.setPaintingFlow(snapshot["flow"])
        view.setBrushRotation(snapshot["rotation"])
        view.setCurrentBlendingMode(snapshot["blending"])
        view.setEraserMode(snapshot["eraser"])
        view.setGlobalAlphaLock(snapshot["alpha_lock"])
        view.setDisablePressure(snapshot["disable_pressure"])
        view.setForeGroundColor(snapshot["foreground"])

    def _paint(self, target, params, coordinates, native_call):
        document, node, view, preset = self._paint_targets(target, params, coordinates)
        snapshot = self._view_snapshot(view)
        original_node = document.activeNode()
        dispatched = False
        failure = None
        try:
            document.setActiveNode(node)
            view.setCurrentBrushPreset(preset)
            view.setBrushSize(params["size_px"])
            view.setPaintingOpacity(params["opacity"])
            view.setPaintingFlow(1.0)
            view.setBrushRotation(0.0)
            view.setCurrentBlendingMode("normal")
            view.setEraserMode(False)
            view.setGlobalAlphaLock(False)
            view.setDisablePressure(False)
            # QColor is interpreted in standard sRGB by ManagedColor without a canvas.
            color = ManagedColor.fromQColor(QColor(params["color"]))
            if color is None or not color.setColorSpace("RGBA", "U8", SRGB_PROFILE):
                raise BridgeError(
                    "UNSUPPORTED_COLORSPACE", "Krita could not create a managed sRGB color."
                )
            view.setForeGroundColor(color)
            if (
                view.currentBrushPreset() != preset
                or view.eraserMode()
                or view.globalAlphaLock()
                or view.disablePressure()
                or view.currentBlendingMode() != "normal"
                or not math.isclose(view.paintingFlow(), 1.0, abs_tol=1e-6)
                or not math.isclose(view.brushRotation(), 0.0, abs_tol=1e-6)
                or not math.isclose(view.brushSize(), params["size_px"], abs_tol=0.01)
                or not math.isclose(view.paintingOpacity(), params["opacity"], abs_tol=0.005)
            ):
                raise BridgeError(
                    "UNSUPPORTED_BRUSH_STATE",
                    "Krita could not apply the required native brush settings.",
                )
            if node.paintAbility() != "PAINT":
                raise BridgeError(
                    "UNSUPPORTED_CAPABILITY",
                    "Krita reports this target cannot receive native painting.",
                )
            dispatched = True
            native_call(node)
        except BridgeError as error:
            failure = BridgeError(
                error.code, error.message, effect="unknown" if dispatched else error.effect
            )
        except Exception:
            failure = BridgeError(
                "NATIVE_PAINT_FAILED",
                "The native painting call failed.",
                effect="unknown" if dispatched else "none",
            )
        finally:
            try:
                # No event-loop yield occurs during this block. Native helpers must
                # capture resources before returning; the live-host gate verifies it.
                self._restore_view(view, snapshot)
                document.setActiveNode(original_node)
                self._refresh_preset_after_bridge_change(params["preset_id"], preset)
            except Exception:
                failure = BridgeError(
                    "RESTORE_FAILED",
                    "Krita could not restore the originating brush context.",
                    effect="unknown" if dispatched else "partial",
                )
        if failure is not None and not dispatched:
            raise failure
        return Pending(
            self,
            target["document_id"],
            {
                "document_id": target["document_id"],
                "node_id": target["node_id"],
                "native_strokes": 1,
                "undo": "one_native_stroke_on_reference_build",
                "settings_restored": True,
            },
            error=failure,
        )

    def _paint_path(self, target, params):
        path = QPainterPath(QPointF(*params["points"][0]))
        for point in params["points"][1:]:
            path.lineTo(QPointF(*point))
        return self._paint(
            target,
            params,
            params["points"],
            lambda node: node.paintPath(path, "ForegroundColor", "None"),
        )

    def _paint_line(self, target, params):
        return self._paint(
            target,
            params,
            [params["start"], params["end"]],
            lambda node: node.paintLine(
                QPoint(*params["start"]),
                QPoint(*params["end"]),
                params.get("pressure_start", 1.0),
                params.get("pressure_end", 1.0),
                "ForegroundColor",
            ),
        )

    def _write_file(self, target, params, export):
        document = self._document(target["document_id"])
        self._require_ready(document)
        path = resolve_output_path(
            self.output_roots,
            params["root"],
            params["path"],
            ".png" if export else ".kra",
            params.get("overwrite", False),
        )
        previous_batchmode = document.batchmode()
        try:
            document.setBatchmode(True)
            if export:
                config = InfoObject()
                config.setProperties(
                    {
                        "alpha": True,
                        "compression": 6,
                        "forceSRGB": True,
                        "saveSRGBProfile": True,
                        "indexed": False,
                    }
                )
                success = document.exportImage(str(path), config)
            else:
                success = document.saveAs(str(path))
            if not success or not path.is_file() or path.stat().st_size == 0:
                raise BridgeError(
                    "FILE_WRITE_FAILED",
                    "Krita did not produce a nonempty output file.",
                    effect="unknown",
                )
        except BridgeError:
            raise
        except Exception:
            raise BridgeError(
                "FILE_WRITE_FAILED",
                "Krita failed while writing the requested output.",
                effect="unknown",
            ) from None
        finally:
            document.setBatchmode(previous_batchmode)
        return {
            "document_id": target["document_id"],
            "root": params["root"],
            "path": params["path"],
            "size_bytes": path.stat().st_size,
            "filename": document.fileName(),
            "modified": document.modified(),
            "format": "png" if export else "kra",
        }

    def _save_document(self, target, params):
        return self._write_file(target, params, export=False)

    def _export_png(self, target, params):
        return self._write_file(target, params, export=True)
