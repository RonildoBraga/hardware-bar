"""brightness_daemon.py — long-running brightness adjuster.

Listens on 127.0.0.1:48736 and accepts one command per connection:

    adjust <idx> <delta>   -> "ok ..." | "err ..."
    status                 -> compact per-display pct, e.g. "0:40 1:38 2:50"
    list                   -> multi-line display info, ends with a blank line
    refresh                -> re-enumerate monitors, "ok"
    ping                   -> "pong"
    quit                   -> shut down cleanly

Pairs with brightness_client.py (the thing Loupedeck calls). Keeps the
DisplayConfig and monitorcontrol state in memory so dial ticks don't pay
the ~500ms Python-startup + DDC-scan cost each time.
"""

from __future__ import annotations

import logging
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

import brightness as br  # reuse the existing helpers

HOST = "127.0.0.1"
PORT = 48736

LOG_PATH = Path(tempfile.gettempdir()) / "hardware-bar-brightness-daemon.log"
log = logging.getLogger("brightness-daemon")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    except Exception:
        pass


# Cached display index; refreshed on startup / on `refresh` / after errors.
_lock = threading.Lock()
_displays: list[tuple[br.DisplayTarget, Optional[object]]] = []
# DDC brightness cache, keyed by display index. Populated on refresh
# (slow — hits the I2C bus) and kept fresh by adjust() calls.
_ddc_cache: dict[int, Optional[int]] = {}


def refresh_displays() -> None:
    global _displays, _ddc_cache
    rows = br.build_display_index()
    cache: dict[int, Optional[int]] = {}
    for i, (t, m) in enumerate(rows):
        if m is None:
            cache[i] = None
            continue
        try:
            with m:
                cache[i] = m.get_luminance()
        except Exception:
            cache[i] = None
    with _lock:
        _displays = rows
        _ddc_cache = cache
    log.info("refreshed: %d display(s): %s",
             len(rows), ", ".join(t.name for t, _ in rows))


def _snapshot() -> list[tuple[br.DisplayTarget, Optional[object]]]:
    with _lock:
        return list(_displays)


def handle_adjust(index: int, delta: int) -> str:
    rows = _snapshot()
    if index < 0 or index >= len(rows):
        return f"err invalid index {index} (have {len(rows)} displays)"
    target, m = rows[index]
    hdr_on = br.is_hdr_enabled(target)

    if hdr_on:
        cur_wl = br.get_sdr_white_level(target)
        cur_pct = br.sdr_wl_to_pct(cur_wl)
        new_pct = max(0, min(100, cur_pct + delta))
        new_wl = br.sdr_pct_to_wl(new_pct)
        if new_wl == cur_wl:
            log.info("adjust[%d] %s hdr +%d noop (at %s)", index, target.name,
                     delta, "max" if new_pct >= 100 else ("min" if new_pct <= 0 else f"{cur_pct}%"))
            return f"ok noop hdr pct={cur_pct}"
        ok = br.set_sdr_white_level(target, new_wl)
        log.info("adjust[%d] %s hdr wl %d->%d (%d%%->%d%%) %s",
                 index, target.name, cur_wl, new_wl, cur_pct, new_pct,
                 "ok" if ok else "FAILED")
        return (f"ok hdr wl={cur_wl}->{new_wl} pct={cur_pct}->{new_pct}"
                if ok else f"err set_sdr_white_level failed ({cur_wl}->{new_wl})")

    # SDR / DDC path
    if m is None:
        return "err no DDC/CI device for this display"
    try:
        with m:
            cur = m.get_luminance()
            new = max(0, min(100, cur + delta))
            if new == cur:
                log.info("adjust[%d] %s ddc +%d noop (at %d)", index, target.name, delta, cur)
                with _lock:
                    _ddc_cache[index] = cur
                return f"ok noop ddc={cur}"
            m.set_luminance(new)
            log.info("adjust[%d] %s ddc %d->%d", index, target.name, cur, new)
        with _lock:
            _ddc_cache[index] = new
        return f"ok ddc={cur}->{new}"
    except Exception as e:
        log.error("adjust[%d] %s ddc error: %s", index, target.name, e)
        # Something about the monitor state changed — refresh on next call.
        threading.Thread(target=refresh_displays, daemon=True).start()
        return f"err ddc {e}"


def handle_status() -> str:
    """Fast per-display percent summary. HDR uses a live API call (~1ms),
    SDR uses the cached DDC value so no I2C traffic happens here."""
    rows = _snapshot()
    with _lock:
        cache = dict(_ddc_cache)
    parts: list[str] = []
    for i, (t, _) in enumerate(rows):
        if br.is_hdr_enabled(t):
            pct = br.sdr_wl_to_pct(br.get_sdr_white_level(t))
            parts.append(f"{i}:{pct}")
        else:
            v = cache.get(i)
            parts.append(f"{i}:{v if v is not None else '-'}")
    return " ".join(parts) if parts else "-"


def handle_list() -> str:
    rows = _snapshot()
    out = [f"{len(rows)} displays"]
    for i, (t, m) in enumerate(rows):
        hdr = br.is_hdr_enabled(t)
        parts = [f"[{i}]", t.name, f"hdr={'on' if hdr else 'off'}"]
        if m is not None:
            try:
                with m:
                    parts.append(f"ddc={m.get_luminance()}")
            except Exception as e:
                parts.append(f"ddc=err({e})")
        else:
            parts.append("ddc=none")
        if hdr:
            wl = br.get_sdr_white_level(t)
            parts.append(f"wl={wl} pct={br.sdr_wl_to_pct(wl)}")
        out.append(" ".join(parts))
    out.append("")  # blank line marks end of output
    return "\n".join(out)


def dispatch(line: str) -> tuple[str, bool]:
    """Returns (response, should_shutdown)."""
    parts = line.strip().split()
    if not parts:
        return "err empty", False
    cmd = parts[0].lower()
    try:
        if cmd == "ping":
            return "pong", False
        if cmd == "quit":
            return "ok bye", True
        if cmd == "refresh":
            refresh_displays()
            return "ok refreshed", False
        if cmd == "list":
            return handle_list(), False
        if cmd == "status":
            return handle_status(), False
        if cmd == "adjust":
            if len(parts) != 3:
                return "err usage: adjust <idx> <delta>", False
            return handle_adjust(int(parts[1]), int(parts[2])), False
        return f"err unknown command: {cmd}", False
    except ValueError:
        return "err integer parse failed", False
    except Exception as e:
        log.exception("dispatch error")
        return f"err {e}", False


def serve() -> int:
    _setup_logging()
    log.info("=" * 50)
    log.info("starting daemon on %s:%d  log=%s", HOST, PORT, LOG_PATH)

    try:
        refresh_displays()
    except Exception as e:
        log.exception("initial refresh failed")
        return 1

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as e:
        log.error("bind failed — another daemon may be running: %s", e)
        return 2
    srv.listen(8)

    shutting_down = False
    while not shutting_down:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            conn.settimeout(2.0)
            try:
                data = conn.recv(256).decode("utf-8", errors="replace")
            except Exception:
                continue
            resp, should_stop = dispatch(data)
            try:
                conn.sendall((resp + "\n").encode("utf-8"))
            except Exception:
                pass
            if should_stop:
                shutting_down = True

    srv.close()
    log.info("daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(serve())
