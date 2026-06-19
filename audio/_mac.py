"""macOS audio backend — master volume, mute, default output cycling.

Volume and mute go through `osascript` (AppleScript `get/set volume`), which is
always present, so the bar's VOL field works out of the box. Output-device
switching uses `SwitchAudioSource` (brew install switchaudio-osx); if it's not
installed, device listing/cycling degrade to empty and the bar hides the OUT
field — same graceful path as Windows when COM enumeration fails.

Unlike Windows endpoints, macOS output devices have no stable GUID exposed to
the shell, so we key on the friendly name (id == name). Cycling by name is
fine for the Loupedeck use case.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

from ._filter import load_exclude_patterns
from ._types import OutputDevice

log = logging.getLogger("audio")

_SWITCH_CANDIDATES = (
    "SwitchAudioSource",
    "/opt/homebrew/bin/SwitchAudioSource",
    "/usr/local/bin/SwitchAudioSource",
)

_VOL_RE  = re.compile(r"output volume:(\d+)")
_MUTE_RE = re.compile(r"output muted:(true|false)")


# -------- subprocess helpers -------------------------------------------

def _osascript(script: str) -> str | None:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("osascript failed: %s", e)
        return None
    if r.returncode != 0:
        log.debug("osascript rc=%d stderr=%s", r.returncode, r.stderr.strip())
        return None
    return r.stdout.strip()


def _switch_bin() -> str | None:
    for cand in _SWITCH_CANDIDATES:
        path = shutil.which(cand)
        if path:
            return path
    return None


def _switch(*args: str) -> str | None:
    exe = _switch_bin()
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("SwitchAudioSource %s failed: %s", args, e)
        return None
    if r.returncode != 0:
        log.debug("SwitchAudioSource %s rc=%d stderr=%s", args, r.returncode, r.stderr.strip())
        return None
    return r.stdout.strip()


# -------- volume / mute ------------------------------------------------

def _settings() -> tuple[int | None, bool | None]:
    """Return (volume_pct, muted) from a single `get volume settings` call."""
    out = _osascript("get volume settings")
    if out is None:
        return None, None
    vol = mute = None
    m = _VOL_RE.search(out)
    if m:
        vol = int(m.group(1))
    m = _MUTE_RE.search(out)
    if m:
        mute = m.group(1) == "true"
    return vol, mute


def get_volume_pct() -> int | None:
    return _settings()[0]


def get_mute() -> bool | None:
    return _settings()[1]


def set_volume_delta(delta_pct: int) -> int | None:
    cur = get_volume_pct()
    if cur is None:
        return None
    new = max(0, min(100, cur + delta_pct))
    if _osascript(f"set volume output volume {new}") is None:
        log.error("set volume failed")
        return None
    return new


def set_mute(muted: bool) -> bool | None:
    flag = "true" if muted else "false"
    if _osascript(f"set volume output muted {flag}") is None:
        log.error("set mute failed")
        return None
    return muted


def toggle_mute() -> bool | None:
    cur = get_mute()
    if cur is None:
        return None
    return set_mute(not cur)


# -------- device enumeration + switching -------------------------------

def get_default_device() -> OutputDevice | None:
    name = _switch("-c", "-t", "output")
    if not name:
        return None
    return OutputDevice(id=name, name=name)


def list_outputs(apply_filter: bool = False) -> list[OutputDevice]:
    out = _switch("-a", "-t", "output")
    if out is None:
        return []
    patterns = load_exclude_patterns() if apply_filter else []
    devices: list[OutputDevice] = []
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        if patterns and any(p.search(name) for p in patterns):
            continue
        devices.append(OutputDevice(id=name, name=name))
    return devices


def cycle_output() -> OutputDevice | None:
    """Advance to the next non-filtered active output device (wraps)."""
    outputs = list_outputs(apply_filter=True)
    if not outputs:
        log.error("no outputs available (is switchaudio-osx installed?)")
        return None
    cur = get_default_device()
    target = outputs[0]
    if cur is not None:
        for i, d in enumerate(outputs):
            if d.id == cur.id:
                target = outputs[(i + 1) % len(outputs)]
                break
    if _switch("-s", target.name, "-t", "output") is not None:
        log.info("switched default output: %s -> %s",
                 cur.name if cur else "?", target.name)
        return target
    log.error("SwitchAudioSource set failed for %s", target.name)
    return None


# -------- snapshot for bar ---------------------------------------------

def get_status() -> dict:
    vol, mute = _settings()
    dev = get_default_device()
    return {
        "volume":    vol,
        "mute":      mute,
        "device":    dev.name if dev else None,
        "device_id": dev.id if dev else None,
    }
