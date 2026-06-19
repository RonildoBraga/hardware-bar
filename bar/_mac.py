"""macOS hardware-sensor backend for the bar.

What's freely available (no privileges):
    - psutil -> CPU %, (RAM/network/clock handled by the shared Poller)

What Apple gates behind root (`powermetrics`): GPU activity/power, CPU package
power, die temperatures, and fan RPM. Rather than run the GUI as root, a small
privileged sampler daemon (scripts/install/ + macos/powermetrics_daemon.py)
streams `powermetrics` and writes parsed samples to a shared JSON file; this
backend reads that file if it's fresh. With no daemon installed, those fields
stay None and the bar shows `--` for them — same graceful degradation as a
Windows box without LibreHardwareMonitor.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import psutil

from .model import DiskReading, Sample

log = logging.getLogger("bar")

# Fixed, well-known path (NOT tempfile.gettempdir(), which is per-user): the
# powermetrics daemon runs as root and must write somewhere the user-level bar
# can read. Kept in sync with macos/powermetrics_daemon.py.
PM_FILE = Path("/tmp/hardware-bar-powermetrics.json")
PM_STALE_S = 5.0  # daemon samples ~1 Hz; older than this = treat as absent


class MacBackend:
    def __init__(self) -> None:
        # Prime psutil's internal "since last call" CPU counter so the first
        # populate() returns a real delta rather than 0/None.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
        # In-process sensors (no sudo): SoC/SSD temps via IOHID, fan RPM via SMC.
        self._thermals = _make_thermal_reader()
        self._fans = _make_fan_reader()

    def read_cpu_pct(self) -> float | None:
        try:
            return psutil.cpu_percent(interval=None)
        except Exception:
            return None

    def populate(self, s: Sample) -> None:
        s.cpu_pct = self.read_cpu_pct()
        s.disks = []
        self._apply_thermals(s)
        self._apply_fans(s)
        self._apply_powermetrics(s)

    def _apply_thermals(self, s: Sample) -> None:
        if self._thermals is None:
            return
        t = self._thermals.read()
        s.cpu_temp_c = t.get("cpu_temp_c")  # SoC die temp (CPU+GPU share the die)
        ssd = t.get("ssd_temp_c")
        if ssd is not None:
            s.disks = [DiskReading(label="SSD", temp_c=ssd, activity_pct=None)]

    def _apply_fans(self, s: Sample) -> None:
        if self._fans is None:
            return
        rpms = self._fans.read_rpms()
        if rpms:
            # Render shows case_fans as the plain "FAN <rpm...>" group.
            s.case_fans = [(i, rpm) for i, rpm in enumerate(rpms)]

    def _apply_powermetrics(self, s: Sample) -> None:
        """Overlay GPU%/power (and CPU power/clock) from the privileged daemon's
        file. Temps/fans come from the in-process readers above; powermetrics
        only *adds* what it has, never nulls out an already-read value."""
        data = self._read_pm()
        if data is None:
            return
        for key in ("gpu_pct", "gpu_power_w", "gpu_temp_c", "cpu_power_w", "cpu_clock_ghz"):
            val = data.get(key)
            if val is not None:
                setattr(s, key, val)

    def _read_pm(self) -> dict | None:
        try:
            st = PM_FILE.stat()
        except OSError:
            return None
        if time.time() - st.st_mtime > PM_STALE_S:
            return None
        try:
            return json.loads(PM_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# -------- in-process sensor readers (lazy, degrade to None) --------------

def _make_thermal_reader():
    try:
        from .iohid import ThermalReader
        r = ThermalReader()
        return r if r._ok else None
    except Exception as e:
        log.debug("thermal reader init failed: %s", e)
        return None


def _make_fan_reader():
    try:
        from .smc import FanReader
        r = FanReader()
        return r if r._ok else None
    except Exception as e:
        log.debug("fan reader init failed: %s", e)
        return None
