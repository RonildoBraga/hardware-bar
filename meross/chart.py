"""meross/chart.py — live stacked-area chart of per-plug power draw.

Own Qt window (not part of bar.charts) because Meross calls are cloud-
routed and 100-500ms each — far too slow to mix into the 1 Hz bar
Poller. Instead, a background asyncio thread holds the MQTT connection
open and polls all plugs in parallel every POLL_INTERVAL_S, dropping
results into a shared buffer that the Qt timer reads at ~1 Hz.

Visualisation: stacked area via pyqtgraph FillBetweenItem. Each device
is a coloured band; the top edge of the stack is the total draw. A text
header shows live total + top-3 consumers.

Usage:
    python -m meross.chart

Same single-instance toggle as bar.charts — a second launch closes the
existing window. Position persists across launches under .charts/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QAction, QColor, QFont, QKeySequence, QShortcut
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QVBoxLayout, QWidget

from meross_iot.controller.mixins.electricity import ElectricityMixin

from .core import _connect

LOG_PATH = Path(tempfile.gettempdir()) / "hardware-bar-meross-chart.log"
log = logging.getLogger("meross.chart")

PROJECT_ROOT    = Path(__file__).resolve().parent.parent
CONFIG_DIR      = PROJECT_ROOT / ".charts"
POSITION_FILE   = CONFIG_DIR / "meross-energy.json"
CSV_FILE        = CONFIG_DIR / "meross-energy.csv"

POLL_INTERVAL_S   = 10.0
HISTORY_MINUTES   = 20
WINDOW_N_SAMPLES  = int(HISTORY_MINUTES * 60 / POLL_INTERVAL_S)  # 120 samples
REFRESH_MS        = 1000

WINDOW_W = 720
WINDOW_H = 360
BG       = (20, 20, 22, 220)
TEXT     = "#e6e6e6"

# Stable palette cycled by device-name sort order. Distinct hues + mid-brightness
# so stacked bands stay distinguishable against the dark background.
PALETTE = [
    "#4ea1ff", "#57d787", "#ff9f43", "#c780ff", "#ff6b6b",
    "#ffcc44", "#4ec0ff", "#87d757", "#ff80bf", "#a0a0a0",
    "#80ffff", "#e0c080",
]


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(process)5d] %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass
    # Quiet meross_iot's own chatty loggers.
    log.propagate = False
    for noisy in ("meross_iot", "paho", "asyncio", "aiohttp"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.ERROR)
        lg.propagate = False


# -------- shared state ------------------------------------------------

@dataclass
class Device:
    uuid:    str
    name:    str
    color:   str
    history: deque                  # rolling deque of float|None, len == WINDOW_N_SAMPLES


@dataclass
class SharedState:
    lock:         threading.Lock            = field(default_factory=threading.Lock)
    devices:      dict[str, Device]         = field(default_factory=dict)
    # Stable ordering for stacked-area layering: sorted by name at startup.
    order:        list[str]                 = field(default_factory=list)
    status:       str                       = "connecting..."
    last_update:  Optional[float]           = None
    stop:         threading.Event           = field(default_factory=threading.Event)


# -------- async poller ------------------------------------------------

async def _read_one(plug):
    try:
        m = await plug.async_get_instant_metrics()
        return plug.uuid, float(m.power)
    except Exception as e:
        log.warning("metric read failed for %s: %s", plug.name, e)
        return plug.uuid, None


async def _poll_loop(shared: SharedState) -> None:
    log.info("connecting to Meross cloud...")
    try:
        http, manager = await _connect()
    except Exception as e:
        log.exception("connect failed")
        with shared.lock:
            shared.status = f"login failed: {e}"
        return

    try:
        # First discovery runs inside _connect(); enrollment for some devices
        # completes asynchronously via MQTT ability-queries AFTER that returns.
        # Give it settling time and re-discover to catch late enrollments —
        # otherwise we can end up with only the first-to-respond plug.
        with shared.lock:
            shared.status = "discovering devices..."
        for _ in range(3):
            await asyncio.sleep(2.0)
            try:
                await manager.async_device_discovery()
            except Exception as e:
                log.warning("re-discovery failed: %s", e)

        plugs = [p for p in manager.find_devices() if isinstance(p, ElectricityMixin)]
        plugs.sort(key=lambda p: p.name.casefold())
        log.info("discovered %d ElectricityMixin device(s): %s",
                 len(plugs), ", ".join(p.name for p in plugs))
        if not plugs:
            with shared.lock:
                shared.status = "no energy-monitoring plugs online"
            log.error("no ElectricityMixin devices online")
            return

        with shared.lock:
            shared.devices.clear()
            shared.order.clear()
            for i, p in enumerate(plugs):
                shared.devices[p.uuid] = Device(
                    uuid=p.uuid,
                    name=p.name,
                    color=PALETTE[i % len(PALETTE)],
                    history=deque([None] * WINDOW_N_SAMPLES, maxlen=WINDOW_N_SAMPLES),
                )
                shared.order.append(p.uuid)
            shared.status = f"polling {len(plugs)} plug(s) every {POLL_INTERVAL_S:.0f}s"
        log.info("polling %d plugs", len(plugs))

        # Ensure CSV dir exists
        try:
            CONFIG_DIR.mkdir(exist_ok=True)
        except OSError:
            pass

        while not shared.stop.is_set():
            t0 = time.monotonic()
            results = await asyncio.gather(*[_read_one(p) for p in plugs])
            ts = time.time()

            with shared.lock:
                for uuid, power in results:
                    if uuid in shared.devices:
                        shared.devices[uuid].history.append(power)
                shared.last_update = ts

            # Append to CSV (one row per device per sample, ISO timestamp for
            # readable later analysis).
            try:
                iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
                with CSV_FILE.open("a", encoding="utf-8") as f:
                    for uuid, power in results:
                        name = shared.devices[uuid].name if uuid in shared.devices else uuid
                        pw   = "" if power is None else f"{power:.2f}"
                        f.write(f"{iso},{name},{pw}\n")
            except OSError as e:
                log.warning("csv write failed: %s", e)

            # Interruptible wait — check stop flag every 250ms so we can
            # shut down within ~250ms of the window closing, not 10s.
            elapsed = time.monotonic() - t0
            remaining = max(0.0, POLL_INTERVAL_S - elapsed)
            end = time.monotonic() + remaining
            while not shared.stop.is_set() and time.monotonic() < end:
                await asyncio.sleep(0.25)

    finally:
        try:
            manager.close()
        except Exception:
            pass


def _run_poller(shared: SharedState) -> None:
    try:
        asyncio.run(_poll_loop(shared))
    except Exception:
        log.exception("poller thread crashed")


# -------- chart window ------------------------------------------------

class EnergyChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.shared = SharedState()
        self._drag_offset: QPoint | None = None

        # We build the per-device curves lazily once the poller reports the
        # device set — before that we don't know who's there.
        self._built = False
        self._curves: dict[str, pg.PlotDataItem] = {}

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(WINDOW_W, WINDOW_H)

        # Header: total + top-3 consumers + status line
        self.header = QLabel("connecting...")
        self.header.setFont(QFont("Cascadia Mono", 9))
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setStyleSheet(
            f"color: {TEXT}; padding: 4px 10px 2px 10px; background: transparent;"
        )
        self.header.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Plot
        pg.setConfigOptions(antialias=True, background=None, foreground=TEXT)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.getAxis("left").setLabel("W")
        self.plot.getAxis("bottom").setLabel("minutes ago")
        self.plot.setXRange(-HISTORY_MINUTES, 0, padding=0)
        self.legend = self.plot.addLegend(offset=(-10, 10))

        # X axis: sample index i maps to (i - (N-1)) * POLL / 60 minutes ago.
        self._x = [(i - (WINDOW_N_SAMPLES - 1)) * POLL_INTERVAL_S / 60.0
                   for i in range(WINDOW_N_SAMPLES)]

        # Container with dark rounded bg
        root = QWidget(self)
        root.setStyleSheet(
            f"background: rgba({BG[0]}, {BG[1]}, {BG[2]}, {BG[3]});"
            "border-radius: 10px;"
        )
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(2, 2, 2, 2)
        root_layout.setSpacing(0)
        root_layout.addWidget(self.header)
        root_layout.addWidget(self.plot)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        QShortcut(QKeySequence("Esc"), self, activated=self.close)

        # Background polling thread
        self._poller = threading.Thread(target=_run_poller, args=(self.shared,),
                                        name="meross-chart-poller", daemon=True)
        self._poller.start()

        self._load_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)

    # -- curve construction (once the device set is known) --
    def _build_curves(self, devices: dict[str, Device], order: list[str]) -> None:
        for uuid in list(self._curves):
            self.plot.removeItem(self._curves[uuid])
        self._curves.clear()
        try:
            self.legend.clear()
        except Exception:
            pass
        for uuid in order:
            dev = devices[uuid]
            pen = pg.mkPen(color=QColor(dev.color), width=2)
            self._curves[uuid] = self.plot.plot(name=dev.name, pen=pen)
        self._built = True

    # -- per-tick update --
    def _tick(self) -> None:
        with self.shared.lock:
            devices = {u: Device(uuid=d.uuid, name=d.name, color=d.color,
                                 history=deque(d.history, maxlen=WINDOW_N_SAMPLES))
                       for u, d in self.shared.devices.items()}
            order = list(self.shared.order)
            status = self.shared.status
            last_update = self.shared.last_update

        if not devices:
            # Still connecting / no devices yet.
            self.header.setText(status)
            return

        # Build curves once we have a stable device set.
        if not self._built:
            self._build_curves(devices, order)

        # One line per device; skip None samples (same pattern as bar.charts).
        for uuid in order:
            if uuid not in self._curves:
                continue
            xs, ys = [], []
            for x, v in zip(self._x, devices[uuid].history):
                if v is not None:
                    xs.append(x)
                    ys.append(float(v))
            self._curves[uuid].setData(xs, ys)

        # Header: total + top-3 current consumers.
        totals: list[tuple[str, float, str]] = []
        for uuid in order:
            dev = devices[uuid]
            v = dev.history[-1]
            if v is None:
                continue
            totals.append((dev.name, float(v), dev.color))
        total_w = sum(w for _, w, _ in totals)
        top3 = sorted(totals, key=lambda x: -x[1])[:3]

        def _colored(text: str, color: str) -> str:
            return f'<span style="color:{color}">{text}</span>'

        top3_html = "&nbsp;&nbsp;".join(
            _colored(f"{n} {w:.0f}W", c) for n, w, c in top3
        ) if top3 else ""
        staleness = ""
        if last_update is not None:
            age = time.time() - last_update
            if age > POLL_INTERVAL_S * 2:
                staleness = f' <span style="color:#ff5555">(stale {age:.0f}s)</span>'
        self.header.setText(
            f"TOTAL <b>{total_w:.0f}W</b>{staleness}&nbsp;&nbsp;&nbsp;{top3_html}"
        )

    # -- drag --
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        self._save_position()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        close_act = QAction("Close", self)
        close_act.triggered.connect(self.close)
        menu.addAction(close_act)
        menu.exec(event.globalPos())

    def closeEvent(self, event) -> None:
        log.info("window closing; stopping poller")
        self.shared.stop.set()
        super().closeEvent(event)

    # -- position persistence --
    def _save_position(self) -> None:
        try:
            CONFIG_DIR.mkdir(exist_ok=True)
            POSITION_FILE.write_text(json.dumps({"x": self.x(), "y": self.y()}))
        except OSError:
            pass

    def _load_position(self) -> None:
        try:
            cfg = json.loads(POSITION_FILE.read_text())
            self.move(cfg["x"], cfg["y"])
        except (OSError, ValueError, KeyError):
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center().x() - WINDOW_W // 2, screen.top() + 60)


# -------- single-instance toggle --------------------------------------

class SingleInstance:
    def __init__(self) -> None:
        self._name = "hardware-bar-meross-energy"
        self._server: QLocalServer | None = None

    def signal_existing(self) -> bool:
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
        QLocalServer.removeServer(self._name)
        self._server = QLocalServer()
        if not self._server.listen(self._name):
            log.error("listen failed: %s", self._server.errorString())
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


# -------- entry -------------------------------------------------------

def main() -> int:
    _setup_logging()
    log.info("=" * 60)
    log.info("launch argv=%s log=%s", sys.argv[1:], LOG_PATH)

    app = QApplication(sys.argv)

    single = SingleInstance()
    if single.signal_existing():
        log.info("signaled existing chart — exiting")
        return 0

    win = EnergyChart()
    single.become_primary(win.close)
    win.show()
    rc = app.exec()
    log.info("event loop exited rc=%s", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
