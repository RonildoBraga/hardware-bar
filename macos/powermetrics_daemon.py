#!/usr/bin/env python3
"""Privileged powermetrics sampler for the macOS hardware bar.

`powermetrics` needs root, and we don't want to run the GUI bar as root — so
this tiny stdlib-only daemon (the Darwin analogue of LibreHardwareMonitor's
remote server) runs as root, streams `powermetrics`, and writes the parsed
numbers to a world-readable JSON file that the user-level bar polls.

It deliberately supplies only what Apple gates behind root — GPU active
residency and CPU/GPU package power. Temperatures and fan RPM are read without
privileges by the bar itself (bar/iohid.py + bar/smc.py), so they're not here.

Run modes:
    sudo /usr/bin/python3 powermetrics_daemon.py            # stream forever
    sudo /usr/bin/python3 powermetrics_daemon.py --once     # one sample, print JSON
    sudo /usr/bin/python3 powermetrics_daemon.py --dump     # raw top-level plist keys

Stdlib only (subprocess, plistlib, json) so it runs under the system python as
root with no virtualenv. Installed as a LaunchDaemon by
scripts/install/install-powermetrics-daemon.sh.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import time

OUT_FILE = "/tmp/hardware-bar-powermetrics.json"
INTERVAL_MS = 1000  # match the bar's 1 Hz refresh

_running = True


def _stop(*_a) -> None:
    global _running
    _running = False


def _find(d, *keys):
    """First present key from a dict (case-insensitive), else None."""
    if not isinstance(d, dict):
        return None
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None:
            return v
    return None


def parse_sample(pl: dict) -> dict:
    """Extract {gpu_pct, gpu_power_w, cpu_power_w} from one powermetrics plist.

    Key locations vary across macOS releases, so we look in both the top level
    and the `processor`/`gpu` sub-dicts and accept several spellings. Power
    fields are milliwatts; we convert to watts. Every field is optional.
    """
    proc = pl.get("processor", {}) if isinstance(pl.get("processor"), dict) else {}
    gpu = pl.get("gpu", {}) if isinstance(pl.get("gpu"), dict) else {}

    out: dict[str, float] = {}

    cpu_mw = _find(proc, "cpu_power") or _find(pl, "cpu_power")
    if cpu_mw is not None:
        out["cpu_power_w"] = round(float(cpu_mw) / 1000.0, 2)

    gpu_mw = _find(gpu, "gpu_power") or _find(proc, "gpu_power") or _find(pl, "gpu_power")
    if gpu_mw is not None:
        out["gpu_power_w"] = round(float(gpu_mw) / 1000.0, 2)

    # GPU active residency: prefer an explicit active ratio, else 1 - idle_ratio.
    active = _find(gpu, "active_ratio")
    idle = _find(gpu, "idle_ratio")
    if active is not None:
        out["gpu_pct"] = round(float(active) * 100.0, 1)
    elif idle is not None:
        out["gpu_pct"] = round((1.0 - float(idle)) * 100.0, 1)

    return out


def _write(data: dict) -> None:
    data = dict(data)
    data["ts"] = time.time()
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, OUT_FILE)
    try:
        os.chmod(OUT_FILE, 0o644)  # readable by the user-level bar
    except OSError:
        pass


def _powermetrics_cmd(samples: int | None) -> list[str]:
    cmd = [
        "powermetrics",
        "--samplers", "cpu_power,gpu_power",
        "-i", str(INTERVAL_MS),
        "--format", "plist",
    ]
    if samples is not None:
        cmd += ["-n", str(samples)]
    return cmd


def _iter_plists(stdout):
    """Yield parsed plists from powermetrics' NUL-separated plist stream."""
    buf = b""
    for chunk in iter(lambda: stdout.read(4096), b""):
        buf += chunk
        while b"\x00" in buf:
            piece, buf = buf.split(b"\x00", 1)
            piece = piece.strip()
            if not piece:
                continue
            try:
                yield plistlib.loads(piece)
            except Exception:
                continue


def run_stream() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    proc = subprocess.Popen(_powermetrics_cmd(None), stdout=subprocess.PIPE)
    try:
        for pl in _iter_plists(proc.stdout):
            if not _running:
                break
            _write(parse_sample(pl))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


def run_once(dump: bool = False) -> int:
    out = subprocess.run(_powermetrics_cmd(1), capture_output=True)
    plists = list(_iter_plists_bytes(out.stdout))
    if not plists:
        print("no plist parsed; stderr:", out.stderr.decode("utf-8", "replace")[:400])
        return 1
    pl = plists[-1]
    if dump:
        print("top-level keys:", sorted(pl.keys()))
        if isinstance(pl.get("gpu"), dict):
            print("gpu keys:", sorted(pl["gpu"].keys()))
        if isinstance(pl.get("processor"), dict):
            print("processor keys:", sorted(pl["processor"].keys()))
        return 0
    print(json.dumps(parse_sample(pl), indent=2))
    return 0


def _iter_plists_bytes(data: bytes):
    for piece in data.split(b"\x00"):
        piece = piece.strip()
        if not piece:
            continue
        try:
            yield plistlib.loads(piece)
        except Exception:
            continue


def main() -> int:
    if os.geteuid() != 0:
        print("powermetrics_daemon must run as root (it shells out to powermetrics).",
              file=sys.stderr)
        return 2
    args = sys.argv[1:]
    if "--dump" in args:
        return run_once(dump=True)
    if "--once" in args:
        return run_once()
    return run_stream()


if __name__ == "__main__":
    sys.exit(main())
