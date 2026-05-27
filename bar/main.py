"""Minimalist hardware-monitor bar for Ronildo's ASUS / 12700F / RTX 3080 setup.

Data sources:
    - psutil       -> CPU %, RAM, network
    - pynvml       -> GPU %, GPU temp, VRAM (RTX 3080)
    - LHM HTTP     -> CPU package temp, NVMe SSD temp (requires LibreHardwareMonitor
                      running with "Remote Web Server" enabled on port 8085)
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import ctypes
from ctypes import wintypes

import psutil
import pynvml
import requests

# Allow direct-file invocation (e.g. `pythonw.exe C:\...\bar\main.py`) by
# Loupedeck bindings that only have File + Arguments fields and no
# working-directory field — put the project root on sys.path ourselves.
if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio
import nightlight
from PyQt6.QtCore import Qt, QTimer, QPoint
from PyQt6.QtGui import QAction, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget, QHBoxLayout

from _common import (
    SingleInstance, load_window_pos, publish_sample, save_window_pos, setup_logging,
)

LHM_URL = "http://localhost:8085/data.json"
LHM_TIMEOUT_S = 0.5
LHM_TASK_NAME = "LibreHardwareMonitor"
REFRESH_MS = 1000
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.local.json"

# Handlers attached lazily in main() so `from bar import Sample/Poller`
# from bar.charts doesn't create a log file as a side-effect.
log = logging.getLogger("bar")

BRIGHTNESS_HOST = "127.0.0.1"
BRIGHTNESS_PORT = 48736
BRIGHTNESS_TIMEOUT_S = 0.15  # fail fast if daemon is down/slow
# Order to display brightness values in the bar. Each entry is a daemon
# index (same as `brightness_client.py --list`). Daemon order is
# 0=KAMN49QDQUCLA, 1=Smart TV, 2=Cintiq 16 — bar shows TV, KGN, Wacom.
BRIGHTNESS_DISPLAY_ORDER: list[int] = [1, 0, 2]

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
    # Per-display brightness percent, in daemon/Display-Config order.
    # Entry is None if that display's value is unknown (e.g. DDC unreadable).
    brightness_pcts: list[int | None] = field(default_factory=list)
    # Windows Night Light state; None if the registry key is unreadable.
    nightlight_on: bool | None = None
    # Default audio output state; each field is None if the query failed.
    volume_pct: int | None = None
    volume_muted: bool | None = None
    audio_device: str | None = None


class CpuUtility:
    """Reads '\\Processor Information(_Total)\\% Processor Utility' via Windows PDH.

    Same underlying counter Task Manager uses on Windows 8+, but reported as a
    percent of the *base* frequency — so on Turbo it legitimately exceeds 100%
    (a 12700F at 4.8 GHz against a 2.1 GHz P-core base reads ~180% under load).
    Task Manager caps display at 100% and shows the turbo factor as "Speed";
    we keep the raw value so `bar.charts` can show Turbo, and cap the bar's
    own display in `render()` (the GHz field carries the turbo info there).

    Chosen over psutil.cpu_percent() because psutil maps to '% Processor Time'
    (time-not-idle), which under-represents work on modern parked-core CPUs.
    """

    _PDH_FMT_DOUBLE = 0x00000200
    _COUNTER_PATH   = "\\Processor Information(_Total)\\% Processor Utility"

    class _Value(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

    def __init__(self) -> None:
        self._ok = False
        try:
            self._pdh = ctypes.WinDLL("pdh.dll")
            self._query = wintypes.HANDLE()
            self._counter = wintypes.HANDLE()

            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            if self._pdh.PdhAddEnglishCounterW(
                self._query, self._COUNTER_PATH, 0, ctypes.byref(self._counter)
            ) != 0:
                return
            # First collect establishes the baseline; first sample() may be 0.
            self._pdh.PdhCollectQueryData(self._query)
            self._ok = True
        except Exception:
            pass

    def sample(self) -> float | None:
        if not self._ok:
            return None
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            val = self._Value()
            res = self._pdh.PdhGetFormattedCounterValue(
                self._counter, self._PDH_FMT_DOUBLE, None, ctypes.byref(val)
            )
            if res != 0:
                return None
            return val.doubleValue
        except Exception:
            return None


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

        self._cpu_utility = CpuUtility()

        # LHM auto-spawn: set once per Poller lifetime on first unreachable
        # LHM, so we don't spam schtasks every second.
        self._lhm_spawn_tried = False

    def sample(self) -> Sample:
        s = Sample()

        # cpu %: prefer Processor Utility (matches Task Manager on Win8+),
        # fall back to psutil's Processor Time if PDH isn't available.
        s.cpu_pct = self._cpu_utility.sample()
        if s.cpu_pct is None:
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

        # brightness daemon status — optional, silent if daemon not running
        s.brightness_pcts = _poll_brightness()

        # night light — cheap registry read; None if key absent
        s.nightlight_on = nightlight.is_enabled()

        # audio — COM calls are fast; defensive against device transitions
        try:
            status = audio.get_status()
            s.volume_pct = status["volume"]
            s.volume_muted = status["mute"]
            s.audio_device = status["device"]
        except Exception:
            pass

        # lhm (cpu package temp+power, gpu/case fans, per-disk temp+activity).
        # One outer tree-walk in `_parse_lhm`, plus a small sub-walk per disk
        # device — replaces the previous 5+ full walks per tick.
        try:
            r = requests.get(LHM_URL, timeout=LHM_TIMEOUT_S)
            r.raise_for_status()
            parsed = _parse_lhm(r.json())
            s.cpu_temp_c = parsed.cpu_temp_c
            s.cpu_power_w = parsed.cpu_power_w
            s.gpu_fan_rpm = parsed.gpu_fan_rpm
            s.case_fans = [(n, parsed.motherboard_fans.get(n)) for n in CASE_FAN_NUMBERS]
            if AIO_FAN_NUMBER is not None:
                s.aio_rpm = parsed.motherboard_fans.get(AIO_FAN_NUMBER)
            if s.disks:
                _attach_disk_readings(s.disks, parsed)
        except (requests.RequestException, ValueError):
            self._maybe_start_lhm()

        return s

    def _maybe_start_lhm(self) -> None:
        """Fire the on-demand LHM scheduled task once per Poller lifetime.

        The task is registered (via scripts/install/register-lhm-task.bat)
        with /RL HIGHEST so `schtasks /Run` launches LHM elevated without a
        UAC prompt. If the task isn't registered we log once and stop
        retrying — bar/charts still function, those LHM-fed fields just
        stay `--` until the user runs the installer.
        """
        if self._lhm_spawn_tried:
            return
        self._lhm_spawn_tried = True
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Run", "/TN", LHM_TASK_NAME],
                capture_output=True, text=True, timeout=2.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                log.info("LHM unreachable; triggered scheduled task %s", LHM_TASK_NAME)
            else:
                log.info("LHM unreachable; schtasks /Run %s failed rc=%d stderr=%s",
                         LHM_TASK_NAME, result.returncode,
                         (result.stderr or "").strip())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.info("LHM unreachable; schtasks /Run raised: %s", e)


# -------- brightness daemon ---------------------------------------------


def _poll_brightness() -> list[int | None]:
    """Query brightness_daemon.py for per-display percent values.

    Wire format: 'status' -> '0:40 1:38 2:50' (or '0:- 1:38 ...' for unknown).
    Returns [] if the daemon is unreachable — the bar silently hides the field.
    """
    try:
        with socket.create_connection((BRIGHTNESS_HOST, BRIGHTNESS_PORT),
                                      timeout=BRIGHTNESS_TIMEOUT_S) as s:
            s.sendall(b"status\n")
            reply = s.recv(512).decode("utf-8", errors="replace").strip()
    except (OSError, socket.timeout):
        return []

    if not reply or reply.startswith("err"):
        return []

    # 'i:pct' tokens, sorted by index so output order is stable
    parsed: dict[int, int | None] = {}
    for tok in reply.split():
        if ":" not in tok:
            continue
        i_str, v_str = tok.split(":", 1)
        try:
            i = int(i_str)
        except ValueError:
            continue
        try:
            parsed[i] = int(v_str)
        except ValueError:
            parsed[i] = None

    if not parsed:
        return []
    return [parsed.get(i) for i in range(max(parsed) + 1)]


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


@dataclass
class _LhmReadings:
    cpu_temp_c: float | None = None
    cpu_power_w: float | None = None
    gpu_fan_rpm: float | None = None  # averaged across GPU fans
    motherboard_fans: dict[int, float] = field(default_factory=dict)  # {fan_number: rpm}
    # Disk readings keyed by (model_string, occurrence_index) — same shape DISKS uses.
    disks: dict[tuple[str, int], tuple[float | None, float | None]] = field(default_factory=dict)


def _parse_lhm(tree: dict) -> _LhmReadings:
    """Single outer walk extracting every LHM value the bar/charts use.

    Disk sensors are intrinsically scoped to a device's subtree (so the same
    'Composite Temperature' label can appear in multiple NVMe subtrees), so
    each disk device kicks off a small sub-walk over its own subtree. That's
    still one outer walk + N tiny sub-walks instead of the previous 5+ full
    walks per tick.
    """
    r = _LhmReadings()
    cpu_core_avg: float | None = None       # fallback for cpu_temp_c
    cpu_power_fallback: float | None = None  # weaker fallback for cpu_power_w
    gpu_fan_rpms: list[float] = []
    model_occurrence: dict[str, int] = {}

    for node in _walk(tree):
        text = (node.get("Text") or "").strip()
        text_lower = text.lower()
        ntype = (node.get("Type") or "").lower()

        # Disk device subtree — text contains a DISKS model name and the
        # subtree exposes temp/activity sensors. Sub-walked here so that
        # composite/plain temperature lookups don't bleed across devices.
        for spec in DISKS:
            if spec.lhm_model in text:
                temp, activity = _disk_subtree_sensors(node)
                if temp is None and activity is None:
                    continue  # text matched, but it's not actually a device node
                idx = model_occurrence.get(spec.lhm_model, 0)
                r.disks[(spec.lhm_model, idx)] = (temp, activity)
                model_occurrence[spec.lhm_model] = idx + 1
                break

        val = _parse_lhm_value(node.get("Value"))
        if val is None:
            continue

        if ntype == "temperature":
            if text_lower == "cpu package" and r.cpu_temp_c is None:
                r.cpu_temp_c = val
            elif text_lower == "core average":
                cpu_core_avg = val  # remembered as fallback if no Package node

        elif ntype == "power":
            # "CPU Package" / "Package" — preferred. "CPU Cores [...] Package"
            # variants (some LHM builds) also count. Parens are explicit on the
            # last clause because `or X and Y` parses surprisingly otherwise.
            is_cpu_pkg_pow = (
                "cpu package" in text_lower
                or text_lower == "package"
                or ("cpu cores" in text_lower and "package" in text_lower)
            )
            if is_cpu_pkg_pow:
                if r.cpu_power_w is None:
                    r.cpu_power_w = val
            elif "package" in text_lower and cpu_power_fallback is None:
                cpu_power_fallback = val

        elif ntype == "fan":
            if text_lower.startswith("gpu fan"):
                gpu_fan_rpms.append(val)
            elif text_lower.startswith("fan #"):
                try:
                    n = int(text.split("#", 1)[1])
                    r.motherboard_fans[n] = val
                except (ValueError, IndexError):
                    pass

    if r.cpu_temp_c is None:
        r.cpu_temp_c = cpu_core_avg
    if r.cpu_power_w is None:
        r.cpu_power_w = cpu_power_fallback
    if gpu_fan_rpms:
        r.gpu_fan_rpm = sum(gpu_fan_rpms) / len(gpu_fan_rpms)
    return r


def _disk_subtree_sensors(device_node: dict) -> tuple[float | None, float | None]:
    """Return (temp_c, activity_pct) for a single LHM storage-device subtree.

    Temp prefers 'Composite Temperature' (NVMe), falls back to plain
    'Temperature' (SATA). Activity is the 'Total Activity' Load %.
    Walks the subtree once.
    """
    composite: float | None = None
    plain: float | None = None
    activity: float | None = None
    for node in _walk(device_node):
        ntype = (node.get("Type") or "").lower()
        text = (node.get("Text") or "").strip().lower()
        val = _parse_lhm_value(node.get("Value"))
        if val is None:
            continue
        if ntype == "temperature":
            if "composite" in text:
                composite = val
            elif text == "temperature":
                plain = val
        elif ntype == "load" and text == "total activity":
            activity = val
    return (composite if composite is not None else plain), activity


def _attach_disk_readings(disks: list[DiskReading], parsed: _LhmReadings) -> None:
    """Copy parsed LHM disk values onto the per-drive readings, respecting
    the (model, lhm_index) pairing in DISKS — needed when multiple drives
    share a model name (e.g. two BX500s)."""
    for reading in disks:
        spec = next((d for d in DISKS if d.label == reading.label), None)
        if spec is None:
            continue
        pair = parsed.disks.get((spec.lhm_model, spec.lhm_index))
        if pair is not None:
            reading.temp_c, reading.activity_pct = pair


# -------- ui -------------------------------------------------------------


def _fmt(val: float | None, unit: str, digits: int = 0) -> str:
    if val is None:
        return f"--{unit}"
    if digits == 0:
        return f"{val:.0f}{unit}"
    return f"{val:.{digits}f}{unit}"


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
    if color == COLOR_DEFAULT:
        return text
    return f'<span style="color:{color}">{text}</span>'


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

    # Brightness — one % per display in BRIGHTNESS_DISPLAY_ORDER. Any indices
    # not covered by the order list fall back to daemon order at the end.
    bri_sections: list[str] = []
    if s.brightness_pcts:
        order = [i for i in BRIGHTNESS_DISPLAY_ORDER if i < len(s.brightness_pcts)]
        order += [i for i in range(len(s.brightness_pcts)) if i not in order]
        vals = [f"{s.brightness_pcts[i]}%" if s.brightness_pcts[i] is not None else "--"
                for i in order]
        bri_sections.append("BRI " + " ".join(vals))

    # Night Light — warm colour when on to echo the actual tint
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
        " ".join(gpu_parts),
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

        self._load_position()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(REFRESH_MS)
        self._tick()

    def _tick(self) -> None:
        sample = self.poller.sample()
        publish_sample(sample)  # so bar.charts can subscribe instead of polling
        self.label.setText(render(sample))
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
        save_window_pos(CONFIG_FILE, self)

    def _load_position(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        load_window_pos(CONFIG_FILE, self, (screen.right() - 620, screen.top() + 8))


def main() -> int:
    _, log_path = setup_logging("bar", "hardware-bar.log")
    log.info("launch argv=%s log=%s", sys.argv[1:], log_path)

    app = QApplication(sys.argv)
    single = SingleInstance("hardware-bar", log)
    if single.signal_existing():
        return 0

    bar = Bar()
    single.become_primary(bar.close)
    bar.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
