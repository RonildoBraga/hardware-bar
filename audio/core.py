"""audio — master volume, mute, default output device cycling.

Windows: pycaw (IAudioEndpointVolume) + IPolicyConfig COM (`_win.py`).
macOS:   osascript volume/mute + SwitchAudioSource switching (`_mac.py`).

This module is a thin platform dispatcher; the public API is re-exported by
audio/__init__.py and polled by the bar. Device cycling honours an optional
filter file at the project root (`audio_filter.local.json`) — see `_filter.py`.

Usage:
    audio --status
    audio --list
    audio --vol +5   | audio --vol -5
    audio --mute
    audio --cycle
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import IS_MACOS, IS_WINDOWS, setup_logging
from ._types import OutputDevice  # noqa: F401  (re-exported for callers)

log = logging.getLogger("audio")

if IS_WINDOWS:
    from . import _win as _backend
elif IS_MACOS:
    from . import _mac as _backend
else:
    _backend = None


def _require_backend():
    if _backend is None:
        raise RuntimeError(f"audio: unsupported platform {sys.platform}")
    return _backend


# -------- public API (dispatched to the platform backend) ---------------

def get_volume_pct() -> int | None:
    return _backend.get_volume_pct() if _backend else None


def get_mute() -> bool | None:
    return _backend.get_mute() if _backend else None


def set_volume_delta(delta_pct: int) -> int | None:
    return _backend.set_volume_delta(delta_pct) if _backend else None


def set_mute(muted: bool) -> bool | None:
    return _backend.set_mute(muted) if _backend else None


def toggle_mute() -> bool | None:
    return _backend.toggle_mute() if _backend else None


def get_default_device() -> OutputDevice | None:
    return _backend.get_default_device() if _backend else None


def list_outputs(apply_filter: bool = False) -> list[OutputDevice]:
    return _backend.list_outputs(apply_filter) if _backend else []


def cycle_output() -> OutputDevice | None:
    return _backend.cycle_output() if _backend else None


def get_status() -> dict:
    if _backend is None:
        return {"volume": None, "mute": None, "device": None, "device_id": None}
    return _backend.get_status()


# -------- CLI ----------------------------------------------------------

def main() -> int:
    _, log_path = setup_logging("audio", "hardware-bar-audio.log")
    args = sys.argv[1:]
    log.info("launch argv=%s log=%s", args, log_path)

    if _backend is None:
        print(f"audio: unsupported platform {sys.platform}")
        return 2

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
