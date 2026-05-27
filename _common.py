"""Project-wide shared helpers. Sits at the project root so any module
(bar, brightness, audio, nightlight, meross, discovery) can import from
it without depending on the bar package.

Provides:
    setup_logging(name, filename)        attach file+stderr handlers to a named logger
    publish_sample / read_published_sample  bar↔chart pickle broadcast
    save_window_pos / load_window_pos    JSON {x,y} persistence for frameless windows
    SingleInstance(name, log)            QLocalServer one-instance toggle (Qt lazy)
"""

from __future__ import annotations

import json
import logging
import pickle
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

SAMPLE_FILE = Path(tempfile.gettempdir()) / "hardware-bar-sample.pickle"
SAMPLE_STALE_S = 3.0  # > REFRESH_MS * 2; older = bar not running


def setup_logging(name: str, log_filename: str) -> tuple[logging.Logger, Path]:
    """Configure a module logger writing to %TEMP%/<log_filename> + stderr."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log_path = Path(tempfile.gettempdir()) / log_filename
    fmt = logging.Formatter("%(asctime)s [%(process)5d] %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass  # pythonw has no stderr
    return log, log_path


def publish_sample(sample: Any) -> None:
    """Atomically write the current Sample to SAMPLE_FILE. Silent on error."""
    try:
        tmp = SAMPLE_FILE.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(sample))
        tmp.replace(SAMPLE_FILE)
    except OSError:
        pass


def read_published_sample() -> Any | None:
    """Return the most recent published Sample if fresh (within SAMPLE_STALE_S), else None."""
    try:
        st = SAMPLE_FILE.stat()
        if time.time() - st.st_mtime > SAMPLE_STALE_S:
            return None
        return pickle.loads(SAMPLE_FILE.read_bytes())
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError):
        return None


def save_window_pos(path: Path, widget) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"x": widget.x(), "y": widget.y()}))
    except OSError:
        pass


def load_window_pos(path: Path, widget, fallback_xy: tuple[int, int]) -> None:
    try:
        cfg = json.loads(path.read_text())
        widget.move(cfg["x"], cfg["y"])
    except (OSError, ValueError, KeyError):
        widget.move(*fallback_xy)


class SingleInstance:
    """One-instance toggle via QLocalServer (named pipe on Windows).

    Qt is lazy-imported inside the methods so non-Qt modules can import
    `setup_logging` from this file without dragging PyQt6 into their import.
    """

    def __init__(self, name: str, log: logging.Logger) -> None:
        self._name = name
        self._log = log
        self._server = None

    def signal_existing(self) -> bool:
        from PyQt6.QtNetwork import QLocalSocket
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if not sock.waitForConnected(500):
            return False
        sock.write(b"close")
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        sock.waitForDisconnected(500)
        return True

    def become_primary(self, on_close: Callable[[], None]) -> None:
        from PyQt6.QtNetwork import QLocalServer
        from PyQt6.QtWidgets import QApplication
        QLocalServer.removeServer(self._name)
        self._server = QLocalServer()
        if not self._server.listen(self._name):
            self._log.error("listen failed: %s", self._server.errorString())
            self._server = None
            return

        def _on_new_connection() -> None:
            while self._server is not None and self._server.hasPendingConnections():
                conn = self._server.nextPendingConnection()
                conn.waitForReadyRead(500)
                msg = bytes(conn.readAll()).decode("utf-8", "ignore")
                conn.disconnectFromServer()
                if "close" in msg:
                    try:
                        self._server.close()
                    except Exception:
                        pass
                    QLocalServer.removeServer(self._name)
                    self._server = None
                    on_close()
                    app = QApplication.instance()
                    if app is not None:
                        app.quit()

        self._server.newConnection.connect(_on_new_connection)
