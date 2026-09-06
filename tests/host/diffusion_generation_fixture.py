"""Fixed scratch canvas checks around real MCP-controlled AI Diffusion generations."""

import hashlib
import json
import os
from pathlib import Path
import time
import traceback

from krita import Extension, Krita, Selection
from PyQt6.QtCore import QTimer


class DiffusionGenerationFixture(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.output = Path(os.environ["KRITA6_DIFFUSION_FIXTURE_OUTPUT"])
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.tick)
        self.doc = None
        self.model = None
        self.deadline = time.monotonic() + 85
        self.phases = set()
        self.pending_phase = None

    def setup(self):
        QTimer.singleShot(2000, self.timer.start)

    def createActions(self, window):
        pass

    def snapshot(self):
        from ai_diffusion.settings import settings

        selection = self.doc.selection()
        return {
            "pixels": hashlib.sha256(bytes(self.doc.pixelData(0, 0, 512, 512))).hexdigest(),
            "corner_pixels": hashlib.sha256(bytes(self.doc.pixelData(0, 0, 64, 64))).hexdigest(),
            "layers": [(n.uniqueId().toString(), n.name()) for n in self.doc.topLevelNodes()],
            "selection": [selection.x(), selection.y(), selection.width(), selection.height()]
            if selection
            else None,
            "settings": {
                "positive": self.model.regions.positive,
                "negative": self.model.regions.negative,
                "strength": self.model.strength,
                "batch_count": self.model.batch_count,
                "seed": self.model.seed,
                "fixed_seed": self.model.fixed_seed,
                "style_name": self.model.style.name,
                "generation_finished_action": settings.generation_finished_action.name,
            },
            "jobs": [
                {
                    "state": job.state.name,
                    "result_count": len(job.results),
                    "workflow_kind": job.params.workflow_kind.name
                    if job.params.workflow_kind
                    else None,
                    "has_mask": job.params.has_mask,
                }
                for job in self.model.jobs
            ],
        }

    def prepare(self):
        from ai_diffusion.model.root import root

        app = Krita.instance()
        self.doc = app.createDocument(
            512,
            512,
            "Diffusion generation fixture",
            "RGBA",
            "U8",
            "sRGB-elle-V2-srgbtrc.icc",
            72.0,
        )
        app.activeWindow().addView(self.doc)
        layer = self.doc.createNode("Scratch color study", "paintlayer")
        self.doc.rootNode().addChildNode(layer, None)
        self.doc.setActiveNode(layer)
        pixels = bytearray(bytes((232, 205, 175, 255)) * (512 * 512))
        for y in range(192, 320):
            pixels[(y * 512 + 192) * 4 : (y * 512 + 320) * 4] = bytes((40, 65, 180, 255)) * 128
        layer.setPixelData(bytes(pixels), 0, 0, 512, 512)
        self.doc.refreshProjection()
        self.model = root.model_for_active_document()
        assert self.model is not None
        self.model.regions.positive = "Preserve the user's scratch prompt"
        self.model.regions.negative = "Preserve the user's scratch negative prompt"
        self.model.strength = 0.75
        self.model.batch_count = 2
        self.model.seed = 123
        self.model.fixed_seed = False

    def tick(self):
        try:
            from ai_diffusion.model.root import root

            if self.doc is None:
                if root.connection.state.name != "connected":
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError(
                            f"AI Diffusion did not connect: {root.connection.state.name}"
                        )
                    return
                self.prepare()
                return
            if not self.doc.tryBarrierLock():
                return
            self.doc.unlock()
            if self.pending_phase:
                (self.output / f"fixture-{self.pending_phase}.json").write_text(
                    json.dumps(self.snapshot())
                )
                self.pending_phase = None
            if not (self.output / "fixture-ready.json").exists():
                (self.output / "fixture-ready.json").write_text(
                    json.dumps(
                        {
                            "ready": True,
                            "snapshot": self.snapshot(),
                        }
                    )
                )
            for phase in (
                "configured",
                "before-apply-0",
                "after-apply-0",
                "undo-0",
                "redo-0",
                "selection",
                "before-apply-1",
                "after-apply-1",
                "undo-1",
                "redo-1",
            ):
                if phase in self.phases or not (self.output / f"fixture-request-{phase}").exists():
                    continue
                if phase == "selection":
                    selection = Selection()
                    selection.select(160, 160, 192, 192, 255)
                    self.doc.setSelection(selection)
                    self.doc.refreshProjection()
                    self.pending_phase = phase
                    self.phases.add(phase)
                    return
                if phase.startswith(("undo-", "redo-")):
                    action = "edit_undo" if phase.startswith("undo-") else "edit_redo"
                    Krita.instance().action(action).trigger()
                    self.pending_phase = phase
                    self.phases.add(phase)
                    return
                (self.output / f"fixture-{phase}.json").write_text(json.dumps(self.snapshot()))
                self.phases.add(phase)
        except Exception:
            self.timer.stop()
            (self.output / "fixture-failure.json").write_text(
                json.dumps(
                    {
                        "passed": False,
                        "traceback": traceback.format_exc(),
                    },
                    indent=2,
                )
            )


Krita.instance().addExtension(DiffusionGenerationFixture(Krita.instance()))
