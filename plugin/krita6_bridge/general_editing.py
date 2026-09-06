"""Bounded GUI-thread editing; native operations settle through the existing barrier."""

import math
import sys
from array import array

from .protocol import BridgeError


def flood_coverage(raw, width, height, mask, seed):
    """Exact four-connected flood coverage inside a bounded selection."""
    if not mask[seed]:
        raise BridgeError("SEED_OUTSIDE_SELECTION", "Flood seed must be inside the selection.")
    match = raw[seed * 4 : seed * 4 + 4]
    visited = bytearray(width * height)
    stack = array("I", [seed])
    visited[seed] = 1
    while stack:
        pos = stack.pop()
        x = pos % width
        for neighbor in (
            (pos - 1 if x else -1),
            (pos + 1 if x + 1 < width else -1),
            pos - width,
            pos + width,
        ):
            if (
                0 <= neighbor < width * height
                and not visited[neighbor]
                and mask[neighbor]
                and raw[neighbor * 4 : neighbor * 4 + 4] == match
            ):
                visited[neighbor] = 1
                stack.append(neighbor)
    return bytes(a if visited[i] else 0 for i, a in enumerate(mask))


class GeneralEditingMixin:
    def _active_view(self, document):
        window = self.app.activeWindow()
        view = window.activeView() if window else None
        if view is None or view.document() != document:
            raise BridgeError("TARGET_NOT_ACTIVE", "Activate the requested document first.")
        return view

    def _structure_node(
        self, document, identifier, allowed=("paintlayer", "grouplayer", "transparencymask")
    ):
        node = self._node(document, identifier)
        if node == document.rootNode() or node.type() not in allowed or node.animated():
            raise BridgeError(
                "INVALID_TARGET_TYPE", "Choose a supported, nonanimated layer or mask."
            )
        ancestor, count = node, 0
        while ancestor is not None:
            if ancestor.locked():
                raise BridgeError("TARGET_LOCKED", "The layer or an ancestor is locked.")
            count += 1
            if count > 4096:
                raise BridgeError("INVALID_DOCUMENT", "Layer ancestry exceeds supported limits.")
            ancestor = ancestor.parentNode()
        return node

    def _bounded_subtree(self, node):
        stack, nodes = [node], []
        while stack:
            item = stack.pop()
            nodes.append(item)
            if len(nodes) > 4096:
                raise BridgeError("SIZE_LIMIT", "Subtree exceeds 4096 nodes.")
            if item.locked() or item.animated():
                raise BridgeError("TARGET_LOCKED", "Subtree contains locked or animated nodes.")
            stack.extend(item.childNodes())
        return nodes

    def _create_group_layer(self, target, params):
        document = self._editing_document(target["document_id"])
        self._require_layer_capacity(document)
        parent, _ = self._editing_parent(document, params)
        node = document.createGroupLayer(params["name"])
        if node is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create the group.")
        result = {"node_id": self._node_id(node), "type": "grouplayer"}
        return self._editing_mutate(
            target["document_id"],
            lambda: self._editing_steps([lambda: parent.addChildNode(node, None)]),
            result,
        )

    def _mask_selection(self, document, source):
        from krita import Selection

        self._editing_size(document.width(), document.height())
        if source == "selection":
            selection = document.selection()
            if selection is None or selection.width() == 0 or selection.height() == 0:
                raise BridgeError("SELECTION_REQUIRED", "Create a nonempty selection first.")
            self._editing_rect(
                document, selection.x(), selection.y(), selection.width(), selection.height()
            )
            selection = selection.duplicate()
        else:
            selection = Selection()
            selection.select(
                0, 0, document.width(), document.height(), 255 if source == "opaque" else 0
            )
        return selection

    def _create_transparency_mask(self, target, params):
        document = self._editing_document(target["document_id"])
        parent = self._structure_node(document, target["node_id"], ("paintlayer", "grouplayer"))
        self._require_layer_capacity(document)
        selection = self._mask_selection(document, params["source"])
        node = document.createTransparencyMask(params["name"])
        if node is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create the transparency mask.")
        node.setSelection(selection)
        return self._editing_mutate(
            target["document_id"],
            lambda: self._editing_steps([lambda: parent.addChildNode(node, None)]),
            {
                "node_id": self._node_id(node),
                "parent_node_id": target["node_id"],
                "type": "transparencymask",
            },
        )

    def _set_transparency_mask(self, target, params):
        document = self._editing_document(target["document_id"])
        node = self._structure_node(document, target["node_id"], ("transparencymask",))
        selection = self._mask_selection(document, params["source"])
        return self._editing_mutate(
            target["document_id"],
            lambda: node.setSelection(selection),
            {"node_id": target["node_id"], "source": params["source"]},
        )

    def _delete_layer(self, target, params):
        document = self._editing_document(target["document_id"])
        node = self._structure_node(document, target["node_id"])
        removed = [self._node_id(n) for n in self._bounded_subtree(node)]
        if node.parentNode() == document.rootNode() and len(document.topLevelNodes()) <= 1:
            raise BridgeError("LAST_LAYER", "Keep at least one top-level layer.")
        return self._editing_mutate(
            target["document_id"],
            lambda: self._editing_steps([node.remove]),
            {"removed_node_ids": removed},
        )

    def _merge_layer_down(self, target, params):
        document = self._editing_document(target["document_id"])
        node = self._editing_node(document, target["node_id"])
        parent = node.parentNode()
        siblings = parent.childNodes()
        index = siblings.index(node)
        if index == 0:
            raise BridgeError("INVALID_TARGET", "There is no layer below the target.")
        below = self._editing_node(document, self._node_id(siblings[index - 1]))
        self._writable_color(document)
        for layer in (node, below):
            if not layer.visible() or layer.inheritAlpha():
                raise BridgeError(
                    "INVALID_TARGET", "Merge requires two visible layers without alpha inheritance."
                )
        before_ids = [self._node_id(n) for n in siblings]
        result = {"merged_node_ids": [self._node_id(below), target["node_id"]]}

        def merge():
            # Krita 6.0.3 can return None after a successful native merge because
            # it queries the removed source node's previous sibling. Reconcile
            # the actual parent instead; mergeDown waits for its native work.
            node.mergeDown()
            after = parent.childNodes()
            after_ids = [self._node_id(n) for n in after]
            if (
                len(after_ids) != len(before_ids) - 1
                or after_ids[: index - 1] != before_ids[: index - 1]
                or after_ids[index:] != before_ids[index + 1 :]
                or after_ids[index - 1] in before_ids
                or after[index - 1].type() != "paintlayer"
            ):
                raise BridgeError(
                    "MERGE_FAILED", "Could not reconcile the merged layer tree.", effect="unknown"
                )
            result["node_id"] = after_ids[index - 1]

        # Result is filled synchronously before _editing_mutate constructs its Pending.
        return self._editing_mutate(target["document_id"], merge, result)

    def _edit_history(self, target, params):
        document = self._editing_document(target["document_id"])
        self._active_view(document)
        action = self.app.action("edit_" + params["direction"])
        if action is None or not action.isEnabled():
            raise BridgeError("HISTORY_UNAVAILABLE", "No enabled history step in that direction.")
        from .host import Pending

        label = action.text()[:256]
        action.trigger()
        return Pending(
            self,
            target["document_id"],
            {
                "document_id": target["document_id"],
                "direction": params["direction"],
                "steps": 1,
                "label": label,
                "scope": "active_document_history_including_user_edits",
            },
        )

    def _modify_selection(self, target, params):
        from krita import Selection

        document = self._editing_document(target["document_id"])
        self._editing_size(document.width(), document.height())
        existing = document.selection()
        if existing is None or existing.width() == 0 or existing.height() == 0:
            raise BridgeError("SELECTION_REQUIRED", "Create a nonempty selection first.")
        self._editing_rect(
            document, existing.x(), existing.y(), existing.width(), existing.height()
        )
        selection = existing.duplicate()
        canvas = Selection()
        canvas.select(0, 0, document.width(), document.height(), 255)
        action = params["action"]
        if action == "invert":
            canvas.subtract(selection)
            selection = canvas
        else:
            radius = params["radius"]
            if action == "grow":
                selection.grow(radius, radius)
            elif action == "shrink":
                selection.shrink(radius, radius, False)
            else:
                selection.feather(radius)
            selection.intersect(canvas)
        return self._editing_mutate(
            target["document_id"],
            lambda: document.setSelection(selection),
            {"action": action, "clipped_to_canvas": True},
        )

    def _transform_canvas(self, target, params):
        document = self._editing_document(target["document_id"])
        self._editing_size(document.width(), document.height())
        self._bounded_subtree(document.rootNode())
        action = params["action"]
        if action == "crop":
            self._editing_rect(
                document, params["x"], params["y"], params["width"], params["height"]
            )

            def change():
                return document.crop(params["x"], params["y"], params["width"], params["height"])
        elif action == "resize":

            def change():
                return document.resizeImage(
                    params["x"], params["y"], params["width"], params["height"]
                )
        elif action == "scale":

            def change():
                return document.scaleImage(
                    params["width"],
                    params["height"],
                    round(document.xRes()),
                    round(document.yRes()),
                    params["filter"],
                )
        elif action == "rotate":

            def change():
                return document.rotateImage(math.radians(params["degrees"]))
        else:
            self._active_view(document)
            native = self.app.action(
                "mirrorImageHorizontal" if params["axis"] == "horizontal" else "mirrorImageVertical"
            )
            if native is None or not native.isEnabled():
                raise BridgeError("UNSUPPORTED_CAPABILITY", "Image flip is unavailable.")
            change = native.trigger
        return self._editing_mutate(
            target["document_id"], change, {"action": action, "parameters": params}
        )

    def _paint_shape(self, target, params):
        from PyQt6.QtCore import QRectF

        x, y, w, h = (params[k] for k in ("x", "y", "width", "height"))
        # Rectangle extents must fit; corner at the exclusive edge is valid geometry.
        document = self._editing_document(target["document_id"])
        self._editing_rect(document, x, y, w, h)
        rect = QRectF(x, y, w, h)

        def paint(node):
            method = node.paintRectangle if params["shape"] == "rectangle" else node.paintEllipse
            method(rect, "ForegroundColor", "ForegroundColor" if params["fill"] else "None")

        return self._paint(target, params, [(x, y), (x + w - 1, y + h - 1)], paint)

    def _rgba_document(self, target):
        document = self._editing_document(target["document_id"])
        self._editing_size(document.width(), document.height())
        self._writable_color(document)
        if sys.byteorder != "little":
            raise BridgeError(
                "UNSUPPORTED_HOST", "Raw color operations require little-endian RGBA/U8."
            )
        return document

    def _fill_layer(self, target, params):
        from PyQt6.QtGui import QImage, QPainter, QColor, QLinearGradient

        document = self._rgba_document(target)
        node = self._editing_node(document, target["node_id"], pixels=True)
        w, h = document.width(), document.height()
        if w * h > 1048576:
            raise BridgeError("SIZE_LIMIT", "Raster fills are limited to one megapixel.")
        for key in ("point", "start", "end"):
            if key in params:
                self._editing_rect(document, *params[key], 1, 1)
        raw = bytes(node.pixelData(0, 0, w, h))
        if len(raw) != w * h * 4:
            raise BridgeError("PIXEL_READ_FAILED", "Unexpected layer pixel buffer.")
        source = QImage(raw, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        overlay = QImage(w, h, QImage.Format.Format_ARGB32)
        first = QColor(params.get("color", "#FFFFFF"))
        first.setAlphaF(params["opacity"])
        overlay.fill(first)
        if params["kind"] == "linear_gradient":
            gradient = QLinearGradient(*params["start"], *params["end"])
            last = QColor(params["end_color"])
            last.setAlphaF(params["opacity"])
            gradient.setColorAt(0, first)
            gradient.setColorAt(1, last)
            painter = QPainter(overlay)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(overlay.rect(), gradient)
            painter.end()
        selected = document.selection()
        mask = (
            bytes(selected.pixelData(0, 0, w, h))
            if selected is not None
            else bytes([255]) * (w * h)
        )
        if len(mask) != w * h:
            raise BridgeError("PIXEL_READ_FAILED", "Unexpected selection buffer.")
        if params["kind"] == "flood":
            seed = params["point"][1] * w + params["point"][0]
            if not mask[seed]:
                raise BridgeError(
                    "SEED_OUTSIDE_SELECTION", "Flood seed must be inside the selection."
                )
            mask = flood_coverage(raw, w, h, mask, seed)
        pixels = bytearray(self._image_bytes(overlay))
        for i, alpha in enumerate(mask):
            pixels[4 * i + 3] = (pixels[4 * i + 3] * alpha + 127) // 255
        overlay = QImage(bytes(pixels), w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        painter = QPainter(source)
        if params["kind"] == "erase":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.drawImage(0, 0, overlay)
        painter.end()
        output = self._image_bytes(source)
        return self._editing_mutate(
            target["document_id"],
            lambda: node.setPixelData(output, 0, 0, w, h),
            {
                "node_id": target["node_id"],
                "kind": params["kind"],
                "compositing": "destination_out" if params["kind"] == "erase" else "source_over",
                "flood_match": "exact_BGRA_4_connected" if params["kind"] == "flood" else None,
            },
        )

    def _sample_color(self, target, params):
        document = self._rgba_document(target)
        self._editing_rect(document, params["x"], params["y"], 1, 1)
        node = self._node(document, params["node_id"]) if "node_id" in params else None
        if node is not None:
            self._writable_color(node)
        raw = bytes(
            node.projectionPixelData(params["x"], params["y"], 1, 1)
            if node
            else document.pixelData(params["x"], params["y"], 1, 1)
        )
        if len(raw) != 4:
            raise BridgeError("PIXEL_READ_FAILED", "Unexpected pixel buffer.")
        b, g, r, a = raw
        return {
            "document_id": target["document_id"],
            "node_id": params.get("node_id"),
            "color": f"#{r:02X}{g:02X}{b:02X}",
            "alpha": a / 255,
            "rgba": [r, g, b, a],
            "profile": document.colorProfile(),
            "x": params["x"],
            "y": params["y"],
        }

    def _get_layer_preview(self, target, params):
        from PyQt6.QtCore import Qt, QByteArray, QBuffer, QIODevice
        from PyQt6.QtGui import QImage

        document = self._rgba_document(target)
        node = self._node(document, target["node_id"])
        self._writable_color(node)
        w, h = document.width(), document.height()
        raw = bytes(node.projectionPixelData(0, 0, w, h))
        if len(raw) != w * h * 4:
            raise BridgeError("PIXEL_READ_FAILED", "Unexpected layer projection.")
        image = QImage(raw, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        if max(w, h) > params["max_edge"]:
            image = image.scaled(
                params["max_edge"],
                params["max_edge"],
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            if not image.save(buffer, "PNG"):
                raise BridgeError("PREVIEW_FAILED", "Could not encode layer PNG.")
        finally:
            buffer.close()
        return {
            **self.artifacts.put(bytes(encoded), "image/png"),
            "document_id": target["document_id"],
            "node_id": target["node_id"],
            "source_bounds": {"x": 0, "y": 0, "width": w, "height": h},
            "preview_width": image.width(),
            "preview_height": image.height(),
            "profile": document.colorProfile(),
            "scope": "node_projection_in_canvas_bounds",
        }

    def _inspect_brush(self, target, params):
        document = self._document(target["document_id"])
        self._require_ready(document)
        view = self._active_view(document)
        color = view.foregroundColor()
        qcolor = color.colorForCanvas(None)
        preset = view.currentBrushPreset()
        return {
            "document_id": target["document_id"],
            "preset_name": preset.name()[:128] if preset else None,
            "size_px": view.brushSize(),
            "opacity": view.paintingOpacity(),
            "flow": view.paintingFlow(),
            "rotation_degrees": view.brushRotation(),
            "blending_mode": view.currentBlendingMode(),
            "eraser": view.eraserMode(),
            "alpha_lock": view.globalAlphaLock(),
            "pressure_enabled": not view.disablePressure(),
            "foreground": qcolor.name(),
            "foreground_alpha": qcolor.alphaF(),
            "foreground_conversion": "Krita colorForCanvas without canvas",
        }
