"""Brightness control — DDC/CI + HDR SDRWhiteLevel helpers, daemon, client.

Submodules:
    core      display enumeration, HDR detection, DDC/SDR-white-level helpers
    daemon    long-running adjuster that holds display state in memory
    client    thin TCP client the Loupedeck calls
    protocol  dependency-free wire constants + status codec

Nothing is re-exported here on purpose: `core` pulls in monitorcontrol/ctypes,
so importing the package (e.g. `from brightness.protocol import ...`) must stay
cheap for the bar and client.
"""
