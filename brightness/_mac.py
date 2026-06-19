"""macOS brightness backend.

macOS has no DisplayConfig/SDRWhiteLevel API, so HDR is irrelevant here:
`is_hdr_enabled` is always False and every display takes the "luminance" path.
Two transports:

  - Built-in panel: the private DisplayServices framework via ctypes
    (`_displayservices.py`). The Homebrew `brightness` CLI can't read the
    built-in panel on Apple Silicon, so we call DisplayServices directly.
  - External DDC displays: `m1ddc` (brew install m1ddc), 0-100 luminance.

The per-display object (`MacMonitor`) mimics the `monitorcontrol.Monitor` API
the daemon uses — a context manager exposing `get_luminance()` /
`set_luminance(int)` — so brightness/daemon.py works unchanged across
platforms. If a transport is missing, that display is dropped from enumeration
and the bar hides its BRI entry.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import NamedTuple, Optional

from . import _displayservices as ds
from ._scale import HdrAdjust

log = logging.getLogger("brightness")

_M1DDC = ("m1ddc", "/opt/homebrew/bin/m1ddc", "/usr/local/bin/m1ddc")

# m1ddc `display list` lines look like:  [1] Display Name (UUID)
# or, when a display is asleep/unnamed:  [1] (null) (UUID)
_LIST_RE = re.compile(r"\[(\d+)\]\s*(.*?)\s*\(([0-9A-Fa-f-]+)\)\s*$")


def _which(cands: tuple[str, ...]) -> str | None:
    for c in cands:
        path = shutil.which(c)
        if path:
            return path
    return None


def _run(exe: str | None, *args: str, timeout: float = 3.0) -> str | None:
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("%s %s failed: %s", exe, args, e)
        return None
    if r.returncode != 0:
        log.debug("%s %s rc=%d stderr=%s", exe, args, r.returncode, r.stderr.strip())
        return None
    return r.stdout.strip()


# -------- display target + monitor object -------------------------------


class DisplayTarget(NamedTuple):
    name:       str
    kind:       str          # 'builtin' | 'ddc'
    handle:     int | None   # CGDirectDisplayID (builtin) or m1ddc ordinal (ddc)


class MacMonitor:
    """Quacks like monitorcontrol.Monitor for the daemon's `with m:` usage."""

    def __init__(self, target: DisplayTarget) -> None:
        self._t = target

    def __enter__(self) -> "MacMonitor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get_luminance(self) -> int:
        if self._t.kind == "builtin":
            pct = ds.get_brightness_pct(self._t.handle)
            if pct is None:
                raise RuntimeError("DisplayServices get failed")
            return pct
        out = _run(_which(_M1DDC), "display", str(self._t.handle), "get", "luminance")
        if out is None:
            raise RuntimeError("m1ddc get luminance failed")
        try:
            return int(out.strip().split()[-1])
        except (ValueError, IndexError):
            raise RuntimeError(f"unparseable m1ddc output: {out!r}")

    def set_luminance(self, value: int) -> None:
        value = max(0, min(100, value))
        if self._t.kind == "builtin":
            if not ds.set_brightness_pct(self._t.handle, value):
                raise RuntimeError("DisplayServices set failed")
            return
        if _run(_which(_M1DDC), "display", str(self._t.handle),
                "set", "luminance", str(value)) is None:
            raise RuntimeError("m1ddc set luminance failed")


# -------- enumeration ---------------------------------------------------


def _ddc_displays() -> list[DisplayTarget]:
    out = _run(_which(_M1DDC), "display", "list")
    if not out:
        return []
    # m1ddc keeps listing a ghost entry (by cached UUID) for a just-unplugged
    # display — sometimes *ahead* of a real one. CoreGraphics is authoritative,
    # so keep only m1ddc entries whose UUID is actually online. Fall back to a
    # plain count cap if the UUID set is unavailable.
    online = ds.online_external_uuids()
    cap = ds.online_external_count() if online is None else None

    targets: list[DisplayTarget] = []
    for line in out.splitlines():
        m = _LIST_RE.search(line.strip())
        if not m:
            continue
        idx, name, uuid = int(m.group(1)), m.group(2).strip(), m.group(3).upper()
        if online is not None and uuid not in online:
            log.debug("dropping ghost m1ddc display [%d] %s (uuid %s not online)",
                      idx, name or "(null)", uuid)
            continue
        if not name or name.lower() == "(null)":
            name = f"Display {idx}"
        targets.append(DisplayTarget(name=name, kind="ddc", handle=idx))

    if cap is not None and len(targets) > cap:
        targets = targets[:cap]
    return targets


def enumerate_display_targets() -> list[DisplayTarget]:
    targets: list[DisplayTarget] = []
    builtin_id = ds.builtin_display_id()
    if builtin_id is not None:
        targets.append(DisplayTarget(name="Built-in", kind="builtin", handle=builtin_id))
    targets.extend(_ddc_displays())
    return targets


def build_display_index() -> list[tuple[DisplayTarget, Optional[MacMonitor]]]:
    """Built-in display first (if present), then external DDC displays."""
    return [(t, MacMonitor(t)) for t in enumerate_display_targets()]


# -------- HDR shims (unused on macOS, kept for API symmetry) ------------

def is_hdr_enabled(target: DisplayTarget) -> bool:
    return False


def get_sdr_white_level(target: DisplayTarget) -> int:
    return 1000


def set_sdr_white_level(target: DisplayTarget, level: int) -> bool:
    return False


def compute_hdr_adjust(target: DisplayTarget, delta: int) -> HdrAdjust:
    return HdrAdjust(1000, 1000, 0, 0)


# -------- public operations ---------------------------------------------


def list_displays() -> int:
    rows = build_display_index()
    if not rows:
        print("No displays found. The built-in panel uses DisplayServices "
              "(no install needed); external displays need `m1ddc`:")
        print("  brew install m1ddc")
        return 0
    print(f"Found {len(rows)} display(s):\n")
    for i, (t, m) in enumerate(rows):
        print(f"[{i}] {t.name}   ({t.kind})")
        try:
            with m:
                print(f"     brightness: {m.get_luminance()}")
        except Exception as e:
            print(f"     brightness: not available ({e})")
    return 0


def adjust(index: int, delta: int) -> int:
    rows = build_display_index()
    if index < 0 or index >= len(rows):
        log.error("invalid display index %d (valid: 0..%d)", index, len(rows) - 1)
        return 2
    target, m = rows[index]
    log.info("adjust: index=%d display=%s delta=%+d", index, target.name, delta)
    try:
        with m:
            cur = m.get_luminance()
            new = max(0, min(100, cur + delta))
            m.set_luminance(new)
            log.info("  brightness %d -> %d", cur, new)
            return 0
    except Exception as e:
        log.error("  brightness adjust error: %s", e)
        return 1
