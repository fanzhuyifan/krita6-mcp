"""A GUI-owned timer executes one admitted operation at a time."""

from PyQt6.QtCore import QObject, QTimer

from .host import Pending
from .protocol import BridgeError, MUTATIONS


class GuiExecutor(QObject):
    def __init__(self, ledger, host, parent=None, on_drained=None):
        super().__init__(parent)
        host._assert_gui_thread()
        self.ledger = ledger
        self.host = host
        self.on_drained = on_drained
        self._current = None
        self._pending = None
        self._draining = False
        self._disposed = False
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)

    def start(self):
        self.host._assert_gui_thread()
        self.timer.start()

    def drain(self):
        self.host._assert_gui_thread()
        self._draining = True
        self.ledger.drain()
        # Continue ticking until an already dispatched native call settles.
        if not self.timer.isActive():
            self.timer.start()

    def dispose(self):
        """Release Qt ownership and Python caches after native work has settled."""
        if self._disposed:
            return
        self.host._assert_gui_thread()
        if self._current is not None or self._pending is not None:
            raise RuntimeError("Cannot dispose an executor with dispatched native work")
        self.timer.stop()
        self.timer.timeout.disconnect(self.tick)
        self.on_drained = None
        self.host = None
        self.ledger = None
        self._disposed = True
        # The QObject parent otherwise retains this executor and its timer across
        # each bridge restart, even after Extension drops its Python attribute.
        self.deleteLater()

    def tick(self):
        if self._disposed:
            return
        self.host._assert_gui_thread()
        try:
            if self._pending is not None:
                result = self._pending.poll()
                if result is not None:
                    self._finish(result=result)
                return
            if self._current is None:
                self._current = self.ledger.take_next()
            if self._current is None:
                if self._draining and self.ledger.is_idle():
                    self.timer.stop()
                    if self.on_drained is not None:
                        callback, self.on_drained = self.on_drained, None
                        callback()
                return
            result = self.host.execute(self._current)
            if isinstance(result, Pending):
                self._pending = result
            else:
                self._finish(result=result)
        except BridgeError as error:
            # Pending results can already contain confirmed document/node handles
            # even when native completion fails. Keep those recovery details with
            # the terminal error, including when the document has since closed.
            self._finish(
                result=self._pending_recovery_result(),
                error={"code": error.code, "message": error.message},
                effect=error.effect,
            )
        except Exception:
            # Exception text may contain document names, paths or plugin details.
            # Preserve uncertainty after dispatch without publishing those values.
            effect = (
                "unknown" if self._current and self._current["command"] in MUTATIONS else "none"
            )
            self._finish(
                result=self._pending_recovery_result(),
                error={"code": "HOST_ERROR", "message": "An unexpected Krita host error occurred."},
                effect=effect,
            )

    def _pending_recovery_result(self):
        result = self._pending.result if self._pending is not None else None
        if not isinstance(result, dict):
            return None
        # Other Pending fields may describe expected success (for example,
        # settings_restored or native_strokes). Preserve identities only; a known
        # handle does not establish that the corresponding object is still live.
        handles = {
            key: result[key]
            for key in (
                "document_id",
                "node_id",
                "source_document_id",
                "source_node_id",
                "generation_id",
                "result_id",
            )
            if key in result
        }
        return handles or None

    def _finish(self, result=None, error=None, effect=None):
        current = self._current
        if current is None:
            return
        if effect is None:
            effect = "applied" if current["command"] in MUTATIONS else "none"
        try:
            self.ledger.finish(current["operation_id"], result=result, error=error, effect=effect)
        except (BridgeError, ValueError, TypeError, OverflowError):
            # A too-large or non-JSON host result must not strand a running
            # operation after its native work has already finished.
            self.ledger.finish(
                current["operation_id"],
                result=None,
                error={
                    "code": "HOST_RESULT_INVALID",
                    "message": "The host result exceeded the bridge's data limits.",
                },
                effect="unknown" if current["command"] in MUTATIONS else "none",
            )
        self._current = None
        self._pending = None
