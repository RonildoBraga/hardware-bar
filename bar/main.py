"""Minimalist always-on-top hardware-monitor bar.

Cross-platform: the shared Poller reads CPU%/RAM/network (psutil) plus the
brightness daemon, blue-light state, and audio; OS-specific telemetry comes
from a platform sensor backend (bar/sensors.py):

    Windows -> psutil + pynvml + LibreHardwareMonitor HTTP + PDH    (bar/_win.py)
    macOS   -> psutil + a privileged powermetrics sampler           (bar/_mac.py)

Fields a backend can't supply on a given machine stay None and render as `--`.
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from pathlib import Path

import psutil

# Allow direct-file invocation (e.g. `pythonw.exe C:\...\bar\main.py`) by
# Loupedeck bindings that only have File + Arguments fields and no
# working-directory field — put the project root on sys.path ourselves.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio
import nightlight
from PyQt6.QtCore import Qt, QTimer, QPoint, QObject, QThread, pyqtSignal, pyqtSlot, QMetaObject
from PyQt6.QtGui import QAction, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget, QHBoxLayout

from _common import (
    IS_MACOS, SingleInstance, colored, fmt as _fmt, load_window_pos,
    publish_sample, save_window_pos, setup_logging,
)
from brightness.protocol import HOST as BRIGHTNESS_HOST, PORT as BRIGHTNESS_PORT, parse_status

# Data model + LHM parsing live in bar.model (pure, importable on any OS).
# Re-exported here so tests/test_lhm.py and bar.charts keep importing them from
# bar.main unchanged.
from bar.model import (  # noqa: F401  (re-exported for tests/test_lhm.py + bar.charts)
    DISKS,
    DiskReading,
    Sample,
    _disk_subtree_sensors,
    _parse_lhm,
    _parse_lhm_value,
)
from bar.sensors import make_backend

REFRESH_MS = 1000
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.local.json"

# Handlers attached lazily in main() so imports don't create log files.
log = logging.getLogger("bar")

BRIGHTNESS_TIMEOUT_S = 0.15  # fail fast if daemon is down/slow
# Order to display brightness values in the bar. Each entry is a daemon
# index (same as `brightness.client --list`). Indices beyond the connected
# display count are skipped, so a shorter macOS display set degrades cleanly.
BRIGHTNESS_DISPLAY_ORDER: list[int] = [1, 0, 2]

# Color thresholds: value >= threshold paints that colour. Order matters.
THRESHOLDS = {
    "cpu_pct":     [(95, "#ff5555"), (85, "#ffcc44")],
    "cpu_temp_c":  [(90, "#ff5555"), (80, "#ffcc44")],
    "gpu_pct":     [(98, "#ff5555"), (90, "#ffcc44")],
    "gpu_temp_c":  [(83, "#ff5555"), (75, "#ffcc44")],
    "disk_temp_c":  [(70, "#ff5555"), (60, "#ffcc44")],
    "disk_activity":[(95, "#ff5555"), (70, "#ffcc44")],
    "ram_pct":     [(90, "#ff5555"), (80, "#ffcc44")],
}
COLOR_DEFAULT = "#e6e6e6"


# -------- polling --------------------------------------------------------


class Poller:
    def __init__(self) -> None:
        # Created on the worker thread (see PollWorker.start) so backend init
        # — PDH/NVML on Windows — happens off the GUI thread.
        self._backend = make_backend()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.monotonic()

    def sample(self) -> Sample:
        s = Sample()

        # Platform hardware: cpu%, gpu, cpu temp/power, fans, disks.
        self._backend.populate(s)

        # cpu clock (psutil; None on Apple Silicon) -> GHz. Don't clobber a
        # value a backend may already have supplied (e.g. macOS powermetrics).
        if s.cpu_clock_ghz is None:
            try:
                freq = psutil.cpu_freq()
                if freq and freq.current:
                    s.cpu_clock_ghz = freq.current / 1000.0
            except Exception:
                pass

        # ram
        try:
            mem = psutil.virtual_memory()
            s.ram_used_gb = mem.used / 1024**3
            s.ram_total_gb = mem.total / 1024**3
        except Exception:
            pass

        # network rates
        try:
            now = time.monotonic()
            cur = psutil.net_io_counters()
            dt = max(now - self._last_net_t, 1e-6)
            s.net_down_mbps = (cur.bytes_recv - self._last_net.bytes_recv) / dt / 1024**2
            s.net_up_mbps = (cur.bytes_sent - self._last_net.bytes_sent) / dt / 1024**2
            self._last_net, self._last_net_t = cur, now
        except Exception:
            pass

        # brightness daemon status — optional, silent if daemon not running
        s.brightness_pcts = _poll_brightness()

        # blue-light reduction (Windows Night Light / macOS Night Shift)
        s.nightlight_on = nightlight.is_enabled()

        # audio — fast; defensive against device transitions
        try:
            status = audio.get_status()
            s.volume_pct = status["volume"]
            s.volume_muted = status["mute"]
            s.audio_device = status["device"]
        except Exception:
            pass

        return s


# -------- brightness daemon ---------------------------------------------


def _poll_brightness() -> list[int | None]:
    """Query the brightness daemon for per-display percent values.

    Wire format: 'status' -> '0:40 1:38 2:50' (or '0:- 1:38 ...' for unknown).
    Returns [] if the daemon is unreachable — the bar silently hides the field.
    """
    try:
        with socket.create_connection((BRIGHTNESS_HOST, BRIGHTNESS_PORT),
                                      timeout=BRIGHTNESS_TIMEOUT_S) as s:
            s.sendall(b"status\n")
            reply = s.recv(512).decode("utf-8", errors="replace")
    except (OSError, socket.timeout):
        return []

    return parse_status(reply)


# -------- ui -------------------------------------------------------------


def _abbreviate_device(name: str, max_len: int = 16) -> str:
    """Strip the " (driver name)" suffix Windows appends, then cap length."""
    paren = name.find(" (")
    if paren > 0:
        name = name[:paren]
    return name if len(name) <= max_len else name[:max_len - 1] + "…"


def _color_for(key: str, val: float | None) -> str:
    if val is None:
        return COLOR_DEFAULT
    for threshold, color in THRESHOLDS.get(key, []):
        if val >= threshold:
            return color
    return COLOR_DEFAULT


def _colored(text: str, color: str) -> str:
    return colored(text, color, COLOR_DEFAULT)


def render(s: Sample) -> str:
    """Render the bar as HTML so values can be individually coloured by threshold."""
    # CPU group — cap display at 100% to match Task Manager. The raw counter
    # can exceed 100% during Turbo (see CpuUtility); the GHz field already
    # carries that info on the bar, and bar.charts uses the uncapped value.
    cpu_pct_display = min(s.cpu_pct, 100.0) if s.cpu_pct is not None else None
    cpu_parts = [
        "CPU",
        _colored(_fmt(cpu_pct_display, "%"), _color_for("cpu_pct", cpu_pct_display)),
    ]
    if s.cpu_clock_ghz is not None:
        cpu_parts.append(f"{s.cpu_clock_ghz:.1f}G")
    cpu_parts.extend([
        _colored(_fmt(s.cpu_temp_c, "°C"), _color_for("cpu_temp_c", s.cpu_temp_c)),
        _fmt(s.cpu_power_w, "W"),
    ])

    # GPU group. Apple Silicon is a unified SoC: no dedicated VRAM, and the GPU
    # shares the CPU's die (so a "GPU temp" just duplicates the CPU temp). On
    # macOS we therefore show only the meaningful signals — load% and power —
    # and hide the group entirely when there's no GPU data (e.g. the
    # powermetrics daemon isn't installed). Windows keeps the discrete GPU's
    # full readout (load, clock, temp, VRAM, power).
    gpu_sections: list[str] = []
    if IS_MACOS:
        if s.gpu_pct is not None or s.gpu_power_w is not None:
            gpu_parts = ["GPU"]
            if s.gpu_pct is not None:
                gpu_parts.append(_colored(_fmt(s.gpu_pct, "%"), _color_for("gpu_pct", s.gpu_pct)))
            if s.gpu_power_w is not None:
                gpu_parts.append(_fmt(s.gpu_power_w, "W"))
            gpu_sections.append(" ".join(gpu_parts))
    else:
        gpu_parts = [
            "GPU",
            _colored(_fmt(s.gpu_pct, "%"), _color_for("gpu_pct", s.gpu_pct)),
        ]
        if s.gpu_clock_ghz is not None:
            gpu_parts.append(f"{s.gpu_clock_ghz:.1f}G")
        gpu_parts.append(_colored(_fmt(s.gpu_temp_c, "°C"), _color_for("gpu_temp_c", s.gpu_temp_c)))
        if s.gpu_vram_used_gb is not None and s.gpu_vram_total_gb is not None:
            gpu_parts.append(f"{s.gpu_vram_used_gb:.1f}/{s.gpu_vram_total_gb:.0f}G")
        gpu_parts.append(_fmt(s.gpu_power_w, "W"))
        gpu_sections.append(" ".join(gpu_parts))

    # RAM
    if s.ram_used_gb is not None and s.ram_total_gb is not None:
        ram_pct = 100 * s.ram_used_gb / s.ram_total_gb if s.ram_total_gb else None
        ram_text = f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}G"
        ram = "RAM " + _colored(ram_text, _color_for("ram_pct", ram_pct))
    else:
        ram = "RAM --"

    # Disks — per drive: label, temp, activity% (LHM "Total Activity" = disk busy time)
    disk_groups: list[str] = []
    for reading in s.disks or []:
        temp_fmt = _fmt(reading.temp_c, "°C")
        act_fmt  = _fmt(reading.activity_pct, "%")
        parts = [
            reading.label,
            _colored(temp_fmt, _color_for("disk_temp_c", reading.temp_c)),
            _colored(act_fmt,  _color_for("disk_activity", reading.activity_pct)),
        ]
        disk_groups.append(" ".join(parts))

    # Network
    net = f"NET ↓{_fmt(s.net_down_mbps, 'M', 1)} ↑{_fmt(s.net_up_mbps, 'M', 1)}"

    # Brightness — one % per display in BRIGHTNESS_DISPLAY_ORDER. Any indices
    # not covered by the order list fall back to daemon order at the end.
    bri_sections: list[str] = []
    if s.brightness_pcts:
        order = [i for i in BRIGHTNESS_DISPLAY_ORDER if i < len(s.brightness_pcts)]
        order += [i for i in range(len(s.brightness_pcts)) if i not in order]
        vals = [f"{s.brightness_pcts[i]}%" if s.brightness_pcts[i] is not None else "--"
                for i in order]
        bri_sections.append("BRI " + " ".join(vals))

    # Night Light / Night Shift — warm colour when on to echo the actual tint
    nl_sections: list[str] = []
    if s.nightlight_on is not None:
        if s.nightlight_on:
            nl_sections.append("NL " + _colored("on", "#ffcc44"))
        else:
            nl_sections.append("NL off")

    # Audio — VOL % (red if muted) and OUT <device name abbreviated>
    audio_sections: list[str] = []
    if s.volume_pct is not None:
        if s.volume_muted:
            audio_sections.append("VOL " + _colored("MUTE", "#ff5555"))
        else:
            audio_sections.append(f"VOL {s.volume_pct}%")
    if s.audio_device:
        audio_sections.append(f"OUT {_abbreviate_device(s.audio_device)}")

    # AIO pump
    aio_sections: list[str] = []
    if s.aio_rpm is not None:
        aio_sections.append(f"AIO {s.aio_rpm:.0f}rpm")

    # Case fans — plain RPMs (no #N prefix since there's only one in most configs)
    fan_sections: list[str] = []
    if s.case_fans:
        rpm_texts = [f"{rpm:.0f}" if rpm is not None else "--" for _, rpm in s.case_fans]
        fan_sections.append("FAN " + " ".join(rpm_texts))

    # GPU fan — sits next to the case fans
    gpu_fan_sections: list[str] = []
    if s.gpu_fan_rpm is not None:
        gpu_fan_sections.append(f"GPU-FAN {s.gpu_fan_rpm:.0f}rpm")

    # Order: CPU, GPU, RAM, NET, BRI, NL, VOL, OUT, AIO, FAN, GPU-FAN, disks
    sections = [
        " ".join(cpu_parts),
        *gpu_sections,
        ram,
        net,
        *bri_sections,
        *nl_sections,
        *audio_sections,
        *aio_sections,
        *fan_sections,
        *gpu_fan_sections,
        *disk_groups,
    ]
    return "&nbsp;&nbsp;&nbsp;".join(sections)


class PollWorker(QObject):
    """Runs the Poller on a background thread so blocking I/O (the LHM HTTP
    request and the brightness socket) never stalls the GUI. Owns a QTimer
    that lives in the worker thread once `start` runs there; each tick emits
    the fresh Sample back to the GUI thread via the `sampled` signal."""

    sampled = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._poller: Poller | None = None
        self._timer: QTimer | None = None

    @pyqtSlot()
    def start(self) -> None:
        self._poller = Poller()  # init backend (NVML/PDH) on the worker thread
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(REFRESH_MS)
        self._poll()

    @pyqtSlot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _poll(self) -> None:
        sample = self._poller.sample()
        publish_sample(sample)  # off-thread file write so bar.charts can subscribe
        self.sampled.emit(sample)


class Bar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        # Qt.Tool keeps the bar out of the Windows taskbar, but on macOS a Tool
        # window is a utility panel that auto-hides whenever its app isn't
        # frontmost — useless for an always-on widget. So drop Tool on macOS
        # and instead make the app a background "accessory" (no Dock icon, no
        # menu bar, doesn't deactivate the widget) via _macos_accessory_mode().
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        if not IS_MACOS:
            flags |= Qt.WindowType.Tool  # no taskbar entry (Windows)
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._drag_offset: QPoint | None = None
        # While True, keep the bar pinned flush to the top-right corner as its
        # width changes (content/platform-dependent). Cleared once the user
        # drags it somewhere, or if a saved position is loaded.
        self._auto_position = True

        self.label = QLabel("initializing…")
        self.label.setTextFormat(Qt.TextFormat.RichText)
        # Rich-text QLabel intercepts mouse events; make it transparent to them
        # so drag (press/move/release) reaches the parent Bar widget.
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font_family = (
            "Cascadia Mono" if "Cascadia Mono" in QFontDatabase.families()
            else "Menlo" if "Menlo" in QFontDatabase.families()
            else "Consolas"
        )
        self.label.setFont(QFont(font_family, 10))
        self.label.setStyleSheet(
            "background: rgba(20, 20, 22, 200);"
            f"color: {COLOR_DEFAULT};"
            "padding: 6px 14px;"
            "border-radius: 10px;"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)

        self._load_position()

        # Poll on a worker thread so blocking I/O can't freeze the UI.
        self._poll_thread = QThread(self)
        self._worker = PollWorker()
        self._worker.moveToThread(self._poll_thread)
        self._poll_thread.started.connect(self._worker.start)
        self._worker.sampled.connect(self._on_sample)
        self._poll_thread.start()

    def _on_sample(self, sample: Sample) -> None:
        self.label.setText(render(sample))
        self.adjustSize()  # triggers resizeEvent -> re-pin if auto-positioning

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # resizeEvent fires after the new geometry is applied, so width() is
        # reliable here (unlike frameGeometry() right after show()).
        if self._auto_position:
            self._pin_top_right()

    def _pin_top_right(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = max(area.left(), area.right() - self.width() - 5)
        log.debug("pin: area=(%d,%d,%d,%d) width=%d -> move(%d,%d)",
                  area.x(), area.y(), area.width(), area.height(),
                  self.width(), x, area.top() + 8)
        self.move(x, area.top() + 8)

    def shutdown(self) -> None:
        """Stop the worker's timer and join its thread before the app exits."""
        QMetaObject.invokeMethod(
            self._worker, "stop", Qt.ConnectionType.BlockingQueuedConnection
        )
        self._poll_thread.quit()
        self._poll_thread.wait(2000)

    # -- drag --
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        self._auto_position = False  # user chose a spot; stop auto-pinning
        self._save_position()

    # -- right-click exit --
    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())

    # -- position persistence --
    def _save_position(self) -> None:
        save_window_pos(CONFIG_FILE, self)

    def _load_position(self) -> None:
        # A saved position wins and disables auto-pinning; otherwise default to
        # the top-right corner and keep it pinned there as the width settles.
        if CONFIG_FILE.exists():
            self._auto_position = False
            screen = QApplication.primaryScreen().availableGeometry()
            load_window_pos(CONFIG_FILE, self, (screen.right() - 620, screen.top() + 8))
        else:
            self._auto_position = True
            self.adjustSize()
            self._pin_top_right()


def main() -> int:
    _, log_path = setup_logging("bar", "hardware-bar.log")
    log.info("launch argv=%s log=%s", sys.argv[1:], log_path)

    app = QApplication(sys.argv)
    if IS_MACOS:
        from bar.macwin import accessory_mode
        accessory_mode()  # no Dock icon; don't deactivate the widget on focus loss

    single = SingleInstance("hardware-bar", log)
    if single.signal_existing():
        return 0

    bar = Bar()
    single.become_primary(bar.close)
    app.aboutToQuit.connect(bar.shutdown)
    bar.show()
    if IS_MACOS:
        from bar.macwin import float_on_all_spaces
        float_on_all_spaces(bar)  # status level, visible across Spaces
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
