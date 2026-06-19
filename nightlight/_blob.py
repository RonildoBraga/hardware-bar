"""Pure CloudStore-blob manipulation for the Windows Night Light backend.

Split out of core.py so it carries no `winreg` import and can therefore be
imported (and unit-tested) on any platform. The Windows backend (`_win.py`)
pairs these pure byte operations with the actual registry read/write.

Blob layout (reverse-engineered empirically) and the toggle algorithm are
documented on the functions below; the registry path lives in `_win.py`.
"""

from __future__ import annotations

SENTINEL      = bytes([0x43, 0x42, 0x01, 0x00])  # 'CB\x01\x00' — appears twice
ENABLE_MARKER = bytes([0x10, 0x00])              # after INNER sentinel => ON
TIMESTAMP_RANGE = range(10, 15)                  # varint bytes to bump


def _inner_sentinel_offsets(blob: bytes) -> tuple[int, int]:
    """Locate the inner state section.

    Returns (length_idx, after_inner_sentinel). Raises if the blob doesn't
    match the expected shape.
    """
    first = blob.find(SENTINEL)
    if first < 0:
        raise RuntimeError("outer sentinel not found")
    second = blob.find(SENTINEL, first + len(SENTINEL))
    if second < 0:
        raise RuntimeError("inner sentinel not found")
    length_idx = second - 1  # length byte sits immediately before inner sentinel
    if length_idx < 0:
        raise RuntimeError("no length byte before inner sentinel")
    return length_idx, second + len(SENTINEL)


def _is_enabled_from_blob(blob: bytes) -> bool:
    try:
        _, pos = _inner_sentinel_offsets(blob)
    except RuntimeError:
        return False
    return blob[pos:pos + len(ENABLE_MARKER)] == ENABLE_MARKER


def _bump_timestamp(blob: bytearray) -> None:
    """Nudge the varint timestamp forward so the display broker re-reads state."""
    for i in TIMESTAMP_RANGE:
        if i < len(blob) and blob[i] != 0xFF:
            blob[i] = (blob[i] + 1) & 0xFF
            return


def _set_enabled_in_blob(blob: bytes, enabled: bool) -> bytes:
    length_idx, pos = _inner_sentinel_offsets(blob)
    currently_on = blob[pos:pos + len(ENABLE_MARKER)] == ENABLE_MARKER
    length = blob[length_idx]
    if enabled and not currently_on:
        new = bytearray(blob[:length_idx] + bytes([length + len(ENABLE_MARKER)])
                        + blob[length_idx + 1:pos] + ENABLE_MARKER + blob[pos:])
    elif not enabled and currently_on:
        new = bytearray(blob[:length_idx] + bytes([length - len(ENABLE_MARKER)])
                        + blob[length_idx + 1:pos]
                        + blob[pos + len(ENABLE_MARKER):])
    else:
        new = bytearray(blob)  # already in desired state; just bump timestamp
    _bump_timestamp(new)
    return bytes(new)
