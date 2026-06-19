"""Windows audio backend — master volume, mute, default output cycling.

Uses pycaw (IAudioEndpointVolume) for volume/mute and the undocumented
IPolicyConfig COM interface for switching the default render endpoint.
"""

from __future__ import annotations

import logging
from ctypes import HRESULT, c_int
from ctypes.wintypes import LPCWSTR

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.pycaw import (
    AudioDeviceState,
    AudioUtilities,
    IMMDeviceEnumerator,
)

from ._filter import load_exclude_patterns
from ._types import OutputDevice

log = logging.getLogger("audio")

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
IID_IPolicyConfig        = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
ERENDER                  = 0
DEVICE_STATE_ACTIVE      = 1


class IPolicyConfig(IUnknown):
    """Undocumented COM interface Windows Settings uses to change the default endpoint.

    We only need SetDefaultEndpoint here; the other slots are stubs so the
    vtable index matches.
    """
    _iid_ = IID_IPolicyConfig
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat"),
        COMMETHOD([], HRESULT, "GetDeviceFormat"),
        COMMETHOD([], HRESULT, "ResetDeviceFormat"),
        COMMETHOD([], HRESULT, "SetDeviceFormat"),
        COMMETHOD([], HRESULT, "GetProcessingPeriod"),
        COMMETHOD([], HRESULT, "SetProcessingPeriod"),
        COMMETHOD([], HRESULT, "GetShareMode"),
        COMMETHOD([], HRESULT, "SetShareMode"),
        COMMETHOD([], HRESULT, "GetPropertyValue"),
        COMMETHOD([], HRESULT, "SetPropertyValue"),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], LPCWSTR, "wszDeviceId"),
                  (["in"], c_int,   "eRole")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility"),
    ]


# -------- volume / mute ------------------------------------------------

def _endpoint_volume():
    try:
        spk = AudioUtilities.GetSpeakers()
        return spk, spk.EndpointVolume if spk else None
    except Exception as e:
        log.debug("GetSpeakers failed: %s", e)
        return None, None


def _read_volume(ev) -> int | None:
    if ev is None:
        return None
    try:
        return int(round(ev.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return None


def _read_mute(ev) -> bool | None:
    if ev is None:
        return None
    try:
        return bool(ev.GetMute())
    except Exception:
        return None


def get_volume_pct() -> int | None:
    _, ev = _endpoint_volume()
    return _read_volume(ev)


def get_mute() -> bool | None:
    _, ev = _endpoint_volume()
    return _read_mute(ev)


def set_volume_delta(delta_pct: int) -> int | None:
    _, ev = _endpoint_volume()
    if ev is None:
        return None
    try:
        cur = ev.GetMasterVolumeLevelScalar()
        new = max(0.0, min(1.0, cur + delta_pct / 100.0))
        ev.SetMasterVolumeLevelScalar(new, None)
        return int(round(new * 100))
    except Exception as e:
        log.error("set volume failed: %s", e)
        return None


def set_mute(muted: bool) -> bool | None:
    _, ev = _endpoint_volume()
    if ev is None:
        return None
    try:
        ev.SetMute(1 if muted else 0, None)
        return muted
    except Exception as e:
        log.error("set mute failed: %s", e)
        return None


def toggle_mute() -> bool | None:
    cur = get_mute()
    if cur is None:
        return None
    return set_mute(not cur)


# -------- device enumeration + switching -------------------------------

def get_default_device() -> OutputDevice | None:
    try:
        spk = AudioUtilities.GetSpeakers()
        if spk is None:
            return None
        return OutputDevice(id=spk.id, name=spk.FriendlyName or "<unknown>")
    except Exception as e:
        log.debug("get_default_device failed: %s", e)
        return None


def _enumerate_render_ids() -> list[str]:
    """IDs of active render endpoints, in Windows' enumeration order."""
    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator,
                                     IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
    coll = enum.EnumAudioEndpoints(ERENDER, DEVICE_STATE_ACTIVE)
    return [coll.Item(i).GetId() for i in range(coll.GetCount())]


def list_outputs(apply_filter: bool = False) -> list[OutputDevice]:
    try:
        render_ids = _enumerate_render_ids()
    except Exception as e:
        log.error("enumerate render devices failed: %s", e)
        return []
    by_id = {d.id: d for d in AudioUtilities.GetAllDevices()
             if d.state == AudioDeviceState.Active}
    patterns = load_exclude_patterns() if apply_filter else []
    out: list[OutputDevice] = []
    for dev_id in render_ids:
        d = by_id.get(dev_id)
        if d is None:
            continue
        name = d.FriendlyName or "<unknown>"
        if patterns and any(p.search(name) for p in patterns):
            continue
        out.append(OutputDevice(id=dev_id, name=name))
    return out


def _set_default(device_id: str) -> bool:
    try:
        pc = comtypes.CoCreateInstance(CLSID_PolicyConfigClient,
                                       IPolicyConfig, comtypes.CLSCTX_ALL)
        # Match Windows' own "Set as default" which writes all three roles.
        for role in (0, 1, 2):  # eConsole, eMultimedia, eCommunications
            pc.SetDefaultEndpoint(device_id, role)
        return True
    except Exception as e:
        log.error("SetDefaultEndpoint failed: %s", e)
        return False


def cycle_output() -> OutputDevice | None:
    """Advance to the next non-filtered active render device (wraps)."""
    outputs = list_outputs(apply_filter=True)
    if not outputs:
        log.error("no outputs available after filter")
        return None
    cur = get_default_device()
    target = outputs[0]
    if cur is not None:
        for i, d in enumerate(outputs):
            if d.id == cur.id:
                target = outputs[(i + 1) % len(outputs)]
                break
    if _set_default(target.id):
        log.info("switched default output: %s -> %s",
                 cur.name if cur else "?", target.name)
        return target
    return None


# -------- snapshot for bar ---------------------------------------------

def get_status() -> dict:
    # One GetSpeakers round-trip for volume, mute, and device identity — the
    # bar polls this every tick, so the previous 3x re-fetch was wasteful.
    spk, ev = _endpoint_volume()
    name: str | None = None
    dev_id: str | None = None
    if spk is not None:
        try:
            name = spk.FriendlyName or "<unknown>"
            dev_id = spk.id
        except Exception:
            pass
    return {
        "volume":    _read_volume(ev),
        "mute":      _read_mute(ev),
        "device":    name,
        "device_id": dev_id,
    }
