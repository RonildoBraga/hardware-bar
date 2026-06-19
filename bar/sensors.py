"""Selects the hardware-sensor backend for the current platform.

A backend exposes `populate(sample)`, which fills the OS-specific fields of a
Sample (CPU %, GPU, temps, power, fans, disks). The cross-platform fields
(clock/ram/network + brightness/nightlight/audio) are handled by bar.main's
Poller, so the backends stay small.
"""

from __future__ import annotations

import logging

from _common import IS_MACOS, IS_WINDOWS
from .model import Sample

log = logging.getLogger("bar")


class _NullBackend:
    """Fallback on unsupported platforms — CPU% only, everything else None."""

    def populate(self, s: Sample) -> None:
        try:
            import psutil
            s.cpu_pct = psutil.cpu_percent(interval=None)
        except Exception:
            pass
        s.disks = []


def make_backend():
    if IS_WINDOWS:
        from ._win import WindowsBackend
        return WindowsBackend()
    if IS_MACOS:
        from ._mac import MacBackend
        return MacBackend()
    log.warning("unsupported platform; hardware sensors disabled")
    return _NullBackend()
