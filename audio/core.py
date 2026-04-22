"""audio/core.py — master volume, mute, default output device cycling.

Uses pycaw (IAudioEndpointVolume) for volume/mute and the undocumented
IPolicyConfig COM interface for switching the default render endpoint.

Device cycling honours an optional filter file at the project root
(`audio_filter.local.json`, gitignored) whose `exclude_patterns` is a list
of regex strings matched case-insensitively against the FriendlyName;
matching devices are skipped by `--cycle` but still appear in `--list`.

Usage:
    audio --status
    audio --list
    audio --vol +5   | audio --vol -5
    audio --mute
    audio --cycle
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
from ctypes import HRESULT, c_int
from ctypes.wintypes import LPCWSTR
from pathlib import Path
from typing import NamedTuple

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown
from pycaw.pycaw import (
    AudioDeviceState,
    AudioUtilities,
    IMMDeviceEnumerator,
)

LOG_PATH = Path(tempfile.gettempdir()) / "hardware-bar-audio.log"
log = logging.getLogger("audio")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILTER_FILE = PROJECT_ROOT / "audio_filter.local.json"

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


class OutputDevice(NamedTuple):
    id:   str
    name: str


# -------- logging (only initialised in CLI main) -----------------------

def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(process)5d] %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass  # pythonw has no stderr


# -------- filter file --------------------------------------------------

def _load_exclude_patterns() -> list[re.Pattern[str]]:
    try:
        data = json.loads(FILTER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    patterns: list[re.Pattern[str]] = []
    for p in data.get("exclude_patterns") or []:
        try:
            patterns.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("bad regex %r in %s: %s", p, FILTER_FILE.name, e)
    return patterns


# -------- volume / mute ------------------------------------------------

def _endpoint_volume():
    try:
        spk = AudioUtilities.GetSpeakers()
        return spk, spk.EndpointVolume if spk else None
    except Exception as e:
        log.debug("GetSpeakers failed: %s", e)
        return None, None


def get_volume_pct() -> int | None:
    _, ev = _endpoint_volume()
    if ev is None:
        return None
    try:
        return int(round(ev.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return None


def get_mute() -> bool | None:
    _, ev = _endpoint_volume()
    if ev is None:
        return None
    try:
        return bool(ev.GetMute())
    except Exception:
        return None


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
    patterns = _load_exclude_patterns() if apply_filter else []
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
    cur = get_default_device()
    return {
        "volume":    get_volume_pct(),
        "mute":      get_mute(),
        "device":    cur.name if cur else None,
        "device_id": cur.id   if cur else None,
    }


# -------- CLI ----------------------------------------------------------

def main() -> int:
    _setup_logging()
    args = sys.argv[1:]
    log.info("=" * 50)
    log.info("launch argv=%s log=%s", args, LOG_PATH)

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = args[0]
    if cmd == "--status":
        s = get_status()
        mute_mark = " MUTE" if s["mute"] else ""
        print(f"vol={s['volume']}%{mute_mark} device={s['device']}")
        return 0

    if cmd == "--list":
        all_outputs = list_outputs(apply_filter=False)
        included = {d.id for d in list_outputs(apply_filter=True)}
        cur = get_default_device()
        for d in all_outputs:
            here    = "*" if cur and cur.id == d.id else " "
            skipped = "  (filtered)" if d.id not in included else ""
            print(f" {here} {d.name}{skipped}")
        return 0

    if cmd == "--vol":
        if len(args) != 2:
            print("usage: --vol <delta>  (e.g. +5 or -5)")
            return 2
        try:
            delta = int(args[1])
        except ValueError:
            print("delta must be an integer")
            return 2
        result = set_volume_delta(delta)
        print(f"{result}%" if result is not None else "err")
        return 0 if result is not None else 1

    if cmd == "--mute":
        result = toggle_mute()
        if result is None:
            print("err")
            return 1
        print("muted" if result else "unmuted")
        return 0

    if cmd == "--cycle":
        dev = cycle_output()
        if dev is None:
            print("err")
            return 1
        print(dev.name)
        return 0

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
