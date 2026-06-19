"""Shared data model + LibreHardwareMonitor tree parsing.

Pure Python, no platform imports — so it loads on any OS and the unit tests
(tests/test_lhm.py) and bar.charts can import `Sample`/`DiskReading`/`DISKS`
and the LHM parsers without dragging in PyQt6, pynvml, or ctypes.

The disk/fan config here describes Ronildo's Windows box (the LHM source);
the macOS backend ignores it and enumerates its own hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # Blue-light-reduction state (Windows Night Light / macOS Night Shift);
    # None if unreadable.
    nightlight_on: bool | None = None
    # Default audio output state; each field is None if the query failed.
    volume_pct: int | None = None
    volume_muted: bool | None = None
    audio_device: str | None = None


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
