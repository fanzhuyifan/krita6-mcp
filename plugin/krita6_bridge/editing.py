"""GUI-thread-only, bounded reference-editing operations.

Imported by host.py. Qt imports are local so guard tests can exercise pure
preconditions without loading another Qt runtime outside Krita.
"""

import sys
import time
from xml.etree import ElementTree
from zipfile import ZipFile

from .protocol import BridgeError
from .input_paths import resolve_input_path

MAX_PIXELS = 16777216
MAX_SIDE = 8192


class EditingMixin:
    def _editing_document(self, document_id):
        document = self._document(document_id)
        self._require_ready(document)
        if (
            document.xOffset()
            or document.yOffset()
            or document.bounds().x()
            or document.bounds().y()
        ):
            raise BridgeError(
                "UNSUPPORTED_ORIGIN", "Reference editing requires a zero-offset canvas."
            )
        return document

    @staticmethod
    def _editing_size(width, height):
        if width < 1 or height < 1 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
            raise BridgeError(
                "SIZE_LIMIT", "The region must fit within 8192 pixels per side and 16 megapixels."
            )

    def _editing_rect(self, document, x, y, width, height):
        self._editing_size(width, height)
        if x < 0 or y < 0 or x + width > document.width() or y + height > document.height():
            raise BridgeError("OUT_OF_BOUNDS", "The entire region must lie inside the canvas.")

    def _editing_node(self, document, node_id, *, pixels=False):
        node = self._node(document, node_id)
        if node.type() != "paintlayer" or node.animated() or node.childNodes():
            raise BridgeError(
                "INVALID_TARGET_TYPE", "Choose a nonanimated paint layer without masks or children."
            )
        ancestor = node
        count = 0
        while ancestor is not None:
            if ancestor.locked():
                raise BridgeError("TARGET_LOCKED", "The layer or an ancestor is locked.")
            ancestor = ancestor.parentNode()
            count += 1
            if count > 4096:
                raise BridgeError("INVALID_DOCUMENT", "Layer ancestry exceeds supported limits.")
        if pixels:
            self._writable_color(document)
            self._writable_color(node)
            if node.alphaLocked() or node.inheritAlpha():
                raise BridgeError(
                    "TARGET_LOCKED",
                    "Disable alpha lock and alpha inheritance before editing pixels.",
                )
        return node

    def _editing_parent(self, document, params):
        parent = (
            self._node(document, params["parent_node_id"])
            if params.get("parent_node_id")
            else document.rootNode()
        )
        if parent.type() != "grouplayer":
            raise BridgeError("INVALID_TARGET_TYPE", "The destination parent must be a group.")
        self._unlocked_ancestry(parent)
        above = (
            self._node(document, params["above_node_id"]) if params.get("above_node_id") else None
        )
        if above is not None and above.parentNode() != parent:
            raise BridgeError(
                "INVALID_TARGET", "The insertion sibling must belong to the destination parent."
            )
        return parent, above

    def _editing_mutate(self, document_id, callback, result):
        from .host import Pending

        document = self._document(document_id)
        failure = None
        try:
            callback()
        except BridgeError as error:
            failure = error
        except Exception:
            failure = BridgeError(
                "EDIT_FAILED",
                "The edit failed after dispatch; inspect the target before another edit.",
                effect="unknown",
            )
        # Direct pixel writes can partially succeed without dirtying the image.
        # Even on callback failure, make those pixels visible and retain the gate.
        try:
            document.setModified(True)
            document.refreshProjection()
        except Exception:
            if failure is None:
                failure = BridgeError(
                    "EDIT_FAILED",
                    "The edit completed but projection refresh failed; inspect the target.",
                    effect="partial",
                )
        return Pending(
            self,
            document_id,
            {"document_id": document_id, "undo": "not_guaranteed", **result},
            error=failure,
        )

    @staticmethod
    def _editing_steps(steps):
        """Track completed mutation phases without claiming rollback or atomicity."""
        completed = 0
        for step in steps:
            try:
                if step() is False:
                    raise RuntimeError("Native edit phase was not confirmed")
            except Exception as error:
                effect = (
                    "partial"
                    if completed
                    else (error.effect if isinstance(error, BridgeError) else "unknown")
                )
                raise BridgeError(
                    "EDIT_FAILED",
                    "An edit phase failed; inspect the known targets before continuing.",
                    effect=effect,
                ) from None
            completed += 1

    def _activate_document(self, target, params):
        from .host import Pending

        class ActivationPending(Pending):
            def poll(self):
                result = super().poll()
                if result is None:
                    return None
                current_window = self.host.app.activeWindow()
                current_view = current_window.activeView() if current_window is not None else None
                current_document = self.host._document(self.document_id)
                if current_view is not None and current_view.document() == current_document:
                    return result
                if time.monotonic() >= self.deadline:
                    raise BridgeError(
                        "ACTIVATION_FAILED",
                        "Krita did not make the requested document the active painting view.",
                        effect="unknown",
                    )
                return None

        document = self._document(target["document_id"])
        self._require_ready(document)
        # Prefer the active window's existing view; never manufacture another view.
        active = self.app.activeWindow()
        windows = ([active] if active else []) + [w for w in self.app.windows() if w != active]
        for window in windows:
            for view in window.views():
                if view.document() == document:
                    window.activate()
                    window.showView(view)
                    pending = ActivationPending(
                        self,
                        target["document_id"],
                        {
                            "document_id": target["document_id"],
                            "active_view": True,
                            "undo": "not_applicable",
                        },
                    )
                    pending.deadline = time.monotonic() + 2.0
                    return pending
        raise BridgeError("NO_DOCUMENT_VIEW", "The document has no open view to activate.")

    def _clear_selection(self, target, params):
        document = self._editing_document(target["document_id"])
        return self._editing_mutate(
            target["document_id"], lambda: document.setSelection(None), {"selection": None}
        )

    def _set_selection(self, target, params):
        from krita import Selection
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QImage, QPainter, QPolygon, QColor
        from PyQt6.QtCore import Qt

        document = self._editing_document(target["document_id"])
        selection = Selection()
        if params["shape"] == "rectangle":
            x, y, w, h = (params[k] for k in ("x", "y", "width", "height"))
            self._editing_rect(document, x, y, w, h)
            selection.select(x, y, w, h, 255)
        else:
            points = params["points"]
            x, y = min(p[0] for p in points), min(p[1] for p in points)
            w, h = max(p[0] for p in points) - x + 1, max(p[1] for p in points) - y + 1
            self._editing_rect(document, x, y, w, h)
            mask = QImage(w, h, QImage.Format.Format_ARGB32)
            mask.fill(0)
            painter = QPainter(mask)
            try:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("white"))
                painter.drawPolygon(
                    QPolygon([QPoint(px - x, py - y) for px, py in points]), Qt.FillRule.OddEvenFill
                )
            finally:
                painter.end()
            mask = mask.convertToFormat(QImage.Format.Format_Grayscale8)
            data = bytes(mask.constBits().asstring(mask.sizeInBytes()))
            stride = mask.bytesPerLine()
            packed = b"".join(data[row * stride : row * stride + w] for row in range(h))
            if not any(packed):
                raise BridgeError("INVALID_GEOMETRY", "The polygon must select at least one pixel.")
            selection.setPixelData(packed, x, y, w, h)
        return self._editing_mutate(
            target["document_id"],
            lambda: document.setSelection(selection),
            {
                "selection": {"x": x, "y": y, "width": w, "height": h},
                "shape": params["shape"],
                "mode": "replace",
            },
        )

    def _get_region_preview(self, target, params):
        from PyQt6.QtCore import Qt, QByteArray, QBuffer, QIODevice

        document = self._editing_document(target["document_id"])
        x, y, w, h = (params[k] for k in ("x", "y", "width", "height"))
        self._editing_rect(document, x, y, w, h)
        image = document.projection(x, y, w, h)
        if image.isNull() or image.width() != w or image.height() != h:
            raise BridgeError(
                "PREVIEW_FAILED", "Krita did not return the requested projection region."
            )
        edge = params["max_edge"]
        if max(w, h) > edge:
            image = image.scaled(
                edge,
                edge,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            if not image.save(buffer, "PNG"):
                raise BridgeError("PREVIEW_FAILED", "Krita could not encode the region preview.")
        finally:
            buffer.close()
        return {
            **self.artifacts.put(bytes(encoded), "image/png"),
            "document_id": target["document_id"],
            "source_bounds": {"x": x, "y": y, "width": w, "height": h},
            "source_offset": {"x": x, "y": y},
            "preview_width": image.width(),
            "preview_height": image.height(),
            "scale": {"x": image.width() / w, "y": image.height() / h},
            "color": {
                "source_profile": document.colorProfile(),
                "source_model": document.colorModel(),
                "source_depth": document.colorDepth(),
                "preview_profile": image.colorSpace().description() or "unspecified",
                "conversion": "Krita projection API",
                "alpha": "Krita projection API",
            },
        }

    def _set_layer_properties(self, target, params):
        document = self._editing_document(target["document_id"])
        node = self._editing_node(document, target["node_id"])
        actual = dict(params)
        if "opacity" in actual:
            actual["opacity"] = round(actual["opacity"] * 255)

        def change():
            self._editing_steps(
                [
                    (lambda setter=setter, value=actual[key]: setter(value))
                    for key, setter in (
                        ("name", node.setName),
                        ("visible", node.setVisible),
                        ("opacity", node.setOpacity),
                    )
                    if key in actual
                ]
            )

        return self._editing_mutate(
            target["document_id"], change, {"node_id": target["node_id"], **actual}
        )

    def _copy_layer(self, target, params):
        source = self._editing_document(target["document_id"])
        node = self._editing_node(source, target["node_id"], pixels=True)
        destination = self._editing_document(params["destination_document_id"])
        self._writable_color(destination)
        self._require_layer_capacity(destination)
        bounds = node.bounds()
        if not bounds.isEmpty():
            self._editing_size(bounds.width(), bounds.height())
        parent, above = self._editing_parent(destination, params)
        copied = node.duplicate()
        if copied is None:
            raise BridgeError("COPY_FAILED", "Krita could not duplicate the paint layer.")
        copied.setName(params["name"])
        result = {
            "source_document_id": target["document_id"],
            "source_node_id": target["node_id"],
            "node_id": self._node_id(copied),
        }

        def attach():
            self._editing_steps(
                (
                    lambda: parent.addChildNode(copied, above),
                    lambda: destination.setActiveNode(copied),
                )
            )

        return self._editing_mutate(params["destination_document_id"], attach, result)

    def _move_layer(self, target, params):
        document = self._editing_document(target["document_id"])
        node = self._editing_node(document, target["node_id"])
        parent, above = self._editing_parent(document, params)
        if above == node:
            raise BridgeError("INVALID_TARGET", "A layer cannot be inserted above itself.")

        old_parent = node.parentNode()
        if old_parent is None:
            raise BridgeError("INVALID_TARGET", "The layer has no parent to move from.")

        def verify_position():
            siblings = parent.childNodes()
            if node not in siblings or node.parentNode() != parent:
                raise RuntimeError("Layer did not reach its destination")
            if above is not None and siblings.index(node) != siblings.index(above) + 1:
                raise RuntimeError("Layer did not reach the requested sibling position")
            if above is None and siblings[-1] != node:
                raise RuntimeError("Layer did not reach the top of its group")

        def move():
            # addChildNode alone silently leaves already-attached nodes in place.
            # Retain the wrapper during explicit detach/reinsert on the GUI thread.
            self._editing_steps(
                (
                    lambda: old_parent.removeChildNode(node),
                    lambda: parent.addChildNode(node, above),
                    verify_position,
                )
            )

        return self._editing_mutate(
            target["document_id"],
            move,
            {
                "node_id": target["node_id"],
                "parent_node_id": params.get("parent_node_id"),
                "above_node_id": params.get("above_node_id"),
            },
        )

    @staticmethod
    def _image_bytes(image):
        from PyQt6.QtGui import QImage

        if sys.byteorder != "little":
            raise BridgeError(
                "UNSUPPORTED_HOST", "Raw RGBA/U8 editing is validated on little-endian hosts only."
            )
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        return bytes(image.constBits().asstring(image.sizeInBytes()))

    def _transform_layer(self, target, params):
        from PyQt6.QtCore import QRectF, Qt
        from PyQt6.QtGui import QImage, QPainter, QTransform

        document = self._editing_document(target["document_id"])
        node = self._editing_node(document, target["node_id"], pixels=True)
        bounds = node.bounds()
        if bounds.isEmpty():
            raise BridgeError("EMPTY_LAYER", "The layer has no pixels to transform.")
        self._editing_size(bounds.width(), bounds.height())
        if sys.byteorder != "little":
            raise BridgeError(
                "UNSUPPORTED_HOST", "Raw RGBA/U8 editing requires little-endian pixel layout."
            )
        px, py = params["pivot"]
        transform = QTransform()
        transform.translate(px + params["translate_x"], py + params["translate_y"])
        transform.rotate(params["rotation_degrees"])
        transform.scale(params["scale_x"], params["scale_y"])
        transform.translate(-px, -py)
        output = transform.mapRect(QRectF(bounds)).toAlignedRect()
        self._editing_rect(document, output.x(), output.y(), output.width(), output.height())
        raw = bytes(node.pixelData(bounds.x(), bounds.y(), bounds.width(), bounds.height()))
        if len(raw) != bounds.width() * bounds.height() * 4:
            raise BridgeError("PIXEL_READ_FAILED", "Krita returned an unexpected pixel buffer.")
        source = QImage(
            raw, bounds.width(), bounds.height(), bounds.width() * 4, QImage.Format.Format_ARGB32
        )
        image = QImage(output.width(), output.height(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(-output.x(), -output.y())
            painter.setTransform(transform, True)
            painter.drawImage(bounds.x(), bounds.y(), source)
        finally:
            painter.end()
        transformed = self._image_bytes(image)
        cleared = bytes(len(raw))

        def replace():
            self._editing_steps(
                (
                    lambda: node.setPixelData(
                        cleared, bounds.x(), bounds.y(), bounds.width(), bounds.height()
                    ),
                    lambda: node.setPixelData(
                        transformed, output.x(), output.y(), output.width(), output.height()
                    ),
                )
            )

        return self._editing_mutate(
            target["document_id"],
            replace,
            {
                "node_id": target["node_id"],
                "output_bounds": self._bounds(output),
                "resampling": "Qt_smooth",
                "transform_order": "scale_then_clockwise_rotate_about_pivot_then_translate",
            },
        )

    def _read_input_image(self, path):
        from PyQt6.QtGui import QImageReader, QColorSpace, QImage

        reader = QImageReader(str(path))
        if bytes(reader.format()).lower() not in {b"png", b"jpeg"}:
            raise BridgeError("UNSUPPORTED_FORMAT", "Only PNG and JPEG images are supported.")
        size = reader.size()
        self._editing_size(size.width(), size.height())
        image = reader.read()
        if image.isNull():
            raise BridgeError("FILE_READ_FAILED", "The image could not be decoded.")
        srgb = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
        if image.colorSpace().isValid():
            image = image.convertedToColorSpace(srgb)
        else:
            image.setColorSpace(srgb)
        return image.convertToFormat(QImage.Format.Format_ARGB32)

    def _import_image_layer(self, target, params):
        document = self._editing_document(target["document_id"])
        self._writable_color(document)
        self._require_layer_capacity(document)
        self._unlocked_ancestry(document.rootNode())
        path = resolve_input_path(
            self.input_roots,
            params["root"],
            params["path"],
            {".png", ".jpg", ".jpeg"},
            max_bytes=32 * 1024 * 1024,
        )
        image = self._read_input_image(path)
        self._editing_rect(document, params["x"], params["y"], image.width(), image.height())
        raw = self._image_bytes(image)
        node = document.createNode(params["name"], "paintlayer")
        if node is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create the imported layer.")

        def attach():
            self._editing_steps(
                (
                    lambda: document.rootNode().addChildNode(node, None),
                    lambda: node.setPixelData(
                        raw, params["x"], params["y"], image.width(), image.height()
                    ),
                    lambda: document.setActiveNode(node),
                )
            )

        return self._editing_mutate(
            target["document_id"],
            attach,
            {
                "node_id": self._node_id(node),
                "name": params["name"],
                "width": image.width(),
                "height": image.height(),
                "x": params["x"],
                "y": params["y"],
                "color_conversion": "embedded_profile_to_sRGB_or_assume_sRGB",
            },
        )

    def _open_document(self, target, params):
        from .host import Pending

        if len(self.app.documents()) >= 32:
            raise BridgeError(
                "DOCUMENT_LIMIT", "Close a document before opening another through the bridge."
            )
        window = self.app.activeWindow()
        if window is None:
            raise BridgeError("NO_ACTIVE_WINDOW", "Open a Krita window first.")
        path = resolve_input_path(
            self.input_roots,
            params["root"],
            params["path"],
            {".png", ".jpg", ".jpeg", ".kra"},
            max_bytes=64 * 1024 * 1024,
        )
        if path.suffix.lower() == ".kra":
            try:
                with ZipFile(path) as archive:
                    if archive.getinfo("maindoc.xml").file_size > 2 * 1024 * 1024:
                        raise ValueError("Manifest too large")
                    manifest = archive.read("maindoc.xml")
                    tree = ElementTree.fromstring(manifest)
                    image = next(e for e in tree.iter() if e.tag.rsplit("}", 1)[-1] == "IMAGE")
                    self._editing_size(int(image.attrib["width"]), int(image.attrib["height"]))
                    if sum(1 for e in tree.iter() if e.tag.rsplit("}", 1)[-1] == "layer") > 4096:
                        raise ValueError("Too many layers")
            except BridgeError:
                raise
            except Exception:
                raise BridgeError(
                    "INVALID_FILE", "The KRA file has no supported bounded document manifest."
                ) from None
        else:
            self._read_input_image(path)
        previous_batch = self.app.batchmode()
        handle = None
        restore_failed = False
        try:
            self.app.setBatchmode(True)
            document = self.app.openDocument(str(path))
            if document is not None:
                handle = self._register_owned_document(document)
        finally:
            try:
                self.app.setBatchmode(previous_batch)
            except Exception:
                if handle is None:
                    raise
                restore_failed = True
        if document is None:
            raise BridgeError(
                "FILE_READ_FAILED", "Krita could not open the document.", effect="unknown"
            )
        if restore_failed:
            return Pending(
                self,
                handle,
                {"document_id": handle},
                error=BridgeError(
                    "OPEN_FAILED",
                    "The file opened but batch mode could not be restored.",
                    effect="partial",
                ),
            )
        try:
            view = window.addView(document)
            if view is None:
                raise RuntimeError("No document view")
            window.showView(view)
            result = self._metadata(handle, document)
        except Exception:
            return Pending(
                self,
                handle,
                {"document_id": handle},
                error=BridgeError(
                    "OPEN_FAILED",
                    "The file opened but its view could not be initialized.",
                    effect="partial",
                ),
            )
        return Pending(self, handle, result)

    def _paint_bezier_path(self, target, params):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QPainterPath

        path = QPainterPath(QPointF(*params["start"]))
        coordinates = [params["start"]]
        for control1, control2, end in params["segments"]:
            path.cubicTo(QPointF(*control1), QPointF(*control2), QPointF(*end))
            coordinates.extend((control1, control2, end))
        return self._paint(
            target,
            params,
            coordinates,
            lambda node: node.paintPath(path, "ForegroundColor", "None"),
        )
