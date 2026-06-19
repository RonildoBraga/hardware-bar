"""Pure brightness math — no ctypes, no monitorcontrol, importable anywhere.

The Windows HDR path drives an SDR white-level (DisplayConfig) instead of
DDC/CI luminance; this maps a user-facing 0..100 percent onto that range. The
unit tests pin this math (tests/test_brightness.py), so it lives in its own
dependency-free module and is re-exported by `brightness.core`.
"""

from __future__ import annotations

from typing import NamedTuple

# SDR white level values roughly map 1000 (80 nits) -> 6250 (500 nits).
# Expose a 0..100 "percent" to users and linearly convert.
SDR_WL_MIN = 1000
SDR_WL_MAX = 6250


def sdr_pct_to_wl(pct: int) -> int:
    pct = max(0, min(100, pct))
    return int(SDR_WL_MIN + (SDR_WL_MAX - SDR_WL_MIN) * pct / 100)


def sdr_wl_to_pct(wl: int) -> int:
    return int(round((wl - SDR_WL_MIN) / (SDR_WL_MAX - SDR_WL_MIN) * 100))


class HdrAdjust(NamedTuple):
    cur_wl: int
    new_wl: int
    cur_pct: int
    new_pct: int
