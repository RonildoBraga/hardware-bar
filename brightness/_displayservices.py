"""Built-in display brightness on Apple Silicon via the private DisplayServices
framework (ctypes).

The Homebrew `brightness` CLI fails to *read* the built-in panel on Apple
Silicon (IODisplay path returns an error), so — like MonitorControl and friends
— we call DisplayServicesGet/SetBrightness directly. These take a
CGDirectDisplayID and a 0.0-1.0 float. External DDC displays still go through
m1ddc; this module is only for the internal panel.

Pure ctypes, no PyObjC dependency. Returns None / False if the frameworks or
symbols aren't available, so callers degrade gracefully.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("brightness")

_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_DS_PATH = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
# CGDisplayCreateUUIDFromDisplayID moved out of CoreGraphics into ColorSync on
# recent macOS; CFUUID/CFString helpers come from CoreFoundation.
_CS_PATH = "/System/Library/Frameworks/ColorSync.framework/ColorSync"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CFSTR_UTF8 = 0x08000100

try:
    _cg = ctypes.CDLL(_CG_PATH)
    _ds = ctypes.CDLL(_DS_PATH)
    _cs = ctypes.CDLL(_CS_PATH)
    _cf = ctypes.CDLL(_CF_PATH)

    for _fn in ("CGGetActiveDisplayList", "CGGetOnlineDisplayList"):
        getattr(_cg, _fn).argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        getattr(_cg, _fn).restype = ctypes.c_int32
    _cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
    _cg.CGDisplayIsBuiltin.restype = ctypes.c_int32

    _ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    _ds.DisplayServicesGetBrightness.restype = ctypes.c_int32
    _ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    _ds.DisplayServicesSetBrightness.restype = ctypes.c_int32

    _cs.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
    _cs.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
    _cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cf.CFUUIDCreateString.restype = ctypes.c_void_p
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _AVAILABLE = True
except (OSError, AttributeError) as e:  # pragma: no cover - platform dependent
    log.debug("DisplayServices unavailable: %s", e)
    _AVAILABLE = False


def _display_uuid(display_id: int) -> str | None:
    """Uppercase UUID string for a CGDirectDisplayID, matching m1ddc's IDs."""
    uuid_ref = _cs.CGDisplayCreateUUIDFromDisplayID(display_id)
    if not uuid_ref:
        return None
    s = _cf.CFUUIDCreateString(None, uuid_ref)
    try:
        if not s:
            return None
        buf = ctypes.create_string_buffer(64)
        if not _cf.CFStringGetCString(s, buf, 64, _CFSTR_UTF8):
            return None
        return buf.value.decode("ascii", "replace").upper()
    finally:
        if s:
            _cf.CFRelease(s)
        _cf.CFRelease(uuid_ref)


def online_external_uuids() -> set[str] | None:
    """UUIDs (uppercase) of connected non-built-in displays, per CoreGraphics.

    The authoritative way to drop m1ddc's stale ghost entries: m1ddc caches the
    EDID/UUID of a just-unplugged display and keeps listing it (sometimes
    *before* a real one), so the Mac backend keeps only the m1ddc displays whose
    UUID is actually online here. Counts connected-but-asleep (DPMS) displays.
    None if the APIs are unavailable.
    """
    if not _AVAILABLE:
        return None
    count = ctypes.c_uint32(0)
    ids = (ctypes.c_uint32 * 16)()
    if _cg.CGGetOnlineDisplayList(16, ids, ctypes.byref(count)) != 0:
        return None
    uuids: set[str] = set()
    for i in range(count.value):
        did = ids[i]
        if _cg.CGDisplayIsBuiltin(did):
            continue
        u = _display_uuid(did)
        if u:
            uuids.add(u)
    return uuids


def builtin_display_id() -> int | None:
    """CGDirectDisplayID of the built-in panel, or None if there isn't one."""
    if not _AVAILABLE:
        return None
    count = ctypes.c_uint32(0)
    ids = (ctypes.c_uint32 * 16)()
    if _cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
        return None
    for i in range(count.value):
        if _cg.CGDisplayIsBuiltin(ids[i]):
            return int(ids[i])
    return None


def online_external_count() -> int | None:
    """Number of currently *connected* non-built-in displays (per CoreGraphics).

    CoreGraphics is authoritative about what's physically attached; `m1ddc` can
    keep listing a ghost entry for a display you just unplugged (it caches the
    EDID/UUID). The Mac brightness backend caps the m1ddc list to this count so
    a disconnected monitor stops showing up as a dead `BRI --` entry. Counts
    connected-but-asleep (DPMS) displays as present. None if unavailable.
    """
    if not _AVAILABLE:
        return None
    count = ctypes.c_uint32(0)
    ids = (ctypes.c_uint32 * 16)()
    if _cg.CGGetOnlineDisplayList(16, ids, ctypes.byref(count)) != 0:
        return None
    return sum(1 for i in range(count.value) if not _cg.CGDisplayIsBuiltin(ids[i]))


def get_brightness_pct(display_id: int) -> int | None:
    """Return 0-100 brightness for the given display id, or None on failure."""
    if not _AVAILABLE:
        return None
    val = ctypes.c_float(-1.0)
    if _ds.DisplayServicesGetBrightness(display_id, ctypes.byref(val)) != 0:
        return None
    if val.value < 0:
        return None
    return int(round(val.value * 100))


def set_brightness_pct(display_id: int, pct: int) -> bool:
    """Set 0-100 brightness for the given display id. Returns True on success."""
    if not _AVAILABLE:
        return False
    pct = max(0, min(100, pct))
    return _ds.DisplayServicesSetBrightness(display_id, pct / 100.0) == 0
