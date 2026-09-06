"""Test-only plugin: fixed native API scenario, never shipped in the bridge ZIP."""

import hashlib
import json
import os
from pathlib import Path
import platform
import time
import traceback

from krita import Extension, InfoObject, Krita, ManagedColor
from PyQt6.QtCore import PYQT_VERSION_STR, QPoint, QT_VERSION_STR, QTimer
from PyQt6.QtGui import QColor, QPainterPath
from PyQt6.QtWidgets import QApplication


class NativeProbe(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.app = Krita.instance()
        self.output = Path(os.environ["KRITA6_PROBE_OUTPUT"])
        self.report = {
            "krita": self.app.version(),
            "qt": QT_VERSION_STR,
            "pyqt": PYQT_VERSION_STR,
            "python": platform.python_version(),
            "checks": {},
            "passed": False,
        }
        self.doc = None
        self.step = 0
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)

    def setup(self):
        QTimer.singleShot(1500, self.start)

    def createActions(self, window):
        pass

    def start(self):
        try:
            self.doc = self.app.createDocument(
                160, 120, "Native probe", "RGBA", "U8", "sRGB-elle-V2-srgbtrc.icc", 72.0
            )
            self.view = self.app.activeWindow().addView(self.doc)
            self.layer = self.doc.createNode("Probe strokes", "paintlayer")
            self.doc.rootNode().addChildNode(self.layer, None)
            self.doc.setActiveNode(self.layer)
            presets = self.app.resources("preset")
            matches = [p for p in presets.values() if "Basic-5 Size" in p.name()]
            if not matches:
                raise RuntimeError("Bundled Basic-5 Size preset was not found")
            self.view.setCurrentBrushPreset(matches[0])
            self.report["preset"] = matches[0].name()
            self.view.setBrushSize(12.0)
            self.view.setPaintingOpacity(1.0)
            self.view.setPaintingFlow(1.0)
            self.view.setEraserMode(False)
            self.view.setGlobalAlphaLock(False)
            self.view.setDisablePressure(False)
            self.view.setCurrentBlendingMode("normal")
            self.view.setForeGroundColor(
                ManagedColor.fromQColor(QColor("#ff0000"), self.view.canvas())
            )
            self.deadline = time.monotonic() + 15
            self.timer.start()
        except Exception:
            self.fail()

    def pixels(self):
        return bytes(self.doc.pixelData(0, 0, 160, 120))

    def check(self, name, passed):
        self.report["checks"][name] = bool(passed)
        if not passed:
            raise AssertionError(name)

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise TimeoutError("Native completion barrier did not settle in 15s")
            if not self.doc.tryBarrierLock():
                return
            self.doc.unlock()
            if self.step == 0:
                self.blank = self.pixels()
                path = QPainterPath()
                path.moveTo(20, 30)
                path.lineTo(80, 70)
                path.lineTo(140, 30)
                self.layer.paintPath(path, "ForegroundColor", "None")
                # Native resources should have been captured before this change.
                self.view.setForeGroundColor(
                    ManagedColor.fromQColor(QColor("#00ff00"), self.view.canvas())
                )
            elif self.step == 1:
                self.path_pixels = self.pixels()
                self.check("native_path_changes_pixels", self.path_pixels != self.blank)
                projection = self.doc.projection(0, 0, 160, 120)
                self.check("explicit_projection_size", not projection.isNull())
                rgba = projection.pixelColor(80, 70)
                self.report["path_center_rgba"] = list(rgba.getRgb())
                self.check("path_png", projection.save(str(self.output / "native-path.png"), "PNG"))
                self.check(
                    "resource_snapshot_survives_immediate_restore",
                    rgba.red() > 200 and rgba.green() < 20,
                )
                self.app.action("edit_undo").trigger()
            elif self.step == 2:
                self.check("path_one_undo_restores_pixels", self.pixels() == self.blank)
                self.app.action("edit_redo").trigger()
            elif self.step == 3:
                self.check("path_redo_restores_pixels", self.pixels() == self.path_pixels)
                self.view.setForeGroundColor(
                    ManagedColor.fromQColor(QColor("#0000ff"), self.view.canvas())
                )
                self.layer.paintLine(QPoint(20, 95), QPoint(140, 95), 0.2, 1.0, "ForegroundColor")
            elif self.step == 4:
                self.line_pixels = self.pixels()
                self.check(
                    "native_pressure_line_changes_pixels", self.line_pixels != self.path_pixels
                )
                self.doc.projection(0, 0, 160, 120).save(
                    str(self.output / "native-line.png"), "PNG"
                )
                self.app.action("edit_undo").trigger()
            elif self.step == 5:
                self.check("line_one_undo_restores_pixels", self.pixels() == self.path_pixels)
                self.app.action("edit_redo").trigger()
            elif self.step == 6:
                self.check("line_redo_restores_pixels", self.pixels() == self.line_pixels)
                name, modified = self.doc.fileName(), self.doc.modified()
                preview = self.doc.thumbnail(80, 60)
                preview.save(str(self.output / "thumbnail.png"), "PNG")
                self.check("thumbnail_size", preview.width() == 80 and preview.height() == 60)
                self.check(
                    "thumbnail_preserves_document_state",
                    (name, modified) == (self.doc.fileName(), self.doc.modified()),
                )
                self.doc.setBatchmode(True)
                self.check("save_kra", self.doc.saveAs(str(self.output / "native.kra")))
                self.check(
                    "export_png",
                    self.doc.exportImage(str(self.output / "export.png"), InfoObject()),
                )
                self.report["pixel_sha256"] = hashlib.sha256(self.line_pixels).hexdigest()
                reopened = self.app.openDocument(str(self.output / "native.kra"))
                self.check(
                    "reopen_kra",
                    reopened is not None and reopened.width() == 160 and reopened.height() == 120,
                )
                self.check(
                    "reopen_layer_structure",
                    any(n.name() == "Probe strokes" for n in reopened.topLevelNodes()),
                )
                reopened.waitForDone()
                self.check(
                    "reopen_pixels", bytes(reopened.pixelData(0, 0, 160, 120)) == self.line_pixels
                )
                reopened.setModified(False)
                reopened.close()
                self.report["passed"] = True
                self.finish()
                return
            self.step += 1
            self.deadline = time.monotonic() + 15
        except Exception:
            self.fail()

    def fail(self):
        self.report["error"] = traceback.format_exc()
        self.finish()

    def finish(self):
        self.timer.stop()
        (self.output / "report.json").write_text(json.dumps(self.report, indent=2))
        for doc in self.app.documents():
            doc.setModified(False)
        QTimer.singleShot(100, QApplication.instance().quit)


Krita.instance().addExtension(NativeProbe(Krita.instance()))
