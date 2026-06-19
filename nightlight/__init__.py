"""Blue-light reduction toggle — Windows Night Light / macOS Night Shift."""

from .core import is_enabled, set_state, toggle

__all__ = ["is_enabled", "set_state", "toggle"]
