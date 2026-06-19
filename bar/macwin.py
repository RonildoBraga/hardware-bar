"""macOS window/app integration for the bar (and charts).

Two behaviours that have no Qt-portable equivalent and need AppKit:

  * accessory_mode(): make the process a background "accessory" app
    (== LSUIElement) — no Dock icon, no menu bar, and crucially it never
    "deactivates" a plain window the way a regular app does, so the bar stays
    put when you click into other apps.

  * float_on_all_spaces(widget): pin the widget's NSWindow above normal windows
    (status level), show it on every Space, and let it sit over full-screen
    apps — the desktop-widget behaviour the Windows Tool+StaysOnTop combo gives
    for free.

Everything is guarded: if PyObjC isn't installed the calls no-op and the bar
still runs as an ordinary always-on-top window (with a Dock icon).
"""

from __future__ import annotations

import logging

log = logging.getLogger("bar")

# NSApplicationActivationPolicyAccessory
_ACCESSORY = 1
# NSWindowCollectionBehavior bits
_CAN_JOIN_ALL_SPACES   = 1 << 0
_STATIONARY            = 1 << 4
_FULLSCREEN_AUXILIARY  = 1 << 8
# NSStatusWindowLevel — above normal/floating windows, below the menu bar.
_STATUS_WINDOW_LEVEL = 25


def accessory_mode() -> bool:
    """Hide the Dock icon / menu bar. Returns True if applied."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(_ACCESSORY)
        return True
    except Exception as e:  # pragma: no cover - depends on PyObjC presence
        log.debug("accessory_mode unavailable: %s", e)
        return False


def float_on_all_spaces(widget) -> bool:
    """Raise the widget's NSWindow to status level on all Spaces. True if applied."""
    try:
        import objc  # noqa: F401
        view = objc.objc_object(c_void_p=int(widget.winId()))
        window = view.window()
        if window is None:
            return False
        window.setCollectionBehavior_(
            _CAN_JOIN_ALL_SPACES | _STATIONARY | _FULLSCREEN_AUXILIARY
        )
        window.setLevel_(_STATUS_WINDOW_LEVEL)
        return True
    except Exception as e:  # pragma: no cover - depends on PyObjC presence
        log.debug("float_on_all_spaces unavailable: %s", e)
        return False
