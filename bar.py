"""Minimalist hardware-monitor bar for Ronildo's ASUS / 12700F / RTX 3080 setup.

Data sources:
    - psutil       -> CPU %, RAM, network
    - pynvml       -> GPU %, GPU temp, VRAM (RTX 3080)
    - LHM HTTP     -> CPU package temp, NVMe SSD temp (requires LibreHardwareMonitor
                      running with "Remote Web Server" enabled on port 8085)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import pynvml
import requests
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QAction, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget, QHBoxLayout

LHM_URL = "http://localhost:8085/data.json"
LHM_TIMEOUT_S = 0.5
REFRESH_MS = 1000
CONFIG_FILE = Path(__file__).with_name("config.local.json")

# Per-drive config.
# psutil reports disks as PhysicalDrive<N>. LHM reports them by model name;
# when two drives share a model (the BX500s), `lhm_index` picks the Nth match
# in tree-order (0 = first, 1 = second).
@dataclass
class DiskSpec:
    label: str
    lhm_model: str
    lhm_index: int = 0

DISKS: list[DiskSpec] = [
    DiskSpec("C", "CT1000P2SSD8",    0),  # P2 NVMe (OS)
    DiskSpec("D", "CT2000T500SSD8",  0),  # T500 NVMe
    DiskSpec("E", "CT2000BX500SSD1", 0),  # BX500 SATA #1
    DiskSpec("F", "CT2000BX500SSD1", 1),  # BX500 SATA #2
]

# Motherboard fan wiring (Nuvoton NCT6798D on this B660-I).
# LHM reports each header as "Fan #N"; these map to actual fan headers per build.
AIO_FAN_NUMBER: int | None = 6       # AIO pump tacho on header #6
CASE_FAN_NUMBERS: list[int] = [2]    # case fans, shown in the FAN group

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

# -------- data -----------------------------------------------------------


@dataclass
class DiskReading:
    label: str
    temp_c: float | None = None
    activity_pct: float | None = None


@dataclass
class Sample:
    cpu_pct: float | None = None
    cpu_temp_c: float | None = None
    cpu_power_w: float | None = None
    cpu_clock_ghz: float | None = None
    gpu_pct: float | None = None
    gpu_temp_c: float | None = None
    gpu_vram_used_gb: float | None = None
    gpu_vram_total_gb: float | None = None
    gpu_power_w: float | None = None
    gpu_fan_rpm: float | None = None
    gpu_clock_ghz: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    disks: list[DiskReading] | None = None
    aio_rpm: float | None = None
    case_fans: list[tuple[int, float | None]] | None = None  # [(fan_number, rpm)]
    net_down_mbps: float | None = None
    net_up_mbps: float | None = None


class Poller:
    def __init__(self) -> None:
        self._nvml_ok = False
        try:
            pynvml.nvmlInit()
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_ok = True
        except pynvml.NVMLError:
            self._gpu_handle = None

        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.monotonic()

    def sample(self) -> Sample:
        s = Sample()

        # cpu %
        try:
            s.cpu_pct = psutil.cpu_percent(interval=None)
        except Exception:
            pass

        # cpu clock (MHz average across cores) -> GHz
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

        # initialise disk readings (temp + activity are filled from LHM below)
        s.disks = [DiskReading(label=spec.label) for spec in DISKS]

        # gpu via nvml
        if self._nvml_ok:
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle)
                s.gpu_pct = float(util.gpu)
                s.gpu_temp_c = float(
                    pynvml.nvmlDeviceGetTemperature(self._gpu_handle, pynvml.NVML_TEMPERATURE_GPU)
                )
                meminfo = pynvml.nvmlDeviceGetMemoryInfo(self._gpu_handle)
                s.gpu_vram_used_gb = meminfo.used / 1024**3
                s.gpu_vram_total_gb = meminfo.total / 1024**3
                try:
                    s.gpu_power_w = pynvml.nvmlDeviceGetPowerUsage(self._gpu_handle) / 1000.0
                except pynvml.NVMLError:
                    pass
                try:
                    s.gpu_clock_ghz = pynvml.nvmlDeviceGetClockInfo(
                        self._gpu_handle, pynvml.NVML_CLOCK_GRAPHICS
                    ) / 1000.0
                except pynvml.NVMLError:
                    pass
            except pynvml.NVMLError:
                pass

        # lhm (cpu package temp, nvme ssd temp)
        try:
            r = requests.get(LHM_URL, timeout=LHM_TIMEOUT_S)
            r.raise_for_status()
            tree = r.json()
            s.cpu_temp_c = _find_cpu_package_temp(tree)
            s.cpu_power_w = _find_cpu_package_power(tree)
            s.gpu_fan_rpm = _find_gpu_fan_avg(tree)
            s.case_fans = _find_case_fans(tree, CASE_FAN_NUMBERS)
            if AIO_FAN_NUMBER is not None:
                aio_list = _find_case_fans(tree, [AIO_FAN_NUMBER])
                s.aio_rpm = aio_list[0][1] if aio_list else None
            if s.disks:
                _attach_disk_sensors(tree, s.disks)
        except (requests.RequestException, ValueError):
            pass

        return s


# -------- LHM tree parsing ----------------------------------------------


def _walk(node: dict):
    yield node
    for child in node.get("Children", []) or []:
        yield from _walk(child)


def _parse_lhm_value(v: str | None) -> float | None:
    """LHM values look like '62.0 °C' or '18.2 %'. Strip units and parse."""
    if not v:
        return None
    token = v.strip().split()
    if not token:
        return None
    try:
        return float(token[0].replace(",", "."))
    except ValueError:
        return None


def _find_cpu_package_temp(tree: dict) -> float | None:
    best = None
    for node in _walk(tree):
        text = (node.get("Text") or "").lower()
        val = _parse_lhm_value(node.get("Value"))
        if val is None:
            continue
        if "cpu package" in text or "package" in text and "cpu" in text:
            return val
        if "core average" in text or text.startswith("cpu core #"):
            best = val if best is None else max(best, val)
    return best


def _find_cpu_package_power(tree: dict) -> float | None:
    """Return CPU Package Power in watts."""
    for node in _walk(tree):
        text = (node.get("Text") or "").lower()
        ntype = (node.get("Type") or "").lower()
        if ntype != "power":
            continue
        if "cpu package" in text or text == "package" or "cpu cores" in text and "package" in text:
            val = _parse_lhm_value(node.get("Value"))
            if val is not None:
                return val
    # fallback: any node whose text contains "package" and type is power
    for node in _walk(tree):
        text = (node.get("Text") or "").lower()
        ntype = (node.get("Type") or "").lower()
        if ntype == "power" and "package" in text:
            return _parse_lhm_value(node.get("Value"))
    return None


def _find_gpu_fan_avg(tree: dict) -> float | None:
    """Return the average RPM across all GPU fans (e.g. 'GPU Fan 1', 'GPU Fan 2')."""
    rpms: list[float] = []
    for node in _walk(tree):
        if (node.get("Type") or "").lower() != "fan":
            continue
        text = (node.get("Text") or "").lower()
        if text.startswith("gpu fan"):
            val = _parse_lhm_value(node.get("Value"))
            if val is not None:
                rpms.append(val)
    return sum(rpms) / len(rpms) if rpms else None


def _find_case_fans(tree: dict, fan_numbers: list[int]) -> list[tuple[int, float | None]]:
    """Return [(fan_number, rpm)] for the given motherboard Fan #N sensors."""
    by_number: dict[int, float] = {}
    for node in _walk(tree):
        if (node.get("Type") or "").lower() != "fan":
            continue
        text = (node.get("Text") or "").strip()
        # Match "Fan #N" (motherboard fans) — skip "GPU Fan N"
        if not text.lower().startswith("fan #"):
            continue
        try:
            n = int(text.split("#", 1)[1])
        except (ValueError, IndexError):
            continue
        val = _parse_lhm_value(node.get("Value"))
        if val is not None:
            by_number[n] = val
    return [(n, by_number.get(n)) for n in fan_numbers]


def _attach_disk_sensors(tree: dict, disks: list[DiskReading]) -> None:
    """For each DISKS spec, find its Nth matching LHM device node and pull
    the primary temperature + 'Total Activity' load % from its subtree."""
    model_occurrence: dict[str, int] = {}
    found: dict[tuple[str, int], tuple[float | None, float | None]] = {}

    for node in _walk(tree):
        text = (node.get("Text") or "").strip()
        for spec in DISKS:
            if spec.lhm_model not in text:
                continue
            temp = _first_disk_temp(node)
            activity = _first_disk_activity(node)
            if temp is None and activity is None:
                continue  # not actually a storage device node
            idx = model_occurrence.get(spec.lhm_model, 0)
            found[(spec.lhm_model, idx)] = (temp, activity)
            model_occurrence[spec.lhm_model] = idx + 1
            break

    for reading in disks:
        spec = next((d for d in DISKS if d.label == reading.label), None)
        if spec is None:
            continue
        pair = found.get((spec.lhm_model, spec.lhm_index))
        if pair is not None:
            reading.temp_c, reading.activity_pct = pair


def _first_disk_temp(device_node: dict) -> float | None:
    """Primary temperature from a storage-device subtree.
    Prefers 'Composite Temperature' (NVMe), falls back to plain 'Temperature' (SATA)."""
    composite: float | None = None
    plain: float | None = None
    for node in _walk(device_node):
        if (node.get("Type") or "").lower() != "temperature":
            continue
        text = (node.get("Text") or "").strip().lower()
        val = _parse_lhm_value(node.get("Value"))
        if val is None:
            continue
        if "composite" in text:
            composite = val
        elif text == "temperature":
            plain = val
    return composite if composite is not None else plain


def _first_disk_activity(device_node: dict) -> float | None:
    """Return the 'Total Activity' Load % from a storage-device subtree."""
    for node in _walk(device_node):
        if (node.get("Type") or "").lower() != "load":
            continue
        text = (node.get("Text") or "").strip().lower()
        if text == "total activity":
            return _parse_lhm_value(node.get("Value"))
    return None


# -------- ui -------------------------------------------------------------


def _fmt(val: float | None, unit: str, digits: int = 0) -> str:
    if val is None:
        return f"--{unit}"
    if digits == 0:
        return f"{val:.0f}{unit}"
    return f"{val:.{digits}f}{unit}"


def _color_for(key: str, val: float | None) -> str:
    if val is None:
        return COLOR_DEFAULT
    for threshold, color in THRESHOLDS.get(key, []):
        if val >= threshold:
            return color
    return COLOR_DEFAULT


def _colored(text: str, color: str) -> str:
    if color == COLOR_DEFAULT:
        return text
    return f'<span style="color:{color}">{text}</span>'


def render(s: Sample) -> str:
    """Render the bar as HTML so values can be individually coloured by threshold."""
    # CPU group
    cpu_parts = [
        "CPU",
        _colored(_fmt(s.cpu_pct, "%"), _color_for("cpu_pct", s.cpu_pct)),
    ]
    if s.cpu_clock_ghz is not None:
        cpu_parts.append(f"{s.cpu_clock_ghz:.1f}G")
    cpu_parts.extend([
        _colored(_fmt(s.cpu_temp_c, "°C"), _color_for("cpu_temp_c", s.cpu_temp_c)),
        _fmt(s.cpu_power_w, "W"),
    ])

    # GPU group
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

    # Order: CPU, GPU, RAM, NET, AIO, FAN, GPU-FAN, disks (far right)
    sections = [
        " ".join(cpu_parts),
        " ".join(gpu_parts),
        ram,
        net,
        *aio_sections,
        *fan_sections,
        *gpu_fan_sections,
        *disk_groups,
    ]
    return "&nbsp;&nbsp;&nbsp;".join(sections)


class Bar(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._drag_offset: QPoint | None = None

        self.label = QLabel("initializing…")
        self.label.setTextFormat(Qt.TextFormat.RichText)
        # Rich-text QLabel intercepts mouse events; make it transparent to them
        # so drag (press/move/release) reaches the parent Bar widget.
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font_family = (
            "Cascadia Mono" if "Cascadia Mono" in QFontDatabase.families() else "Consolas"
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

        self.poller = Poller()
        psutil.cpu_percent(interval=None)  # prime the rolling window

        self._load_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self._tick()

    def _tick(self) -> None:
        self.label.setText(render(self.poller.sample()))
        self.adjustSize()

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

    # -- right-click exit --
    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())

    # -- position persistence --
    def _save_position(self) -> None:
        try:
            CONFIG_FILE.write_text(json.dumps({"x": self.x(), "y": self.y()}))
        except OSError:
            pass

    def _load_position(self) -> None:
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            self.move(cfg["x"], cfg["y"])
        except (OSError, ValueError, KeyError):
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - 620, screen.top() + 8)


def main() -> int:
    app = QApplication(sys.argv)
    bar = Bar()
    bar.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
