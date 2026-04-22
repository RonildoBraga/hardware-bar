"""Passive LAN discovery for smart-home devices (mDNS + SSDP)."""

from .core import Device, scan_mdns, scan_ssdp

__all__ = ["Device", "scan_mdns", "scan_ssdp"]
