"""Live hardware chart widgets — launched from the Loupedeck (or manually).

Usage:
    pythonw.exe charts.py <metric>

Metrics:
    cpu          CPU utility %
    cpu-temp     CPU package temp °C
    gpu          GPU load %
    gpu-temp     GPU core temp °C
    ram          RAM used GB
    disk         Per-disk activity % (4 lines)
    disk-temps   Per-disk temperature (4 lines)
    net          Network down/up MB/s (2 lines)
    temps        CPU + GPU + disk temps overlay

Each chart opens a frameless always-on-top 400x220 window with a 60-second
rolling view. Press Esc or right-click → Close to dismiss.
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QVBoxLayout, QWidget

from bar import Poller, Sample

WINDOW_W = 460
WINDOW_H = 230
WINDOW_N_SAMPLES = 60      # 60s @ 1Hz
REFRESH_MS = 1000
BG = (20, 20, 22, 220)
TEXT = "#e6e6e6"
GRID_ALPHA = 0.15
CONFIG_DIR = Path(__file__).with_name(".charts")


@dataclass
class MetricSpec:
    title: str
    y_label: str
    y_range: tuple[float, float] | None  # None = auto
    # series: list of (label, color, extractor). Extractor takes Sample and returns a value (or None).
    series: list[tuple[str, str, Callable[[Sample], float | None]]]


def _disk_activity_series(label_prefix: str, idx: int, color: str):
    def extract(s: Sample) -> float | None:
        if not s.disks or idx >= len(s.disks):
            return None
        return s.disks[idx].activity_pct
    return (label_prefix, color, extract)


def _disk_temp_series(label_prefix: str, idx: int, color: str):
    def extract(s: Sample) -> float | None:
        if not s.disks or idx >= len(s.disks):
            return None
        return s.disks[idx].temp_c
    return (label_prefix, color, extract)


METRICS: dict[str, MetricSpec] = {
    "cpu": MetricSpec(
        title="CPU utility",
        y_label="%",
        y_range=(0, 100),
        series=[("CPU", "#4ea1ff", lambda s: s.cpu_pct)],
    ),
    "cpu-temp": MetricSpec(
        title="CPU temperature",
        y_label="°C",
        y_range=(20, 100),
        series=[("CPU", "#ff6b6b", lambda s: s.cpu_temp_c)],
    ),
    "gpu": MetricSpec(
        title="GPU load",
        y_label="%",
        y_range=(0, 100),
        series=[("GPU", "#57d787", lambda s: s.gpu_pct)],
    ),
    "gpu-temp": MetricSpec(
        title="GPU temperature",
        y_label="°C",
        y_range=(20, 100),
        series=[("GPU", "#ff9f43", lambda s: s.gpu_temp_c)],
    ),
    "ram": MetricSpec(
        title="RAM used",
        y_label="GB",
        y_range=(0, 64),
        series=[("RAM", "#c780ff", lambda s: s.ram_used_gb)],
    ),
    "disk": MetricSpec(
        title="Disk activity",
        y_label="%",
        y_range=(0, 100),
        series=[
            _disk_activity_series("C", 0, "#4ea1ff"),
            _disk_activity_series("D", 1, "#57d787"),
            _disk_activity_series("E", 2, "#ff9f43"),
            _disk_activity_series("F", 3, "#c780ff"),
        ],
    ),
    "disk-temps": MetricSpec(
        title="Disk temperatures",
        y_label="°C",
        y_range=(20, 80),
        series=[
            _disk_temp_series("C", 0, "#4ea1ff"),
            _disk_temp_series("D", 1, "#57d787"),
            _disk_temp_series("E", 2, "#ff9f43"),
            _disk_temp_series("F", 3, "#c780ff"),
        ],
    ),
    "net": MetricSpec(
        title="Network",
        y_label="MB/s",
        y_range=None,
        series=[
            ("down", "#4ea1ff", lambda s: s.net_down_mbps),
            ("up",   "#ff9f43", lambda s: s.net_up_mbps),
        ],
    ),
    "temps": MetricSpec(
        title="All temperatures",
        y_label="°C",
        y_range=(20, 100),
        series=[
            ("CPU", "#ff6b6b", lambda s: s.cpu_temp_c),
            ("GPU", "#ff9f43", lambda s: s.gpu_temp_c),
            _disk_temp_series("C",    0, "#4ea1ff"),
            _disk_temp_series("D",    1, "#57d787"),
            _disk_temp_series("E",    2, "#a0a0a0"),
            _disk_temp_series("F",    3, "#c780ff"),
        ],
    ),
}


class ChartWindow(QWidget):
    def __init__(self, metric_key: str, spec: MetricSpec) -> None:
        super().__init__()
        self._metric = metric_key
        self._spec = spec
        self._drag_offset: QPoint | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(WINDOW_W, WINDOW_H)

        # Title label (top bar)
        self.title_label = QLabel(spec.title)
        self.title_label.setFont(QFont("Cascadia Mono", 9))
        self.title_label.setStyleSheet(
            f"color: {TEXT}; padding: 4px 8px 0 10px; background: transparent;"
        )
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # Plot
        pg.setConfigOptions(antialias=True, background=None, foreground=TEXT)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=GRID_ALPHA)
        self.plot.getAxis("left").setLabel(spec.y_label)
        self.plot.getAxis("bottom").setLabel("seconds ago")
        self.plot.setXRange(-WINDOW_N_SAMPLES, 0, padding=0)
        if spec.y_range is not None:
            self.plot.setYRange(*spec.y_range, padding=0)
        self.plot.addLegend(offset=(-10, 10))

        # Buffers + curves (one per series)
        self._x = list(range(-WINDOW_N_SAMPLES + 1, 1))  # -59..0
        self._buffers: list[deque[float | None]] = []
        self._curves = []
        for name, color, _ in spec.series:
            buf: deque[float | None] = deque([None] * WINDOW_N_SAMPLES, maxlen=WINDOW_N_SAMPLES)
            self._buffers.append(buf)
            curve = self.plot.plot(name=name, pen=pg.mkPen(color=color, width=2))
            self._curves.append(curve)

        # Root container with rounded dark background
        root = QWidget(self)
        root.setStyleSheet(
            f"background: rgba({BG[0]}, {BG[1]}, {BG[2]}, {BG[3]});"
            "border-radius: 10px;"
        )
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(2, 2, 2, 2)
        root_layout.setSpacing(0)
        root_layout.addWidget(self.title_label)
        root_layout.addWidget(self.plot)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        # Esc closes
        QShortcut(QKeySequence("Esc"), self, activated=self.close)

        # Poller + timer
        self.poller = Poller()

        self._load_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self._tick()  # first sample immediately (may be None for derivative-based metrics)

    # -- polling --
    def _tick(self) -> None:
        sample = self.poller.sample()
        for i, (_, _, extract) in enumerate(self._spec.series):
            val = extract(sample)
            self._buffers[i].append(val)
            xs, ys = [], []
            for x, y in zip(self._x, self._buffers[i]):
                if y is not None:
                    xs.append(x)
                    ys.append(y)
            self._curves[i].setData(xs, ys)

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

    # -- position persistence per metric --
    def _config_file(self) -> Path:
        CONFIG_DIR.mkdir(exist_ok=True)
        return CONFIG_DIR / f"{self._metric}.json"

    def _save_position(self) -> None:
        import json
        try:
            self._config_file().write_text(json.dumps({"x": self.x(), "y": self.y()}))
        except OSError:
            pass

    def _load_position(self) -> None:
        import json
        try:
            cfg = json.loads(self._config_file().read_text())
            self.move(cfg["x"], cfg["y"])
        except (OSError, ValueError, KeyError):
            # Default: top-center of primary screen, below the bar
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center().x() - WINDOW_W // 2, screen.top() + 60)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in METRICS:
        print("Usage: charts.py <metric>")
        print("Available metrics: " + ", ".join(METRICS))
        return 2

    metric = sys.argv[1]
    app = QApplication(sys.argv)
    win = ChartWindow(metric, METRICS[metric])
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
