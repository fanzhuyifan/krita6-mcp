"""Fixed scratch fixture using real AI Diffusion objects, without a generation backend."""

import hashlib
import json
import os
from pathlib import Path
import traceback

from krita import Extension, Krita
from PyQt6.QtCore import QTimer


class DiffusionFixture(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.output = Path(os.environ["KRITA6_DIFFUSION_FIXTURE_OUTPUT"])
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.verify)

    def setup(self):
        QTimer.singleShot(2000, self.prepare)

    def createActions(self, window):
        pass

    def snapshot(self):
        from ai_diffusion.model.root import root

        return {
            "pixels": hashlib.sha256(bytes(self.doc.pixelData(0, 0, 64, 64))).hexdigest(),
            "filename": self.doc.fileName(),
            "modified": self.doc.modified(),
            "models": len(root.models),
            "selection": list(self.model.jobs.selection),
            "jobs": [(job.id, job.state.name, len(job.results)) for job in self.model.jobs],
            "positive": self.model.regions.positive,
            "negative": self.model.regions.negative,
            "strength": self.model.strength,
            "batch_count": self.model.batch_count,
            "style_name": self.model.style.name,
            "layers": [
                (node.uniqueId().toString(), node.name()) for node in self.doc.topLevelNodes()
            ],
            "connection": root.connection.state.name,
            "client_absent": root.connection.client_if_connected is None,
        }

    def prepare(self):
        try:
            from ai_diffusion import __version__
            from ai_diffusion.image import Bounds
            from ai_diffusion.model.jobs import Job, JobKind, JobParams, JobState
            from ai_diffusion.model.root import root

            app = Krita.instance()
            self.doc = app.createDocument(
                64,
                64,
                "Diffusion inspection fixture",
                "RGBA",
                "U8",
                "sRGB-elle-V2-srgbtrc.icc",
                72.0,
            )
            app.activeWindow().addView(self.doc)
            layer = self.doc.createNode("Scratch layer", "paintlayer")
            self.doc.rootNode().addChildNode(layer, None)
            self.doc.setActiveNode(layer)
            # This test setup deliberately creates a model. The MCP reader must not.
            self.model = root.model_for_active_document()
            if self.model is None:
                raise AssertionError("AI Diffusion did not create its scratch model")
            self.model.regions.positive = "A red test square"
            self.model.regions.negative = "blur"
            self.model.strength = 0.65
            self.model.batch_count = 2
            for index, state in enumerate(
                (JobState.queued, JobState.executing, JobState.finished, JobState.cancelled)
            ):
                job = Job(
                    None if index == 0 else f"fixture-job-{index}",
                    JobKind.diffusion,
                    JobParams(Bounds(0, 0, 64, 64), f"Synthetic fixture {index}"),
                )
                job.state = state
                self.model.jobs.add_job(job)
            self.version = __version__
            QTimer.singleShot(1000, self.ready)
        except Exception:
            self.fail()

    def ready(self):
        try:
            self.before = self.snapshot()
            if not self.before["client_absent"]:
                raise AssertionError("Fixture must not connect a generation backend")
            (self.output / "fixture-ready.json").write_text(
                json.dumps({"ready": True, "version": self.version})
            )
            self.timer.start()
        except Exception:
            self.fail()

    def verify(self):
        if not (self.output / "verify-fixture").exists():
            return
        self.timer.stop()
        try:
            after = self.snapshot()
            checks = {f"preserved_{key}": value == after[key] for key, value in self.before.items()}
            checks["no_generation_backend"] = after["client_absent"]
            (self.output / "fixture-report.json").write_text(
                json.dumps({"passed": all(checks.values()), "checks": checks}, indent=2)
            )
        except Exception:
            self.fail()

    def fail(self):
        self.timer.stop()
        (self.output / "fixture-report.json").write_text(
            json.dumps({"passed": False, "traceback": traceback.format_exc()}, indent=2)
        )


Krita.instance().addExtension(DiffusionFixture(Krita.instance()))
