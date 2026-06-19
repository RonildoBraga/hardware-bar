"""Windows Night Light backend — flips the CloudStore registry blob.

Windows doesn't expose a CLI for Night Light, so this manipulates the
CloudStore blob the settings service reads:

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\Store\\
        DefaultAccount\\Current\\default$windows.data.bluelightreduction.
        bluelightreductionstate\\windows.data.bluelightreduction.
        bluelightreductionstate   (value: Data, REG_BINARY)

The byte-level toggle algorithm lives in `_blob.py` (kept winreg-free so it
unit-tests on any platform); this module is the registry I/O around it.
"""

from __future__ import annotations

import logging
import winreg

from ._blob import _is_enabled_from_blob, _set_enabled_in_blob

log = logging.getLogger("nightlight")

REG_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"
    r"\DefaultAccount\Current"
    r"\default$windows.data.bluelightreduction.bluelightreductionstate"
    r"\windows.data.bluelightreduction.bluelightreductionstate"
)
REG_VALUE = "Data"


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
