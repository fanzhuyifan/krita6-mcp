"""GUI-only native vector editing with state-checked, top-level shape addressing."""

import hashlib
import json
import math

from .protocol_validation import BridgeError
from .vector_protocol import MAX_SHAPES, MAX_SVG_BYTES, geometry_points, shape_svg


class VectorEditingMixin:
    def _vector_target(self, target, *, mutation=False):
        document = self._editing_document(target["document_id"])
        self._editing_size(document.width(), document.height())
        node = self._node(document, target["node_id"])
        if node.type() != "vectorlayer" or node.animated():
            raise BridgeError("INVALID_TARGET_TYPE", "Choose a nonanimated vector layer.")
        if mutation:
            self._active_view(document)
            self._structure_node(document, target["node_id"], ("vectorlayer",))
            self._writable_color(document)
            if node.childNodes():
                raise BridgeError(
                    "INVALID_TARGET_TYPE", "Vector edits require a layer without masks."
                )
        for resolution in (document.xRes(), document.yRes()):
            if not math.isfinite(resolution) or not 1 <= resolution <= 1200:
                raise BridgeError("UNSUPPORTED_RESOLUTION", "Vector editing requires 1–1200 DPI.")
        if mutation and document.xRes() != document.yRes():
            raise BridgeError(
                "UNSUPPORTED_RESOLUTION",
                "Vector mutations currently require equal horizontal and vertical DPI.",
            )
        return document, node

    def _vector_snapshot(self, target, document, node):
        shapes = node.shapes()
        if len(shapes) > MAX_SHAPES:
            raise BridgeError("SIZE_LIMIT", "Vector layer exceeds 256 top-level shapes.")
        svg = node.toSvg()
        if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
            raise BridgeError("SIZE_LIMIT", "Vector layer SVG exceeds 256 KiB.")
        records = []
        for index, shape in enumerate(shapes):
            rect = shape.boundingBox()
            records.append(
                {
                    "shape_index": index,
                    "name": shape.name()[:128],
                    "type": shape.type(),
                    "visible": shape.visible(),
                    "geometry_protected": shape.geometryProtected(),
                    "z_index": shape.zIndex(),
                    "bounds_px": [
                        rect.x() * document.xRes() / 72,
                        rect.y() * document.yRes() / 72,
                        rect.width() * document.xRes() / 72,
                        rect.height() * document.yRes() / 72,
                    ],
                }
            )
        # Include state SVG can omit (visibility/protection) and explicit document
        # identity/geometry. No Shape wrapper survives this GUI-thread command.
        state = json.dumps(
            [
                target,
                document.width(),
                document.height(),
                document.xRes(),
                document.yRes(),
                records,
                svg,
            ],
            sort_keys=True,
            allow_nan=False,
        )
        snapshot = hashlib.sha256(state.encode()).hexdigest()
        return shapes, {
            **target,
            "snapshot_id": snapshot,
            "shapes": records,
            "addressing": "layer_state_snapshot_and_top_level_index",
        }

    def _inspect_vector_layer(self, target, params):
        document, node = self._vector_target(target)
        return self._vector_snapshot(target, document, node)[1]

    def _create_vector_layer(self, target, params):
        document = self._editing_document(target["document_id"])
        self._active_view(document)
        self._require_layer_capacity(document)
        parent, _ = self._editing_parent(document, params)
        node = document.createVectorLayer(params["name"])
        if node is None:
            raise BridgeError("CREATE_FAILED", "Krita could not create a vector layer.")
        return self._editing_mutate(
            target["document_id"],
            lambda: self._editing_steps([lambda: parent.addChildNode(node, None)]),
            {"node_id": self._node_id(node), "type": "vectorlayer"},
        )

    def _add_vector_shape(self, target, params):
        document, node = self._vector_target(target, mutation=True)
        shapes, _ = self._vector_snapshot(target, document, node)
        if len(shapes) >= MAX_SHAPES:
            raise BridgeError("SIZE_LIMIT", "Vector layer already contains 256 shapes.")
        if any(
            x > document.width() or y > document.height()
            for x, y in geometry_points(params["geometry"])
        ):
            raise BridgeError("OUT_OF_BOUNDS", "All vector geometry must lie within the canvas.")
        svg = shape_svg(
            params, document.width(), document.height(), document.xRes(), document.yRes()
        )
        # Native serialization expands compact geometry; reserve a full bounded
        # shape's output so a successful add does not make the layer uninspectable.
        if len(node.toSvg().encode()) + 65536 > MAX_SVG_BYTES:
            raise BridgeError("SIZE_LIMIT", "Adding a shape requires 64 KiB of SVG headroom.")
        result = {"node_id": target["node_id"]}

        def add():
            added = node.addShapesFromSvg(svg)
            if len(added) != 1:
                raise BridgeError(
                    "EDIT_FAILED",
                    "Krita did not return exactly one shape; inspect the layer.",
                    effect="unknown",
                )
            added[0].setName(params["name"])
            added[0].update()
            result["added_shapes"] = 1

        return self._editing_mutate(target["document_id"], add, result)

    def _resolve_vector_shape(self, target, params):
        document, node = self._vector_target(target, mutation=True)
        shapes, snapshot = self._vector_snapshot(target, document, node)
        if snapshot["snapshot_id"] != params["snapshot_id"] or params["shape_index"] >= len(shapes):
            raise BridgeError(
                "STALE_VECTOR_SNAPSHOT", "Reinspect the vector layer before editing a shape."
            )
        shape = shapes[params["shape_index"]]
        if shape.geometryProtected():
            raise BridgeError("TARGET_LOCKED", "The vector shape is geometry protected.")
        # Group descendants need their own lock/complexity semantics.
        if shape.type() != "KoPathShape":
            raise BridgeError(
                "INVALID_TARGET_TYPE",
                "Only top-level path shapes are editable; groups and text are unsupported.",
            )
        return document, shape

    def _edit_vector_shape(self, target, params):
        from PyQt6.QtGui import QTransform

        document, shape = self._resolve_vector_shape(target, params)
        transform_keys = {"translate_x", "translate_y", "scale_x", "scale_y", "rotation_degrees"}
        delta = None
        if params.keys() & transform_keys:
            # Conjugate the image-pixel transform into native point coordinates.
            to_pixels = QTransform.fromScale(document.xRes() / 72, document.yRes() / 72)
            pixel_delta = QTransform()
            pixel_delta.translate(params.get("translate_x", 0), params.get("translate_y", 0))
            pixel_delta.rotate(params.get("rotation_degrees", 0))
            pixel_delta.scale(params.get("scale_x", 1), params.get("scale_y", 1))
            delta = to_pixels * pixel_delta * to_pixels.inverted()[0]
            bounds = to_pixels.mapRect(delta.mapRect(shape.boundingBox()))
            values = (bounds.x(), bounds.y(), bounds.width(), bounds.height())
            if any(not math.isfinite(v) or abs(v) > 16384 for v in values):
                raise BridgeError(
                    "SIZE_LIMIT", "Transformed vector bounds exceed supported limits."
                )

        def edit():
            old_bounds = shape.boundingBox()
            if delta is not None:
                shape.setTransformation(shape.transformation() * delta)
            for key, method in (
                ("name", shape.setName),
                ("visible", shape.setVisible),
                ("z_index", shape.setZIndex),
            ):
                if key in params:
                    method(params[key])
            shape.updateAbsolute(old_bounds)
            shape.update()

        return self._editing_mutate(
            target["document_id"],
            edit,
            {"node_id": target["node_id"], "shape_index": params["shape_index"]},
        )

    def _delete_vector_shape(self, target, params):
        _, shape = self._resolve_vector_shape(target, params)
        return self._editing_mutate(
            target["document_id"],
            lambda: self._editing_steps([shape.remove]),
            {"node_id": target["node_id"], "removed_shape_index": params["shape_index"]},
        )

    def _merge_vector_layer_down(self, target, params):
        document, source = self._vector_target(target, mutation=True)
        siblings = source.parentNode().childNodes()
        index = next(i for i, candidate in enumerate(siblings) if candidate == source)
        if index == 0:
            raise BridgeError("INVALID_TARGET", "There is no lower sibling vector layer.")
        destination = siblings[index - 1]
        destination_target = {**target, "node_id": self._node_id(destination)}
        self._vector_target(destination_target, mutation=True)
        source_shapes, _ = self._vector_snapshot(target, document, source)
        destination_shapes, _ = self._vector_snapshot(destination_target, document, destination)
        if len(source_shapes) + len(destination_shapes) > MAX_SHAPES:
            raise BridgeError("SIZE_LIMIT", "Merged layer would exceed 256 shapes.")
        ancestor = source.parentNode()
        while ancestor is not None:
            if (
                ancestor.type() == "grouplayer"
                and ancestor != document.rootNode()
                and ancestor.passThroughMode()
            ):
                raise BridgeError(
                    "UNSUPPORTED_COMPOSITING", "Merge requires isolated ancestor groups."
                )
            ancestor = ancestor.parentNode()
        if source.isAntialiased() != destination.isAntialiased():
            raise BridgeError(
                "UNSUPPORTED_COMPOSITING", "Merge requires matching vector antialiasing settings."
            )
        for node in (source, destination):
            if (
                not node.visible()
                or node.opacity() != 255
                or node.blendingMode() != "normal"
                or node.inheritAlpha()
                or node.alphaLocked()
                or node.layerStyleToAsl()
            ):
                raise BridgeError(
                    "UNSUPPORTED_COMPOSITING",
                    "Merge requires visible, full-opacity normal layers without alpha inheritance, alpha lock, or layer styles.",
                )
        for shape in source_shapes + destination_shapes:
            if shape.type() != "KoPathShape" or shape.geometryProtected() or not shape.visible():
                raise BridgeError(
                    "INVALID_TARGET_TYPE",
                    "Merge supports only visible, unprotected top-level path shapes.",
                )
        svg = source.toSvg()
        if len(svg.encode()) + len(destination.toSvg().encode()) > MAX_SVG_BYTES - 8192:
            raise BridgeError("SIZE_LIMIT", "Merged SVG would exceed supported size.")
        top = max((s.zIndex() for s in destination_shapes), default=-1)
        if top + len(source_shapes) > 32767:
            raise BridgeError("SIZE_LIMIT", "Merged stacking order exceeds supported limits.")
        result = {
            "node_id": destination_target["node_id"],
            "removed_node_id": target["node_id"],
            "copied_shapes": 0,
        }

        def merge():
            if source_shapes:
                added = destination.addShapesFromSvg(svg)
                result["copied_shapes"] = len(added)
                if len(added) != len(source_shapes):
                    raise BridgeError(
                        "EDIT_FAILED",
                        "Incomplete vector copy; both layers retained. Inspect before continuing.",
                        effect="partial",
                    )
                for offset, (shape, original) in enumerate(zip(added, source_shapes), 1):
                    shape.setName(original.name())
                    shape.setZIndex(top + offset)
                    shape.update()
            # Only remove the source after all copied shapes were confirmed.
            if source.remove() is False:
                raise BridgeError(
                    "EDIT_FAILED",
                    "Shapes copied but source removal failed; inspect both layers.",
                    effect="partial",
                )

        return self._editing_mutate(target["document_id"], merge, result)
