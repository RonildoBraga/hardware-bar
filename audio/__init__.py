"""Windows audio control — volume, mute, default output device cycling."""

from .core import (
    OutputDevice,
    cycle_output,
    get_default_device,
    get_mute,
    get_status,
    get_volume_pct,
    list_outputs,
    set_mute,
    set_volume_delta,
    toggle_mute,
)

__all__ = [
    "OutputDevice",
    "cycle_output",
    "get_default_device",
    "get_mute",
    "get_status",
    "get_volume_pct",
    "list_outputs",
    "set_mute",
    "set_volume_delta",
    "toggle_mute",
]
