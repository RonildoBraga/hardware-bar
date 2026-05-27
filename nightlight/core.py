"""nightlight.py — toggle Windows 11 Night Light.

Windows doesn't expose a CLI for Night Light, so this manipulates the
CloudStore blob the settings service reads:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\Store\\
        DefaultAccount\\Current\\default$windows.data.bluelightreduction.
        bluelightreductionstate\\windows.data.bluelightreduction.
        bluelightreductionstate   (value: Data, REG_BINARY)

Blob layout (reverse-engineered empirically on this box):

    43 42 01 00                 outer sentinel 'CB\\x01\\x00'
    0a 02 01 00                 header
    2a 06 .. ..                 tagged timestamp (6-byte varint-ish payload)
    LL                          length of the inner section (1 byte)
    43 42 01 00                 inner sentinel
    [10 00]?                    present iff Night Light is ON
    ...                         trailing state bytes

Toggling = inserting or removing the 2-byte marker after the INNER sentinel,
adjusting the length byte LL by ±2, AND incrementing the first non-0xFF byte
in the 5-byte varint timestamp range [10, 15). Without that increment the
running display-broker ignores the registry change and the screen tint stays
the same even though Settings shows the toggle flipped.

Usage:
    nightlight.py --toggle
    nightlight.py --on
    nightlight.py --off
    nightlight.py --status
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import setup_logging

log = logging.getLogger("nightlight")

REG_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate"
)
REG_VALUE = "Data"

SENTINEL      = bytes([0x43, 0x42, 0x01, 0x00])  # 'CB\x01\x00' — appears twice
ENABLE_MARKER = bytes([0x10, 0x00])              # after INNER sentinel => ON
TIMESTAMP_RANGE = range(10, 15)                  # varint bytes to bump


# -------- registry blob manipulation ------------------------------------

def _read_blob() -> bytes:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY) as k:
        data, kind = winreg.QueryValueEx(k, REG_VALUE)
    if kind != winreg.REG_BINARY or not isinstance(data, (bytes, bytearray)):
        raise RuntimeError(f"unexpected registry type {kind}")
    return bytes(data)


def _write_blob(blob: bytes) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_BINARY, blob)


def _inner_sentinel_offsets(blob: bytes) -> tuple[int, int]:
    """Locate the inner state section.

    Returns (length_idx, after_inner_sentinel). Raises if the blob doesn't
    match the expected shape.
    """
    first = blob.find(SENTINEL)
    if first < 0:
        raise RuntimeError("outer sentinel not found")
    second = blob.find(SENTINEL, first + len(SENTINEL))
    if second < 0:
        raise RuntimeError("inner sentinel not found")
    length_idx = second - 1  # length byte sits immediately before inner sentinel
    if length_idx < 0:
        raise RuntimeError("no length byte before inner sentinel")
    return length_idx, second + len(SENTINEL)


def _is_enabled_from_blob(blob: bytes) -> bool:
    try:
        _, pos = _inner_sentinel_offsets(blob)
    except RuntimeError:
        return False
    return blob[pos:pos + len(ENABLE_MARKER)] == ENABLE_MARKER


def _bump_timestamp(blob: bytearray) -> None:
    """Nudge the varint timestamp forward so the display broker re-reads state."""
    for i in TIMESTAMP_RANGE:
        if i < len(blob) and blob[i] != 0xFF:
            blob[i] = (blob[i] + 1) & 0xFF
            return


def _set_enabled_in_blob(blob: bytes, enabled: bool) -> bytes:
    length_idx, pos = _inner_sentinel_offsets(blob)
    currently_on = blob[pos:pos + len(ENABLE_MARKER)] == ENABLE_MARKER
    length = blob[length_idx]
    if enabled and not currently_on:
        new = bytearray(blob[:length_idx] + bytes([length + len(ENABLE_MARKER)])
                        + blob[length_idx + 1:pos] + ENABLE_MARKER + blob[pos:])
    elif not enabled and currently_on:
        new = bytearray(blob[:length_idx] + bytes([length - len(ENABLE_MARKER)])
                        + blob[length_idx + 1:pos]
                        + blob[pos + len(ENABLE_MARKER):])
    else:
        new = bytearray(blob)  # already in desired state; just bump timestamp
    _bump_timestamp(new)
    return bytes(new)


# -------- Public API (also imported by bar.py) --------------------------

def is_enabled() -> bool | None:
    """Current Night Light state, or None if the registry key is unreadable."""
    try:
        return _is_enabled_from_blob(_read_blob())
    except (OSError, RuntimeError):
        return None


def set_state(enabled: bool) -> bool:
    """Set Night Light on/off. Returns True on successful registry write."""
    try:
        blob = _read_blob()
    except OSError as e:
        log.error("read failed: %s", e)
        return False
    was_on = _is_enabled_from_blob(blob)
    try:
        _write_blob(_set_enabled_in_blob(blob, enabled))
    except (OSError, RuntimeError) as e:
        log.error("write failed: %s", e)
        return False
    if was_on == enabled:
        log.info("already %s; wrote timestamp bump to resync live state",
                 "on" if was_on else "off")
    else:
        log.info("Night Light %s -> %s", "on" if was_on else "off",
                 "on" if enabled else "off")
    return True


def toggle() -> bool | None:
    """Flip state. Returns the NEW state, or None on failure."""
    cur = is_enabled()
    if cur is None:
        log.error("could not read current state")
        return None
    new = not cur
    if not set_state(new):
        return None
    return new


# -------- CLI -----------------------------------------------------------

def main() -> int:
    _, log_path = setup_logging("nightlight", "hardware-bar-nightlight.log")
    args = sys.argv[1:]
    log.info("launch argv=%s log=%s", args, log_path)

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    cmd = args[0]
    if cmd == "--status":
        st = is_enabled()
        print("on" if st else ("off" if st is False else "unknown"))
        return 0 if st is not None else 1
    if cmd == "--toggle":
        new = toggle()
        if new is None:
            return 1
        print("on" if new else "off")
        return 0
    if cmd == "--on":
        return 0 if set_state(True) else 1
    if cmd == "--off":
        return 0 if set_state(False) else 1

    print("Usage: nightlight.py --toggle | --on | --off | --status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
