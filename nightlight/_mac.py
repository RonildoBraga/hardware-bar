"""macOS Night Shift backend — the Darwin analogue of Windows Night Light.

macOS exposes Night Shift only through the private CoreBrightness framework
(`CBBlueLightClient`), with no supported CLI. Rather than poke a private
framework, this backend shells out to the `nightlight` Homebrew tool
(https://github.com/smudge/nightlight), mirroring how the Windows side defers
to OS plumbing and how the bar already shells out to LHM/schtasks.

    brew install smudge/smudge/nightlight

If the tool isn't installed, every call returns the "unknown" sentinel
(`None` / `False`) and the bar silently hides the NL field — same graceful
degradation as a missing registry key on Windows.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger("nightlight")

# brew installs to /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel).
_SEARCH = ("nightlight", "/opt/homebrew/bin/nightlight", "/usr/local/bin/nightlight")


def _bin() -> str | None:
    for cand in _SEARCH:
        path = shutil.which(cand) or (cand if cand.startswith("/") and shutil.os.path.exists(cand) else None)
        if path:
            return path
    return None


def _run(*args: str) -> str | None:
    exe = _bin()
    if exe is None:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("nightlight %s failed: %s", args, e)
        return None
    if r.returncode != 0:
        log.debug("nightlight %s rc=%d stderr=%s", args, r.returncode, r.stderr.strip())
        return None
    return r.stdout.strip()


def is_enabled() -> bool | None:
    """Current Night Shift state, or None if the `nightlight` tool is absent."""
    out = _run("status")
    if out is None:
        return None
    low = out.lower()
    # `nightlight status` prints e.g. "Night Shift: on" / "on" / "off".
    if "on" in low and "off" not in low:
        return True
    if "off" in low:
        return False
    return None


def set_state(enabled: bool) -> bool:
    """Turn Night Shift on/off. Returns True on success."""
    out = _run("on" if enabled else "off")
    if out is None:
        log.error("nightlight tool unavailable or failed")
        return False
    log.info("Night Shift -> %s", "on" if enabled else "off")
    return True


def toggle() -> bool | None:
    """Flip state. Returns the NEW state, or None on failure."""
    cur = is_enabled()
    if cur is None:
        log.error("could not read current state (is `nightlight` installed?)")
        return None
    new = not cur
    if not set_state(new):
        return None
    return new
