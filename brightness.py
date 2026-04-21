"""brightness.py — adjust per-monitor brightness.

Uses DDC/CI (via monitorcontrol) on SDR displays, and Windows'
DisplayConfigSetDeviceInfo / SDRWhiteLevel API on displays that currently have
HDR enabled (where DDC/CI brightness is typically ignored by the firmware).

Usage:
    brightness.py --list                  # show all displays and current state
    brightness.py <index> <delta>         # change brightness by signed delta, 0-100 scale

Examples:
    brightness.py 0 +5
    brightness.py 1 -10
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import NamedTuple, Optional

from monitorcontrol import get_monitors, Monitor, VCPError

# -------- Win32 DisplayConfig API via ctypes -----------------------------

user32 = ctypes.WinDLL("user32")

QDC_ONLY_ACTIVE_PATHS = 0x00000002

# DISPLAYCONFIG_DEVICE_INFO_TYPE values
GET_TARGET_NAME                = 2
GET_ADVANCED_COLOR_INFO        = 9
GET_SDR_WHITE_LEVEL            = 11
# The "set SDR white level" type is not publicly documented; community-known
# value that Microsoft's internal tools use:
SET_SDR_WHITE_LEVEL            = -18  # signed; == 0xFFFFFFEE as unsigned


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type",       ctypes.c_int32),
        ("size",       wintypes.UINT),
        ("adapterId",  LUID),
        ("id",         wintypes.UINT),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId",   LUID),
        ("id",          wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId",        LUID),
        ("id",               wintypes.UINT),
        ("modeInfoIdx",      wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation",         wintypes.UINT),
        ("scaling",          wintypes.UINT),
        ("refreshRate",      DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable",  wintypes.BOOL),
        ("statusFlags",      wintypes.UINT),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags",      wintypes.UINT),
    ]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header",                    DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags",                     wintypes.UINT),
        ("outputTechnology",          wintypes.UINT),
        ("edidManufactureId",         wintypes.USHORT),
        ("edidProductCodeId",         wintypes.USHORT),
        ("connectorInstance",         wintypes.UINT),
        ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
        ("monitorDevicePath",         ctypes.c_wchar * 128),
    ]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header",              DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value",               wintypes.UINT),
        ("colorEncoding",       wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]


class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [
        ("header",        DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("SDRWhiteLevel", wintypes.ULONG),
    ]


class DISPLAYCONFIG_SET_SDR_WHITE_LEVEL(ctypes.Structure):
    """Undocumented. 'finalValue=1' tells Windows this is the committed value."""
    _fields_ = [
        ("header",        DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("SDRWhiteLevel", wintypes.ULONG),
        ("finalValue",    wintypes.BYTE),
    ]


# Function prototypes
user32.GetDisplayConfigBufferSizes.argtypes = [
    wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
]
user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG

user32.QueryDisplayConfig.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(wintypes.UINT), ctypes.c_void_p,
    ctypes.POINTER(wintypes.UINT), ctypes.c_void_p,
    ctypes.c_void_p,
]
user32.QueryDisplayConfig.restype = wintypes.LONG

user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG

user32.DisplayConfigSetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigSetDeviceInfo.restype = wintypes.LONG


# -------- DisplayConfig high-level helpers -------------------------------


class DisplayTarget(NamedTuple):
    name:       str
    adapterId:  LUID
    targetId:   int


def enumerate_display_targets() -> list[DisplayTarget]:
    n_paths = wintypes.UINT(0)
    n_modes = wintypes.UINT(0)
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS,
                                          ctypes.byref(n_paths),
                                          ctypes.byref(n_modes)) != 0:
        return []

    paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
    # Mode buffer: we allocate as raw bytes since we don't use modes here.
    mode_size = 64  # DISPLAYCONFIG_MODE_INFO struct is 64 bytes
    modes = (ctypes.c_byte * (mode_size * n_modes.value))()

    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS,
                                 ctypes.byref(n_paths), paths,
                                 ctypes.byref(n_modes), modes,
                                 None) != 0:
        return []

    out: list[DisplayTarget] = []
    for i in range(n_paths.value):
        tinfo = paths[i].targetInfo
        name_struct = DISPLAYCONFIG_TARGET_DEVICE_NAME()
        name_struct.header.type      = GET_TARGET_NAME
        name_struct.header.size      = ctypes.sizeof(name_struct)
        name_struct.header.adapterId = tinfo.adapterId
        name_struct.header.id        = tinfo.id
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(name_struct)) == 0:
            friendly = name_struct.monitorFriendlyDeviceName.strip()
        else:
            friendly = f"Display {i}"
        out.append(DisplayTarget(friendly, tinfo.adapterId, tinfo.id))
    return out


def is_hdr_enabled(target: DisplayTarget) -> bool:
    info = DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO()
    info.header.type      = GET_ADVANCED_COLOR_INFO
    info.header.size      = ctypes.sizeof(info)
    info.header.adapterId = target.adapterId
    info.header.id        = target.targetId
    if user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)) != 0:
        return False
    return bool(info.value & 0x2)  # bit 1 = advancedColorEnabled


def get_sdr_white_level(target: DisplayTarget) -> int:
    """Return current SDR white level (units of 1/1000 * 80 nits; default 1000)."""
    info = DISPLAYCONFIG_SDR_WHITE_LEVEL()
    info.header.type      = GET_SDR_WHITE_LEVEL
    info.header.size      = ctypes.sizeof(info)
    info.header.adapterId = target.adapterId
    info.header.id        = target.targetId
    if user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)) != 0:
        return 1000
    return info.SDRWhiteLevel or 1000


def set_sdr_white_level(target: DisplayTarget, level: int) -> bool:
    info = DISPLAYCONFIG_SET_SDR_WHITE_LEVEL()
    info.header.type      = SET_SDR_WHITE_LEVEL
    info.header.size      = ctypes.sizeof(info)
    info.header.adapterId = target.adapterId
    info.header.id        = target.targetId
    info.SDRWhiteLevel    = max(1000, min(6250, level))
    info.finalValue       = 1
    return user32.DisplayConfigSetDeviceInfo(ctypes.byref(info)) == 0


# -------- Mapping monitorcontrol index <-> DisplayTarget -----------------

# SDR white level values roughly map 1000 (80 nits) -> 6250 (500 nits).
# Expose a 0..100 "percent" to users and linearly convert.
SDR_WL_MIN = 1000
SDR_WL_MAX = 6250


def sdr_pct_to_wl(pct: int) -> int:
    pct = max(0, min(100, pct))
    return int(SDR_WL_MIN + (SDR_WL_MAX - SDR_WL_MIN) * pct / 100)


def sdr_wl_to_pct(wl: int) -> int:
    return int(round((wl - SDR_WL_MIN) / (SDR_WL_MAX - SDR_WL_MIN) * 100))


def _monitor_name(m: Monitor) -> str:
    """Best-effort model name from monitorcontrol. Returns '' if unavailable."""
    try:
        with m:
            caps = m.get_vcp_capabilities()
            if isinstance(caps, dict):
                return (caps.get("model") or "").strip()
    except Exception:
        pass
    return ""


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def build_display_index() -> list[tuple[DisplayTarget, Optional[Monitor]]]:
    """Return (DisplayTarget, monitorcontrol.Monitor|None) for each active
    display, in DisplayConfig (= Windows Display 1/2/3) order.

    Matching strategy:
     1. Name match — if monitorcontrol reports a model string that overlaps
        the DisplayConfig friendly name (e.g. both say 'Cintiq 16').
     2. Positional fill — leftover DisplayConfig targets get paired with
        leftover monitorcontrol entries in order. Works because both APIs
        usually enumerate adapters/outputs in the same sequence.
    """
    targets = enumerate_display_targets()
    monitors = get_monitors()
    mc_names = [(_norm(_monitor_name(m)), m) for m in monitors]
    used_mc: set[int] = set()
    results: list[tuple[DisplayTarget, Optional[Monitor]]] = [(t, None) for t in targets]

    # Pass 1: name match
    for ti, t in enumerate(targets):
        tname = _norm(t.name)
        for mi, (mcname, m) in enumerate(mc_names):
            if mi in used_mc:
                continue
            if mcname and (mcname in tname or tname in mcname):
                results[ti] = (t, m)
                used_mc.add(mi)
                break

    # Pass 2: positional fill for unmatched targets
    leftover_mc = [i for i in range(len(mc_names)) if i not in used_mc]
    for ti, (t, m) in enumerate(results):
        if m is None and leftover_mc:
            mi = leftover_mc.pop(0)
            results[ti] = (t, mc_names[mi][1])
            used_mc.add(mi)

    return results


# -------- Public operations ---------------------------------------------


def list_displays() -> int:
    rows = build_display_index()
    print(f"Found {len(rows)} display(s) (indexed in Windows display order):\n")
    for i, (t, m) in enumerate(rows):
        hdr = is_hdr_enabled(t)
        print(f"[{i}] {t.name}   HDR={'ON' if hdr else 'off'}")
        if m is not None:
            try:
                with m:
                    b = m.get_luminance()
                    print(f"     DDC/CI brightness: {b}")
            except VCPError as e:
                print(f"     DDC/CI: not available ({e})")
        else:
            print("     DDC/CI: no matching monitorcontrol entry")
        if hdr:
            wl = get_sdr_white_level(t)
            print(f"     SDR white level: {wl}  (~{sdr_wl_to_pct(wl)}% on 0-100 scale)")
    return 0


def adjust(index: int, delta: int) -> int:
    rows = build_display_index()
    if index < 0 or index >= len(rows):
        print(f"Invalid display index {index}. Valid: 0..{len(rows)-1}")
        return 2
    target, m = rows[index]
    hdr_on = is_hdr_enabled(target)

    if hdr_on:
        cur_wl = get_sdr_white_level(target)
        cur_pct = sdr_wl_to_pct(cur_wl)
        new_pct = max(0, min(100, cur_pct + delta))
        new_wl = sdr_pct_to_wl(new_pct)
        ok = set_sdr_white_level(target, new_wl)
        status = "ok" if ok else "FAILED"
        print(f"[{index}] {target.name} (HDR): SDR white level {cur_wl} -> {new_wl}  "
              f"({cur_pct}% -> {new_pct}%)  {status}")
        return 0 if ok else 1

    # SDR path: DDC/CI
    if m is None:
        print(f"[{index}] {target.name} (SDR): no DDC/CI device available")
        return 1
    try:
        with m:
            cur = m.get_luminance()
            new = max(0, min(100, cur + delta))
            m.set_luminance(new)
            print(f"[{index}] {target.name} (SDR): DDC/CI brightness {cur} -> {new}")
            return 0
    except VCPError as e:
        print(f"[{index}] {target.name} (SDR): DDC/CI not available ({e})")
        return 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        return list_displays()
    if len(args) != 2:
        print("Usage: brightness.py <index> <delta>  |  brightness.py --list")
        return 2
    try:
        return adjust(int(args[0]), int(args[1]))
    except ValueError:
        print("Both index and delta must be integers.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
