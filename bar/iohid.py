"""Apple Silicon thermal sensors via IOKit's IOHIDEventSystem (no sudo).

macOS exposes the SoC die/device temperature sensors through the same private
IOHIDEventSystemClient API that menu-bar stats apps use. Reading them needs
neither root nor `powermetrics` — just ctypes against IOKit + CoreFoundation,
with PyObjC used only to build the CFDictionary matching filter.

Sensor naming (M-series): `PMU tdieN` are SoC die junction temps (the closest
thing to a CPU/GPU temperature), `NAND CHx temp` is the internal SSD, `gas
gauge battery` is the battery. We average the `tdie` set for a representative
SoC temperature and the `NAND` set for SSD temperature.

If anything is unavailable (non-Apple-Silicon, API change, PyObjC missing) the
reader degrades to empty readings and the bar shows `--`.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("bar")

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

# kIOHIDEventTypeTemperature; field index is (eventType << 16).
_EVENT_TEMPERATURE = 15
_PAGE_APPLE_VENDOR = 0xFF00
_USAGE_TEMP_SENSOR = 0x05
_CFSTR_UTF8 = 0x08000100


class ThermalReader:
    """Caches the matched temperature services and re-reads them each tick."""

    def __init__(self) -> None:
        self._ok = False
        self._services_array = None
        self._services: list[tuple[int, str]] = []  # (service_ptr, name)
        try:
            self._setup()
            self._ok = bool(self._services)
        except Exception as e:  # pragma: no cover - platform dependent
            log.debug("IOHID thermal reader unavailable: %s", e)

    def _setup(self) -> None:
        import objc
        from Foundation import NSDictionary, NSNumber

        self._iokit = ctypes.CDLL(_IOKIT)
        self._cf = ctypes.CDLL(_CF)
        self._objc = objc

        for fn, res, args in [
            ("IOHIDEventSystemClientCreate", ctypes.c_void_p, [ctypes.c_void_p]),
            ("IOHIDEventSystemClientCopyServices", ctypes.c_void_p, [ctypes.c_void_p]),
            ("IOHIDServiceClientCopyProperty", ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
            ("IOHIDServiceClientCopyEvent", ctypes.c_void_p,
             [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]),
            ("IOHIDEventGetFloatValue", ctypes.c_double, [ctypes.c_void_p, ctypes.c_int32]),
        ]:
            f = getattr(self._iokit, fn)
            f.restype, f.argtypes = res, args
        self._iokit.IOHIDEventSystemClientSetMatching.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._cf.CFArrayGetCount.restype = ctypes.c_long
        self._cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self._cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self._cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]

        self._product_key = self._cf.CFStringCreateWithCString(None, b"Product", _CFSTR_UTF8)

        client = self._iokit.IOHIDEventSystemClientCreate(None)
        if not client:
            raise RuntimeError("IOHIDEventSystemClientCreate failed")
        matching = NSDictionary.dictionaryWithObjectsAndKeys_(
            NSNumber.numberWithInt_(_PAGE_APPLE_VENDOR), "PrimaryUsagePage",
            NSNumber.numberWithInt_(_USAGE_TEMP_SENSOR), "PrimaryUsage", None)
        self._iokit.IOHIDEventSystemClientSetMatching(
            client, ctypes.c_void_p(objc.pyobjc_id(matching)))

        services = self._iokit.IOHIDEventSystemClientCopyServices(client)
        if not services:
            raise RuntimeError("no temperature services")
        self._services_array = services  # retain for our lifetime
        n = self._cf.CFArrayGetCount(services)
        for i in range(n):
            svc = self._cf.CFArrayGetValueAtIndex(services, i)
            nameptr = self._iokit.IOHIDServiceClientCopyProperty(
                svc, ctypes.c_void_p(self._product_key))
            name = str(objc.objc_object(c_void_p=nameptr)) if nameptr else ""
            self._services.append((svc, name))

    def _read_all(self) -> dict[str, list[float]]:
        field = _EVENT_TEMPERATURE << 16
        out: dict[str, list[float]] = {}
        for svc, name in self._services:
            ev = self._iokit.IOHIDServiceClientCopyEvent(svc, _EVENT_TEMPERATURE, 0, 0)
            if not ev:
                continue
            val = self._iokit.IOHIDEventGetFloatValue(ev, field)
            self._cf.CFRelease(ctypes.c_void_p(ev))  # CopyEvent is +1; release it
            if val and val > 0:
                out.setdefault(name, []).append(val)
        return out

    def read(self) -> dict[str, float | None]:
        """Return {'cpu_temp_c', 'ssd_temp_c'} — values None if unavailable."""
        if not self._ok:
            return {"cpu_temp_c": None, "ssd_temp_c": None}
        try:
            readings = self._read_all()
        except Exception as e:  # pragma: no cover
            log.debug("thermal read failed: %s", e)
            return {"cpu_temp_c": None, "ssd_temp_c": None}

        die = [v for name, vals in readings.items() if name.lower().startswith("pmu tdie")
               for v in vals]
        nand = [v for name, vals in readings.items() if "nand" in name.lower() for v in vals]
        return {
            "cpu_temp_c": (sum(die) / len(die)) if die else None,
            "ssd_temp_c": (sum(nand) / len(nand)) if nand else None,
        }
