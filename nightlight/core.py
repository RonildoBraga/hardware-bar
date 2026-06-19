"""nightlight — toggle the OS blue-light reduction feature.

Windows: Night Light (registry-blob backend, `_win.py`).
macOS:   Night Shift (`nightlight` CLI backend, `_mac.py`).

This module is a thin platform dispatcher. The public API
(`is_enabled`/`set_state`/`toggle`) is re-exported by `nightlight/__init__.py`
and used by the bar. The pure blob helpers live in `_blob.py` and are
re-exported here so the unit tests can keep importing them from
`nightlight.core` on any platform.

Usage:
    python -m nightlight --toggle
    python -m nightlight --on
    python -m nightlight --off
    python -m nightlight --status
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import IS_MACOS, IS_WINDOWS, setup_logging

# Re-exported for the unit tests (tests/test_nightlight.py imports these from
# nightlight.core) and for the Windows backend's own use.
from ._blob import (  # noqa: F401
    ENABLE_MARKER,
    SENTINEL,
    TIMESTAMP_RANGE,
    _bump_timestamp,
    _inner_sentinel_offsets,
    _is_enabled_from_blob,
    _set_enabled_in_blob,
)

log = logging.getLogger("nightlight")

if IS_WINDOWS:
    from . import _win as _backend
elif IS_MACOS:
    from . import _mac as _backend
else:
    _backend = None


# -------- Public API (also imported by bar's Poller) --------------------

def is_enabled() -> bool | None:
    """Current state, or None if unreadable / unsupported on this platform."""
    return _backend.is_enabled() if _backend is not None else None


def set_state(enabled: bool) -> bool:
    """Set on/off. Returns True on success."""
    return _backend.set_state(enabled) if _backend is not None else False


def toggle() -> bool | None:
    """Flip state. Returns the NEW state, or None on failure."""
    return _backend.toggle() if _backend is not None else None


# -------- CLI -----------------------------------------------------------

def main() -> int:
    _, log_path = setup_logging("nightlight", "hardware-bar-nightlight.log")
    args = sys.argv[1:]
    log.info("launch argv=%s log=%s", args, log_path)

    if _backend is None:
        print(f"nightlight: unsupported platform {sys.platform}")
        return 2

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

    print("Usage: nightlight --toggle | --on | --off | --status")
    return 2


if __name__ == "__main__":
    sys.exit(main())
