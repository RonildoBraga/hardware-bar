"""brightness — adjust per-monitor brightness (Windows + macOS).

Platform dispatcher. The bar, the daemon (`from . import core as br`), and the
offline CLI all talk to this module; it forwards to the active backend:

    Windows: DDC/CI + HDR SDRWhiteLevel via DisplayConfig (`_win.py`)
    macOS:   `brightness` CLI (built-in) + `m1ddc` (external)  (`_mac.py`)

Both backends expose the same names — DisplayTarget, build_display_index,
is_hdr_enabled, get/set_sdr_white_level, compute_hdr_adjust, list_displays,
adjust — and return monitor objects with a `with m: m.get_luminance()/
set_luminance()` protocol, so brightness/daemon.py is platform-agnostic.

The percent<->SDR-white-level math (`_scale.py`) is re-exported here because
tests/test_brightness.py imports it from brightness.core.

Usage:
    brightness --list                  # show all displays and current state
    brightness <index> <delta>         # change brightness by signed delta
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import IS_MACOS, IS_WINDOWS, setup_logging

# Re-exported for tests/test_brightness.py and for the daemon's HDR math.
from ._scale import (  # noqa: F401
    SDR_WL_MAX,
    SDR_WL_MIN,
    HdrAdjust,
    sdr_pct_to_wl,
    sdr_wl_to_pct,
)

log = logging.getLogger("brightness")

if IS_WINDOWS:
    from ._win import (  # noqa: F401
        DisplayTarget,
        adjust,
        build_display_index,
        compute_hdr_adjust,
        get_sdr_white_level,
        is_hdr_enabled,
        list_displays,
        set_sdr_white_level,
    )
elif IS_MACOS:
    from ._mac import (  # noqa: F401
        DisplayTarget,
        adjust,
        build_display_index,
        compute_hdr_adjust,
        get_sdr_white_level,
        is_hdr_enabled,
        list_displays,
        set_sdr_white_level,
    )
else:
    DisplayTarget = None  # type: ignore

    def build_display_index():  # type: ignore
        return []

    def list_displays() -> int:  # type: ignore
        print(f"brightness: unsupported platform {sys.platform}")
        return 2

    def adjust(index: int, delta: int) -> int:  # type: ignore
        print(f"brightness: unsupported platform {sys.platform}")
        return 2


def main() -> int:
    _, log_path = setup_logging("brightness", "hardware-bar-brightness.log")
    args = sys.argv[1:]
    log.info("launch argv=%s log=%s", args, log_path)

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        return list_displays()
    if len(args) != 2:
        print("Usage: brightness <index> <delta>  |  brightness --list")
        return 2
    try:
        return adjust(int(args[0]), int(args[1]))
    except ValueError:
        log.error("both index and delta must be integers")
        return 2


if __name__ == "__main__":
    sys.exit(main())
