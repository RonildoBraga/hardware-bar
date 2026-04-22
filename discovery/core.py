"""discovery/core.py — reconnaissance scan for smart-home devices on the LAN.

Passive discovery only — we listen and broadcast standard query packets,
nothing gets controlled or altered. Two protocols:

- mDNS / Bonjour (UDP 5353) via `zeroconf`. Finds HomeKit, Chromecast,
  AirPlay, Sonos, Matter commissioners, Spotify Connect receivers, Roku,
  network printers, etc.
- SSDP (UDP 1900 multicast) via a raw socket. Finds UPnP devices:
  Hue bridges, DLNA servers, many older IoT boxes.

Results are grouped by IP so the same physical device showing multiple
services collapses into a single block.

Usage:
    python -m discovery                     # default 3s scan
    python -m discovery --timeout 8         # longer scan, more devices
    python -m discovery --json              # machine-readable output
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

LOG_PATH = Path(tempfile.gettempdir()) / "hardware-bar-discovery.log"
log = logging.getLogger("discovery")


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(process)5d] %(levelname)s: %(message)s",
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


@dataclass
class Device:
    protocol:     str                         # 'mdns' | 'ssdp'
    ip:           str
    port:         int | None = None
    name:         str = ""
    service_type: str = ""
    extras:       dict[str, str] = field(default_factory=dict)


# -------- mDNS ----------------------------------------------------------

# Direct probes for well-known smart-home service types. The meta-query
# `_services._dns-sd._udp.local.` also runs alongside to catch anything
# we didn't hardcode; types it reports are probed in a second pass.
KNOWN_MDNS_TYPES = [
    "_hap._tcp.local.",                 # HomeKit accessories (Matter bridges advertise here too)
    "_matter._tcp.local.",              # Matter operational
    "_matterc._udp.local.",             # Matter commissionable
    "_googlecast._tcp.local.",          # Chromecast / Google Home
    "_airplay._tcp.local.",             # AirPlay
    "_raop._tcp.local.",                # AirTunes (AirPlay speakers, HomePods)
    "_spotify-connect._tcp.local.",     # Spotify Connect receivers
    "_sonos._tcp.local.",
    "_roku-rcp._tcp.local.",
    "_printer._tcp.local.",
    "_ipp._tcp.local.",
    "_dlna-server._tcp.local.",
    "_daap._tcp.local.",
    "_nvstream._tcp.local.",            # NVIDIA Shield
    "_hue._tcp.local.",                 # Hue bridge (newer firmwares)
]
META_TYPE = "_services._dns-sd._udp.local."


class _MDNSCollector(ServiceListener):
    def __init__(self, zc: Zeroconf) -> None:
        self._zc = zc
        self.devices: list[Device] = []
        self.discovered_types: set[str] = set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        # Meta-query: the 'name' here is the advertised subtype.
        if type_ == META_TYPE:
            self.discovered_types.add(name)
            return
        info = zc.get_service_info(type_, name, timeout=2000)
        if info is None:
            return
        ips = [socket.inet_ntoa(a) for a in (info.addresses or []) if len(a) == 4]
        if not ips:
            return
        extras: dict[str, str] = {}
        for k, v in (info.properties or {}).items():
            try:
                ks = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
                vs = (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                      if v is not None else "")
            except Exception:
                continue
            extras[ks] = vs
        self.devices.append(Device(
            protocol="mdns",
            ip=ips[0],
            port=info.port,
            name=(info.name or name).rstrip("."),
            service_type=type_.rstrip("."),
            extras=extras,
        ))

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def scan_mdns(timeout_s: float = 3.0) -> list[Device]:
    zc = Zeroconf(ip_version=IPVersion.V4Only)
    col = _MDNSCollector(zc)
    # First pass: known types + meta-query
    browsers = [ServiceBrowser(zc, t, col) for t in KNOWN_MDNS_TYPES + [META_TYPE]]
    try:
        time.sleep(timeout_s)
        # Second pass: probe any meta-discovered types we didn't hardcode
        unknown = [t for t in col.discovered_types if t not in KNOWN_MDNS_TYPES]
        for t in unknown:
            browsers.append(ServiceBrowser(zc, t, col))
        if unknown:
            time.sleep(min(timeout_s, 2.0))
    finally:
        for b in browsers:
            try:
                b.cancel()
            except Exception:
                pass
        zc.close()
    return col.devices


# -------- SSDP ----------------------------------------------------------

SSDP_MCAST_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 2

SSDP_QUERY = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_MCAST_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    f"MX: {SSDP_MX}\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
).encode("ascii")


def scan_ssdp(timeout_s: float = 3.0) -> list[Device]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    try:
        sock.sendto(SSDP_QUERY, (SSDP_MCAST_ADDR, SSDP_PORT))
    except OSError as e:
        log.error("ssdp send failed: %s", e)
        sock.close()
        return []

    devices: list[Device] = []
    seen: set[tuple[str, str]] = set()
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            data, (ip, port) = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        headers: dict[str, str] = {}
        for line in data.decode("utf-8", "replace").splitlines()[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().upper()] = v.strip()
        st = headers.get("ST", "")
        key = (ip, st)
        if key in seen:
            continue
        seen.add(key)
        devices.append(Device(
            protocol="ssdp",
            ip=ip,
            port=port,
            name=headers.get("SERVER") or st,
            service_type=st,
            extras={
                "server":   headers.get("SERVER", ""),
                "usn":      headers.get("USN", ""),
                "location": headers.get("LOCATION", ""),
            },
        ))
    sock.close()
    return devices


# -------- CLI -----------------------------------------------------------

def _print_human(devices: list[Device]) -> None:
    by_ip: dict[str, list[Device]] = {}
    for d in devices:
        by_ip.setdefault(d.ip, []).append(d)
    if not by_ip:
        print("(nothing found)")
        return
    for ip in sorted(by_ip.keys(), key=lambda s: tuple(int(x) for x in s.split(".") if x.isdigit()) or (s,)):
        entries = by_ip[ip]
        print(f"=== {ip} ===")
        for d in entries:
            label = d.name or d.service_type or "?"
            print(f"  [{d.protocol}] {label}")
            if d.service_type and d.service_type not in d.name:
                print(f"      type: {d.service_type}")
            if d.port:
                print(f"      port: {d.port}")
            # Show up to 4 useful extras, trimmed.
            shown = 0
            for k, v in d.extras.items():
                if not v or shown >= 4:
                    continue
                trimmed = v if len(v) <= 90 else v[:87] + "..."
                print(f"      {k}: {trimmed}")
                shown += 1
        print()


def main() -> int:
    _setup_logging()
    args = sys.argv[1:]
    log.info("=" * 50)
    log.info("launch argv=%s log=%s", args, LOG_PATH)

    timeout = 3.0
    as_json = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a == "--timeout":
            i += 1
            try:
                timeout = float(args[i])
            except (IndexError, ValueError):
                print("usage: --timeout <seconds>")
                return 2
        elif a == "--json":
            as_json = True
        else:
            print(f"unknown argument: {a}")
            return 2
        i += 1

    if not as_json:
        print(f"Scanning for {timeout}s (mDNS + SSDP)...\n")
    mdns = scan_mdns(timeout)
    ssdp = scan_ssdp(timeout)
    all_devices = mdns + ssdp

    if as_json:
        json.dump([asdict(d) for d in all_devices], sys.stdout, indent=2)
        print()
    else:
        _print_human(all_devices)
        print(f"Summary: {len(mdns)} mDNS, {len(ssdp)} SSDP, "
              f"{len({d.ip for d in all_devices})} unique IP(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
