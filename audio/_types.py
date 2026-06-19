"""Shared audio types — kept dependency-free so both platform backends and
the dispatcher in core.py can import them without a circular import."""

from __future__ import annotations

from typing import NamedTuple


class OutputDevice(NamedTuple):
    id:   str
    name: str
