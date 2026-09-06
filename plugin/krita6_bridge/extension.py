"""Krita plugin lifecycle and visible local controls."""

import json
import os
import threading
import uuid

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox
from krita import Extension, Krita

from .executor import GuiExecutor
from .host import KritaHost
from .operations import OperationLedger
from .output_paths import parse_output_roots
from .input_paths import parse_input_roots
from .protocol import BridgeError
from .transport import ArtifactStore, BridgeServer


class KritaBridgeExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self.host = None
        self.ledger = None
        self.server = None
        self.executor = None
        self._shutdown_thread = None
        self._state = "stopped"
        self._last_error = None

    def setup(self):
        # Krita only calls this after the user enables the plugin. Delaying also
        # allows the first window to exist before an autorun session is created.
        notifier = Krita.instance().notifier()
        notifier.setActive(True)
        notifier.applicationClosing.connect(self.stop)
        if os.environ.get("KRITA6_MCP_AUTOSTART", "1") == "1":
            QTimer.singleShot(0, self.start)

    def createActions(self, window):
        for identifier, label, callback in (
            ("krita6_mcp_start", "Start Krita 6 MCP Bridge", self.start),
            ("krita6_mcp_stop", "Stop Krita 6 MCP Bridge", self.stop),
            ("krita6_mcp_status", "Krita 6 MCP Bridge Status", self.show_status),
        ):
            action = window.createAction(identifier, label, "tools/scripts")
            if action is not None:
                action.triggered.connect(lambda checked=False, callback=callback: callback())

    def start(self):
        if self._state != "stopped":
            return
        try:
            roots = parse_output_roots(os.environ.get("KRITA6_MCP_OUTPUT_ROOTS", "{}"))
            artifacts = ArtifactStore()
            input_roots = parse_input_roots(os.environ.get("KRITA6_MCP_INPUT_ROOTS", "{}"))
            self.host = KritaHost(artifacts, roots, input_roots)
            self.ledger = OperationLedger("instance-" + uuid.uuid4().hex)
            self.server = BridgeServer(self.ledger, self.host.session_info(), artifacts)
            self.executor = GuiExecutor(
                self.ledger, self.host, parent=self, on_drained=self._drained
            )
            self.server.start()
            self.executor.start()
            self._state = "running"
            self._last_error = None
        except BridgeError as error:
            self._last_error = error.code + ": " + error.message
            self._startup_failed()
        except Exception:
            self._last_error = "STARTUP_FAILED: Check the plugin installation and private state directory permissions."
            self._startup_failed()

    def _startup_failed(self):
        if self.ledger is not None:
            self.ledger.drain()
        self._dispose_executor()
        if self.server is not None:
            # Failed startup has not dispatched host work. Avoid blocking the GUI
            # while joining any partially started network workers.
            threading.Thread(target=self.server.stop, daemon=True).start()
        self.host = self.ledger = self.server = self.executor = None
        self._state = "stopped"

    def _dispose_executor(self):
        if self.executor is not None:
            self.executor.dispose()
            self.executor = None

    def stop(self):
        if self._state != "running":
            return
        self._state = "draining"
        self.executor.drain()

    def _drained(self):
        server = self.server
        self._state = "stopping"
        # Only the standard-library server crosses this worker boundary.
        self._shutdown_thread = threading.Thread(target=server.stop, daemon=True)
        self._shutdown_thread.start()
        QTimer.singleShot(20, self._check_stopped)

    def _check_stopped(self):
        if self._shutdown_thread is not None and self._shutdown_thread.is_alive():
            QTimer.singleShot(20, self._check_stopped)
            return
        self._dispose_executor()
        self.host = self.ledger = self.server = self.executor = None
        self._shutdown_thread = None
        self._state = "stopped"

    def show_status(self):
        message = "Bridge state: " + self._state
        if self._last_error:
            message += "\n" + self._last_error
        if self.ledger is not None:
            message += "\n" + json.dumps(self.ledger.status(), sort_keys=True)
        QMessageBox.information(None, "Krita 6 MCP Bridge", message)
