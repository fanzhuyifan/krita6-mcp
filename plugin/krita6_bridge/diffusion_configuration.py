"""GUI-only configuration of the source-pinned add-on's existing document model."""

from .diffusion_generation import _module
from .protocol import BridgeError


class DiffusionConfigurator:
    def __init__(self, generator):
        self.generator = generator

    def capabilities(self):
        try:
            self.generator._context()
        except BridgeError as error:
            return {"configuration_control": False, "configuration_unavailable_reason": error.code}
        return {
            "configuration_control": True,
            "read_only": False,
            "configuration_scope": "generation_root_and_linked_regions",
            "configuration_requires_backend": False,
        }

    @staticmethod
    def _region(model, layer):
        regions = list(model.regions)
        if len(regions) > 32:
            raise BridgeError("DIFFUSION_SIZE_LIMIT", "At most 32 regions are supported.")
        identifier = layer.id.toString()
        matches = [r for r in regions if identifier in r.layer_ids.split(",")]
        if len(matches) > 1:
            raise BridgeError("DIFFUSION_REGION_AMBIGUOUS", "Layer is linked to multiple regions.")
        return matches[0] if matches else None

    @staticmethod
    def _layer(model, identifier, region=False):
        from PyQt6.QtCore import QUuid

        uid = QUuid(identifier)
        if uid.isNull():
            raise BridgeError("NODE_NOT_FOUND", "Layer handle is not a native node UUID.")
        layer = model.layers.updated().find(uid)
        if layer is None or layer.is_root:
            raise BridgeError("NODE_NOT_FOUND", "Layer is not in the target document.")
        if not layer.type.is_image or (region and layer.type.name not in {"paint", "group"}):
            raise BridgeError("UNSUPPORTED_NODE", "Layer cannot be used for this conditioning.")
        return layer

    def configure(self, command, document_id, document, params):
        _, model = self.generator._model(document)
        self.generator._generation_guard(model, document)
        if len(model.layers.updated().all) > 10000:
            raise BridgeError("DIFFUSION_SIZE_LIMIT", "The document has too many layers.")
        if command == "configure_diffusion":
            changes = []
            for key, value in params.items():
                owner, attr = model, key
                if key in {"positive_prompt", "negative_prompt"}:
                    owner, attr = model.regions, key.split("_")[0]
                elif key in {"inpaint_mode", "use_inpaint", "use_prompt_focus"}:
                    owner = model.inpaint
                    if key == "inpaint_mode":
                        attr = "mode"
                        value = type(owner.mode)[value]
                elif key == "style_id":
                    root, _ = self.generator._context(connected=True)
                    styles = [
                        s
                        for s in self.generator._styles(root)
                        if self.generator._style_id(s) == value
                    ]
                    if len(styles) != 1:
                        raise BridgeError(
                            "DIFFUSION_STYLE_NOT_FOUND", "Style handle is unavailable."
                        )
                    value, attr = styles[0], "style"
                    if (
                        _module("ai_diffusion.model.model")
                        .resolve_arch(value, root.connection.client_if_connected)
                        .supports_edit
                    ):
                        raise BridgeError(
                            "DIFFUSION_WORKSPACE_UNSUPPORTED", "Edit styles are unsupported."
                        )
                changes.append((owner, attr, value))
            return self._apply(document_id, lambda: self._settings(changes))
        if command == "set_diffusion_region":
            layer = self._layer(model, params["node_id"], region=True)
            region = self._region(model, layer)
            if region is not None and len(region.layer_ids.split(",")) != 1:
                raise BridgeError(
                    "DIFFUSION_REGION_AMBIGUOUS", "Only single-linked regions can be edited."
                )
            if region is None and params["remove"]:
                raise BridgeError(
                    "DIFFUSION_REGION_NOT_FOUND", "No region is linked to that layer."
                )
            if region is None and len(model.regions) >= 32:
                raise BridgeError("DIFFUSION_SIZE_LIMIT", "At most 32 regions are supported.")

            if region is not None and any(c.has_active_job for c in region.control):
                raise BridgeError("DOCUMENT_BUSY", "A control preprocessor job is active.")

            def update_region():
                if params["remove"]:
                    model.regions.remove(region)
                else:
                    target = region if region is not None else model.regions._add(layer)
                    target.positive = params["positive_prompt"]
                return {"node_id": params["node_id"], "removed": params["remove"]}

            return self._apply(document_id, update_region)
        owner = model.regions
        if "region_node_id" in params:
            owner = self._region(model, self._layer(model, params["region_node_id"], region=True))
            if owner is None:
                raise BridgeError(
                    "DIFFUSION_REGION_NOT_FOUND", "No region is linked to that layer."
                )
            if len(owner.layer_ids.split(",")) != 1:
                raise BridgeError(
                    "DIFFUSION_REGION_AMBIGUOUS", "Only single-linked regions can be edited."
                )
        regions = list(model.regions)
        if len(regions) > 32:
            raise BridgeError("DIFFUSION_SIZE_LIMIT", "At most 32 regions are supported.")
        queues = [model.regions.control] + [r.control for r in regions]
        total = sum(len(q) for q in queues) - len(owner.control) + len(params["controls"])
        if total > 64:
            raise BridgeError(
                "DIFFUSION_SIZE_LIMIT", "At most 64 controls are supported per document."
            )
        if any(c.has_active_job for c in owner.control):
            raise BridgeError("DOCUMENT_BUSY", "A control preprocessor job is active.")
        controls = [(self._layer(model, c["node_id"]), c) for c in params["controls"]]

        def replace_controls():
            queue = owner.control
            for control in list(queue):
                queue.remove(control)
            module = _module("ai_diffusion.model.control")
            for layer, spec in controls:
                control = module.ControlLayer(
                    model, module.ControlMode[spec["mode"]], layer.id, len(queue)
                )
                control.use_custom_strength = True
                control.strength = round(spec["strength"] * 50)
                control.start, control.end = spec["start"], spec["end"]
                # Same insertion/signals as upstream add(), without choosing an ambient layer.
                control.mode_changed.connect(queue._update_last_mode)
                queue._layers.append(control)
                queue.added.emit(control)
            return {
                "controls": [
                    dict(spec, is_supported=c.is_supported) for (_, spec), c in zip(controls, queue)
                ],
                "region_node_id": params.get("region_node_id"),
            }

        return self._apply(document_id, replace_controls)

    @staticmethod
    def _settings(changes):
        for owner, attr, value in changes:
            setattr(owner, attr, value)
        return {"updated_fields": [attr for _, attr, _ in changes]}

    @staticmethod
    def _apply(document_id, action):
        try:
            return {"document_id": document_id, **action(), "undo": "not_guaranteed"}
        except Exception:
            raise BridgeError(
                "DIFFUSION_CONFIGURATION_FAILED",
                "Configuration may be partially applied; inspect before retrying.",
                effect="partial",
            ) from None
