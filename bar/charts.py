"""Live hardware chart widgets — launched from the Loupedeck (or manually).

Usage:
    pythonw.exe charts.py <metric>

Metrics:
    cpu          CPU utility %
    cpu-temp     CPU package temp °C
    gpu          GPU load %
    gpu-temp     GPU core temp °C
    cpu-gpu      CPU utility + GPU load overlay
    ram          RAM used GB
    disk         Per-disk activity % (4 lines)
    disk-temps   Per-disk temperature (4 lines)
    net          Network down/up MB/s (2 lines)
    temps        CPU + GPU + disk temps overlay

Each chart opens a frameless always-on-top 400x220 window with a 60-second
rolling view. Press Esc or right-click → Close to dismiss.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QVBoxLayout, QWidget

# Allow direct-file invocation (e.g. `pythonw.exe C:\...\bar\charts.py cpu`)
# by Loupedeck bindings that have no working-directory field.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (
    SingleInstance, colored as _colored, fmt as _fmt, load_window_pos,
    read_published_sample, save_window_pos, setup_logging,
)
from bar import Poller, Sample

WINDOW_W = 460
WINDOW_H = 230
WINDOW_N_SAMPLES = 60      # 60s @ 1Hz
REFRESH_MS = 1000
BG = (20, 20, 22, 220)
TEXT = "#e6e6e6"
GRID_ALPHA = 0.15
CONFIG_DIR = Path(__file__).resolve().parent.parent / ".charts"

log = logging.getLogger("charts")


class SampleSource:
    """Read samples from the running bar's broadcast file if available; otherwise
    fall back to a locally-owned Poller. Multiple charts open at once share the
    bar's poll output instead of multiplying HTTP/NVML/COM traffic."""

    def __init__(self) -> None:
        self._local: Poller | None = None

    def sample(self) -> Sample:
        published = read_published_sample()
        if published is not None:
            return published
        if self._local is None:
            log.info("bar not broadcasting; falling back to local Poller")
            self._local = Poller()
        return self._local.sample()


@dataclass
class MetricSpec:
    title: str
    y_label: str
    y_range: tuple[float, float] | None  # None = auto
    # series: list of (label, color, extractor). Extractor takes Sample and returns a value (or None).
    series: list[tuple[str, str, Callable[[Sample], float | None]]]
    # Optional live header line shown in place of the static title. Receives
    # the current Sample, returns an HTML fragment rendered by the title label.
    header_fn: Callable[[Sample], str] | None = None


def _disk_activity_series(label_prefix: str, idx: int, color: str):
    def extract(s: Sample) -> float | None:
        if not s.disks or idx >= len(s.disks):
            return None
        return s.disks[idx].activity_pct
    return (label_prefix, color, extract)


# -------- header formatters --------------------------------------------

def _h_cpu(s: Sample) -> str:
    return "CPU " + _colored(_fmt(s.cpu_pct, "%"), "#4ea1ff")


def _h_cpu_temp(s: Sample) -> str:
    return "CPU " + _colored(_fmt(s.cpu_temp_c, "°C"), "#ff6b6b")


def _h_gpu(s: Sample) -> str:
    return "GPU " + _colored(_fmt(s.gpu_pct, "%"), "#57d787")


def _h_gpu_temp(s: Sample) -> str:
    return "GPU " + _colored(_fmt(s.gpu_temp_c, "°C"), "#ff9f43")


def _h_cpu_gpu(s: Sample) -> str:
    return (_colored("CPU " + _fmt(s.cpu_pct, "%"), "#4ea1ff")
            + "&nbsp;&nbsp;&nbsp;"
            + _colored("GPU " + _fmt(s.gpu_pct, "%"), "#57d787"))


def _h_ram(s: Sample) -> str:
    if s.ram_used_gb is None or s.ram_total_gb is None:
        return "RAM --"
    pct = 100 * s.ram_used_gb / s.ram_total_gb if s.ram_total_gb else 0
    text = f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}G ({pct:.0f}%)"
    return "RAM " + _colored(text, "#c780ff")


_DISK_COLORS = ["#4ea1ff", "#57d787", "#ff9f43", "#c780ff"]


def _h_disk(s: Sample) -> str:
    parts = []
    for i, r in enumerate(s.disks or []):
        color = _DISK_COLORS[i % len(_DISK_COLORS)]
        parts.append(_colored(f"{r.label} {_fmt(r.activity_pct, '%')}", color))
    return "&nbsp;&nbsp;".join(parts) or "no disks"


def _h_disk_temps(s: Sample) -> str:
    parts = []
    for i, r in enumerate(s.disks or []):
        color = _DISK_COLORS[i % len(_DISK_COLORS)]
        parts.append(_colored(f"{r.label} {_fmt(r.temp_c, '°C')}", color))
    return "&nbsp;&nbsp;".join(parts) or "no disks"


def _h_net(s: Sample) -> str:
    down = _colored(f"↓ {_fmt(s.net_down_mbps, 'M/s', 1)}", "#4ea1ff")
    up   = _colored(f"↑ {_fmt(s.net_up_mbps,  'M/s', 1)}", "#ff9f43")
    return f"{down}&nbsp;&nbsp;&nbsp;{up}"


def _h_temps(s: Sample) -> str:
    parts = [
        _colored(f"CPU {_fmt(s.cpu_temp_c, '°C')}", "#ff6b6b"),
        _colored(f"GPU {_fmt(s.gpu_temp_c, '°C')}", "#ff9f43"),
    ]
    extra_colors = ["#4ea1ff", "#57d787", "#a0a0a0", "#c780ff"]
    for i, r in enumerate(s.disks or []):
        parts.append(_colored(f"{r.label} {_fmt(r.temp_c, '°C')}",
                              extra_colors[i % len(extra_colors)]))
    return "&nbsp;".join(parts)


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
        # 0-200: the Windows "Processor Utility" counter includes frequency
        # scaling, so a boosted 12700F legitimately reads 120-180% under load.
        y_range=(0, 200),
        series=[("CPU", "#4ea1ff", lambda s: s.cpu_pct)],
        header_fn=_h_cpu,
    ),
    "cpu-temp": MetricSpec(
        title="CPU temperature",
        y_label="°C",
        y_range=(20, 100),
        series=[("CPU", "#ff6b6b", lambda s: s.cpu_temp_c)],
        header_fn=_h_cpu_temp,
    ),
    "gpu": MetricSpec(
        title="GPU load",
        y_label="%",
        y_range=(0, 100),
        series=[("GPU", "#57d787", lambda s: s.gpu_pct)],
        header_fn=_h_gpu,
    ),
    "gpu-temp": MetricSpec(
        title="GPU temperature",
        y_label="°C",
        y_range=(20, 100),
        series=[("GPU", "#ff9f43", lambda s: s.gpu_temp_c)],
        header_fn=_h_gpu_temp,
    ),
    "cpu-gpu": MetricSpec(
        title="CPU utility + GPU load",
        y_label="%",
        # CPU can exceed 100% via Turbo (see `cpu` entry); GPU is 0-100 via NVML.
        y_range=(0, 200),
        series=[
            ("CPU", "#4ea1ff", lambda s: s.cpu_pct),
            ("GPU", "#57d787", lambda s: s.gpu_pct),
        ],
        header_fn=_h_cpu_gpu,
    ),
    "ram": MetricSpec(
        title="RAM used",
        y_label="GB",
        y_range=(0, 64),
        series=[("RAM", "#c780ff", lambda s: s.ram_used_gb)],
        header_fn=_h_ram,
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
        header_fn=_h_disk,
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
        header_fn=_h_disk_temps,
    ),
    "net": MetricSpec(
        title="Network",
        y_label="MB/s",
        y_range=None,
        series=[
            ("down", "#4ea1ff", lambda s: s.net_down_mbps),
            ("up",   "#ff9f43", lambda s: s.net_up_mbps),
        ],
        header_fn=_h_net,
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
        header_fn=_h_temps,
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

        # Title label (top bar). Doubles as live header when spec.header_fn is set.
        self.title_label = QLabel(spec.title)
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
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

        # Sample source + timer. Prefers the running bar's broadcast file;
        # falls back to a local Poller if the bar isn't running.
        self.source = SampleSource()

        self._load_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self._tick()  # first sample immediately (may be None for derivative-based metrics)

    # -- polling --
    def _tick(self) -> None:
        sample = self.source.sample()
        for i, (_, _, extract) in enumerate(self._spec.series):
            val = extract(sample)
            self._buffers[i].append(val)
            xs, ys = [], []
            for x, y in zip(self._x, self._buffers[i]):
                if y is not None:
                    xs.append(x)
                    ys.append(y)
            self._curves[i].setData(xs, ys)
        if self._spec.header_fn is not None:
            try:
                self.title_label.setText(self._spec.header_fn(sample))
            except Exception as e:
                log.debug("header_fn failed: %s", e)

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
        return CONFIG_DIR / f"{self._metric}.json"

    def _save_position(self) -> None:
        save_window_pos(self._config_file(), self)

    def _load_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        load_window_pos(self._config_file(), self,
                        (screen.center().x() - WINDOW_W // 2, screen.top() + 60))


def main() -> int:
    _, log_path = setup_logging("charts", "hardware-bar-charts.log")
    log.info("launch argv=%s log=%s", sys.argv[1:], log_path)

    if len(sys.argv) != 2 or sys.argv[1] not in METRICS:
        print("Usage: charts.py <metric>")
        print("Available metrics: " + ", ".join(METRICS))
        return 2

    metric = sys.argv[1]
    app = QApplication(sys.argv)

    # Toggle: if another instance for this metric is running, tell it to close
    # and exit this one silently.
    single = SingleInstance(f"hardware-bar-chart-{metric}", log)
    if single.signal_existing():
        return 0

    win = ChartWindow(metric, METRICS[metric])
    single.become_primary(win.close)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
