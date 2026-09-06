"""Independent GUI-thread observations for the isolated editing MCP probe only."""

import base64
import json
import os
from pathlib import Path
import traceback

from krita import Extension, Krita
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QImage


class EditingFixture(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.output = Path(os.environ["KRITA6_EDITING_FIXTURE_OUTPUT"])
        self.app = Krita.instance()
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.pending = None

    def setup(self):
        QTimer.singleShot(1500, self.start)

    def createActions(self, window):
        pass

    def start(self):
        try:
            image = QImage(24, 16, QImage.Format.Format_ARGB32)
            for y in range(16):
                for x in range(24):
                    color = (
                        QColor(255, 0, 0, 255)
                        if x < 12 and y < 8
                        else QColor(0, 255, 0, 255)
                        if y < 8
                        else QColor(0, 0, 255, 255)
                        if x < 12
                        else QColor(0, 0, 0, 0)
                    )
                    image.setPixelColor(x, y, color)
            assert image.save(str(self.output / "artwork/colors.png"), "PNG")
            image.fill(QColor(240, 120, 20))
            assert image.save(str(self.output / "artwork/orange.jpg"), "JPEG", 100)
            (self.output / "fixture-ready.json").write_text('{"ready": true}')
            self.timer.start()
        except Exception:
            self.fail()

    def snapshot(self):
        documents = []
        for doc in self.app.documents():
            width, height = doc.width(), doc.height()
            assert width <= 128 and height <= 96, "Probe only admits small scratch canvases"
            selection = doc.selection()
            documents.append(
                {
                    "name": doc.name(),
                    "filename": doc.fileName(),
                    "modified": doc.modified(),
                    "width": width,
                    "height": height,
                    "pixels": base64.b64encode(bytes(doc.pixelData(0, 0, width, height))).decode(),
                    "selection": None
                    if selection is None
                    else {
                        "bounds": [
                            selection.x(),
                            selection.y(),
                            selection.width(),
                            selection.height(),
                        ],
                        "pixels": base64.b64encode(
                            bytes(selection.pixelData(0, 0, width, height))
                        ).decode(),
                    },
                    "layers": [
                        {
                            "node_id": node.uniqueId().toString().strip("{}"),
                            "name": node.name(),
                            "visible": node.visible(),
                            "opacity": node.opacity(),
                            "pixels": base64.b64encode(
                                bytes(node.pixelData(0, 0, width, height))
                            ).decode(),
                        }
                        for node in doc.topLevelNodes()
                    ],
                }
            )
        images = {}
        for path in self.output.glob("preview-*.png"):
            image = QImage(str(path)).convertToFormat(QImage.Format.Format_RGBA8888)
            assert not image.isNull()
            images[path.name] = {
                "width": image.width(),
                "height": image.height(),
                "rgba": base64.b64encode(image.constBits().asstring(image.sizeInBytes())).decode(),
            }
        return {"documents": documents, "images": images}

    def tick(self):
        try:
            for doc in self.app.documents():
                if not doc.tryBarrierLock():
                    return
                doc.unlock()
            if self.pending is not None:
                result = self.snapshot()
                (self.output / f"fixture-{self.pending}.json").write_text(json.dumps(result))
                self.pending = None
            request_path = self.output / "fixture-request.json"
            if not request_path.exists():
                return
            request = json.loads(request_path.read_text())
            request_path.unlink()
            action = request["action"]
            assert action in {"snapshot", "undo", "redo"}
            if action != "snapshot":
                # Fixed test-only actions; no generic action tool is exposed by MCP.
                self.app.action(f"edit_{action}").trigger()
            self.pending = int(request["id"])
        except Exception:
            self.fail()

    def fail(self):
        self.timer.stop()
        (self.output / "fixture-error.json").write_text(
            json.dumps({"error": traceback.format_exc()})
        )


Krita.instance().addExtension(EditingFixture(Krita.instance()))
