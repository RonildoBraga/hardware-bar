"""Windows hardware-sensor backend for the bar.

Data sources:
    - PDH        -> CPU % (Processor Utility, matches Task Manager on Win8+)
    - pynvml     -> GPU %, GPU temp, VRAM, GPU power/clock (NVIDIA)
    - LHM HTTP   -> CPU package temp/power, NVMe/SATA temps + activity, fan tachos
                    (requires LibreHardwareMonitor with its Remote Web Server on
                    port 8085; auto-launched once via an on-demand scheduled task)

Exposes WindowsBackend.populate(sample), which fills every platform-specific
field; the cross-platform fields (clock/ram/net + brightness/nightlight/audio)
stay in bar.main's Poller.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from ctypes import wintypes

import psutil
import pynvml
import requests

from .model import (
    AIO_FAN_NUMBER,
    CASE_FAN_NUMBERS,
    DISKS,
    DiskReading,
    Sample,
    _LhmReadings,
    _attach_disk_readings,
    _parse_lhm,
)

log = logging.getLogger("bar")

LHM_URL = "http://localhost:8085/data.json"
LHM_TIMEOUT_S = 0.5
LHM_CACHE_TTL_S = 30.0
LHM_FAILURE_LOG_INTERVAL_S = 30.0
LHM_TASK_NAME = "LibreHardwareMonitor"


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


class WindowsBackend:
    def __init__(self) -> None:
        self._nvml_ok = False
        try:
            pynvml.nvmlInit()
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_ok = True
        except pynvml.NVMLError:
            self._gpu_handle = None

        self._cpu_utility = CpuUtility()

        # LHM auto-spawn: set once per backend lifetime on first unreachable
        # LHM, so we don't spam schtasks every second.
        self._lhm_spawn_tried = False
        self._last_lhm: _LhmReadings | None = None
        self._last_lhm_at: float | None = None
        self._last_lhm_failure_log = 0.0

    def read_cpu_pct(self) -> float | None:
        # Prefer Processor Utility (matches Task Manager on Win8+); fall back to
        # psutil's Processor Time if PDH isn't available.
        pct = self._cpu_utility.sample()
        if pct is None:
            try:
                pct = psutil.cpu_percent(interval=None)
            except Exception:
                pass
        return pct

    def populate(self, s: Sample) -> None:
        s.cpu_pct = self.read_cpu_pct()

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

        # lhm (cpu package temp+power, gpu/case fans, per-disk temp+activity).
        try:
            r = requests.get(LHM_URL, timeout=LHM_TIMEOUT_S)
            r.raise_for_status()
            parsed = _parse_lhm(r.json())
            self._last_lhm = parsed
            self._last_lhm_at = time.monotonic()
            self._apply_lhm_readings(s, parsed)
        except (requests.RequestException, ValueError) as e:
            if self._apply_cached_lhm_readings(s):
                self._log_lhm_failure("using cached LHM readings", e)
            else:
                self._log_lhm_failure("no cached LHM readings available", e)
            self._maybe_start_lhm()

    def _apply_lhm_readings(self, sample: Sample, parsed: _LhmReadings) -> None:
        sample.cpu_temp_c = parsed.cpu_temp_c
        sample.cpu_power_w = parsed.cpu_power_w
        sample.gpu_fan_rpm = parsed.gpu_fan_rpm
        sample.case_fans = [(n, parsed.motherboard_fans.get(n)) for n in CASE_FAN_NUMBERS]
        if AIO_FAN_NUMBER is not None:
            sample.aio_rpm = parsed.motherboard_fans.get(AIO_FAN_NUMBER)
        if sample.disks:
            _attach_disk_readings(sample.disks, parsed)

    def _apply_cached_lhm_readings(self, sample: Sample) -> bool:
        if self._last_lhm is None or self._last_lhm_at is None:
            return False
        if time.monotonic() - self._last_lhm_at > LHM_CACHE_TTL_S:
            return False
        self._apply_lhm_readings(sample, self._last_lhm)
        return True

    def _log_lhm_failure(self, detail: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_lhm_failure_log < LHM_FAILURE_LOG_INTERVAL_S:
            return
        self._last_lhm_failure_log = now
        log.info("LHM poll failed (%s): %s: %s", detail, type(exc).__name__, exc)

    def _maybe_start_lhm(self) -> None:
        """Fire the on-demand LHM scheduled task once per backend lifetime.

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
