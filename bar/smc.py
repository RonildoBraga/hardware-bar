"""AppleSMC fan-speed reader (no sudo) via IOKit IOConnectCallStructMethod.

Fan RPM isn't exposed through the IOHID temperature path (see bar/iohid.py),
so — like the classic `smc.c` and the exelban/Stats app — we open the AppleSMC
service and read the `FNum` (fan count) and `F<n>Ac` (fan N actual RPM) keys.
On Apple Silicon these return IEEE-754 floats (`flt ` type); older Macs use the
`fpe2` fixed-point encoding, which we also decode.

Pure ctypes; no root required. Any failure (non-Apple SMC, struct mismatch,
API change) degrades to an empty fan list and the bar hides the FAN field.
"""

from __future__ import annotations

import ctypes
import logging
import struct

log = logging.getLogger("bar")

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_LIBSYSTEM = "/usr/lib/libSystem.B.dylib"

_KERNEL_INDEX_SMC = 2
_SMC_CMD_READ_BYTES = 5
_SMC_CMD_READ_KEYINFO = 9


class _SMCVers(ctypes.Structure):
    _fields_ = [("major", ctypes.c_char), ("minor", ctypes.c_char),
                ("build", ctypes.c_char), ("reserved", ctypes.c_char),
                ("release", ctypes.c_uint16)]


class _SMCPLimit(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint16), ("length", ctypes.c_uint16),
                ("cpuPLimit", ctypes.c_uint32), ("gpuPLimit", ctypes.c_uint32),
                ("memPLimit", ctypes.c_uint32)]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [("dataSize", ctypes.c_uint32), ("dataType", ctypes.c_uint32),
                ("dataAttributes", ctypes.c_char)]


class _SMCKeyData(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", _SMCVers),
        ("pLimitData", _SMCPLimit),
        ("keyInfo", _SMCKeyInfo),
        ("result", ctypes.c_char),
        ("status", ctypes.c_char),
        ("data8", ctypes.c_char),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]


def _fourcc(s: str) -> int:
    b = s.encode("ascii")
    return (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]


def _type_str(dtype: int) -> str:
    return bytes([(dtype >> 24) & 0xFF, (dtype >> 16) & 0xFF,
                  (dtype >> 8) & 0xFF, dtype & 0xFF]).decode("ascii", "replace")


class FanReader:
    def __init__(self) -> None:
        self._ok = False
        self._conn = None
        self._n_fans = 0
        try:
            self._open()
            self._n_fans = int(self._read_key("FNum") or 0)
            self._ok = self._n_fans > 0
        except Exception as e:  # pragma: no cover - platform dependent
            log.debug("SMC fan reader unavailable: %s", e)

    def _open(self) -> None:
        self._iokit = ctypes.CDLL(_IOKIT)
        libsystem = ctypes.CDLL(_LIBSYSTEM)

        self._iokit.IOServiceMatching.restype = ctypes.c_void_p
        self._iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        self._iokit.IOServiceGetMatchingService.restype = ctypes.c_uint
        self._iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        self._iokit.IOServiceOpen.restype = ctypes.c_int
        self._iokit.IOServiceOpen.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        self._iokit.IOConnectCallStructMethod.restype = ctypes.c_int
        self._iokit.IOConnectCallStructMethod.argtypes = [
            ctypes.c_uint, ctypes.c_uint,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]

        task = ctypes.c_uint.in_dll(libsystem, "mach_task_self_").value
        device = self._iokit.IOServiceGetMatchingService(
            0, self._iokit.IOServiceMatching(b"AppleSMC"))
        if not device:
            raise RuntimeError("AppleSMC service not found")
        conn = ctypes.c_uint(0)
        if self._iokit.IOServiceOpen(device, task, 0, ctypes.byref(conn)) != 0:
            raise RuntimeError("IOServiceOpen(AppleSMC) failed")
        self._conn = conn.value

    def _call(self, inp: _SMCKeyData) -> _SMCKeyData:
        out = _SMCKeyData()
        out_size = ctypes.c_size_t(ctypes.sizeof(_SMCKeyData))
        rc = self._iokit.IOConnectCallStructMethod(
            self._conn, _KERNEL_INDEX_SMC,
            ctypes.byref(inp), ctypes.sizeof(_SMCKeyData),
            ctypes.byref(out), ctypes.byref(out_size))
        if rc != 0:
            raise RuntimeError(f"IOConnectCallStructMethod rc={rc}")
        return out

    def _read_key(self, key: str) -> float | None:
        # 1) key info (size + type)
        info_in = _SMCKeyData()
        info_in.key = _fourcc(key)
        info_in.data8 = bytes([_SMC_CMD_READ_KEYINFO])
        info = self._call(info_in)
        size = info.keyInfo.dataSize
        dtype = info.keyInfo.dataType
        if size == 0:
            return None
        # 2) read bytes
        read_in = _SMCKeyData()
        read_in.key = _fourcc(key)
        read_in.data8 = bytes([_SMC_CMD_READ_BYTES])
        read_in.keyInfo.dataSize = size
        read_in.keyInfo.dataType = dtype
        out = self._call(read_in)
        raw = bytes(out.bytes[:size])
        return _decode(raw, _type_str(dtype))

    def read_rpms(self) -> list[float]:
        """Return actual RPM for each fan (skips fans reading 0/None)."""
        if not self._ok:
            return []
        rpms: list[float] = []
        for i in range(self._n_fans):
            try:
                v = self._read_key(f"F{i}Ac")
            except Exception:
                v = None
            if v is not None:
                rpms.append(v)
        return rpms


def _decode(raw: bytes, dtype: str) -> float | None:
    dtype = dtype.rstrip("\x00 ")
    try:
        if dtype in ("flt", "fp"):
            if len(raw) >= 4:
                return struct.unpack("<f", raw[:4])[0]
        if dtype == "fpe2" and len(raw) >= 2:  # big-endian fixed point, 2 frac bits
            return struct.unpack(">H", raw[:2])[0] / 4.0
        if dtype in ("ui8", "ui16", "ui32"):
            return float(int.from_bytes(raw, "big"))
        # FNum on Apple Silicon often comes back as a small int — try big-endian.
        if raw:
            return float(int.from_bytes(raw, "big"))
    except Exception:
        return None
    return None
