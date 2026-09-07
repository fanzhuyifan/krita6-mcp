"""Independent GUI-thread observations for the isolated editing MCP probe only."""

import base64
import gc
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
        self.recovery_id = None
        self.view_failures = 0
        self.layer_failures = 0
        self.control_owner_removed = None

    def setup(self):
        QTimer.singleShot(1500, self.start)

    def createActions(self, window):
        pass

    def bridge_host(self):
        from krita6_bridge.host import KritaHost

        # Do not enumerate native Extension wrappers: their temporary ownership
        # affects fixture callback lifetime on the reference SIP build. The host
        # is a plain Python object and remains on this GUI thread throughout.
        return next(candidate for candidate in gc.get_objects() if type(candidate) is KritaHost)

    def arm_view_failure(self):
        fixture = self
        host = self.bridge_host()
        real_app = host.app

        class FailingWindow:
            def __init__(self, window):
                self.window = window

            def __getattr__(self, name):
                return getattr(self.window, name)

            def addView(self, document):
                host.app = real_app
                fixture.view_failures += 1
                fixture.recovery_id = next(
                    handle for handle, candidate in host._documents.items() if candidate == document
                )
                # No original wrapper is saved by this fixture. The host must
                # keep the native document alive after this frame unwinds.
                raise RuntimeError("Injected before native view attachment")

        class FailingApp:
            def __getattr__(self, name):
                return getattr(real_app, name)

            def activeWindow(self):
                return FailingWindow(real_app.activeWindow())

        host.app = FailingApp()

    def arm_layer_failure(self):
        fixture = self
        host = self.bridge_host()
        real_document = host._document

        class FailingDocument:
            def __init__(self, document):
                self.document = document

            def __getattr__(self, name):
                return getattr(self.document, name)

            def setActiveNode(self, node):
                host._document = real_document
                fixture.layer_failures += 1
                raise RuntimeError("Injected after native layer attachment")

        def document(handle):
            result = real_document(handle)
            return FailingDocument(result) if handle == fixture.recovery_id else result

        host._document = document

    def ownership_control(self):
        document = self.app.createDocument(
            16, 16, "Unretained owner control", "RGBA", "U8", "sRGB-elle-V2-srgbtrc.icc", 72.0
        )
        assert any(doc.name() == "Unretained owner control" for doc in self.app.documents())
        del document
        gc.collect()
        self.control_owner_removed = not any(
            doc.name() == "Unretained owner control" for doc in self.app.documents()
        )

    def attach_recovery_view(self):
        # addView must receive the original owning wrapper, so native ownership
        # transfer updates that wrapper rather than an enumerated nonowner alias.
        document = self.bridge_host()._owned_documents[self.recovery_id]
        window = self.app.activeWindow()
        view = window.addView(document)
        assert view is not None
        window.showView(view)

    def close_recovery(self):
        document = self.bridge_host()._document(self.recovery_id)
        document.setModified(False)
        assert document.close()

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
        host = self.bridge_host()
        return {
            "documents": documents,
            "images": images,
            "recovery": {
                "document_id": self.recovery_id,
                "view_failures": self.view_failures,
                "layer_failures": self.layer_failures,
                "control_owner_removed": self.control_owner_removed,
                "owner_retained": self.recovery_id in host._owned_documents,
                "owned_count": len(host._owned_documents),
            },
        }

    def tick(self):
        try:
            for doc in self.app.documents():
                if not doc.tryBarrierLock():
                    (self.output / "fixture-busy.json").write_text(
                        json.dumps(
                            {
                                "document": doc.name(),
                                "pending_snapshot": self.pending,
                                "request_waiting": (self.output / "fixture-request.json").exists(),
                            }
                        )
                    )
                    return
                doc.unlock()
            (self.output / "fixture-busy.json").unlink(missing_ok=True)
            if self.pending is not None:
                result = self.snapshot()
                temporary = self.output / f"fixture-{self.pending}.tmp"
                temporary.write_text(json.dumps(result))
                temporary.replace(self.output / f"fixture-{self.pending}.json")
                self.pending = None
            request_path = self.output / "fixture-request.json"
            if not request_path.exists():
                return
            request = json.loads(request_path.read_text())
            request_path.unlink()
            action = request["action"]
            if action in {"undo", "redo"}:
                # Fixed test-only actions; no generic action tool is exposed by MCP.
                self.app.action(f"edit_{action}").trigger()
            elif action == "fail_next_view":
                self.arm_view_failure()
            elif action == "fail_layer_activation":
                self.arm_layer_failure()
            elif action == "ownership_control":
                self.ownership_control()
            elif action == "attach_recovery_view":
                self.attach_recovery_view()
            elif action == "close_recovery":
                self.close_recovery()
            elif action == "collect":
                gc.collect()
            else:
                assert action == "snapshot"
            self.pending = int(request["id"])
        except Exception:
            self.fail()

    def fail(self):
        self.timer.stop()
        (self.output / "fixture-error.json").write_text(
            json.dumps({"error": traceback.format_exc()})
        )


Krita.instance().addExtension(EditingFixture(Krita.instance()))
