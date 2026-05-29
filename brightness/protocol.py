"""Wire protocol shared by the brightness daemon, its client, and the bar.

Kept dependency-free (stdlib only) so importing it never drags in
monitorcontrol/ctypes — the bar and client want the host/port and the status
codec without the DDC/DisplayConfig machinery that lives in core.py.
"""

from __future__ import annotations

HOST = "127.0.0.1"
PORT = 48736


def format_status(pairs: list[tuple[int, int | None]]) -> str:
    """Encode per-display percents as the status wire format.

    [(0, 40), (1, None), (2, 50)] -> '0:40 1:- 2:50'
    """
    return " ".join(f"{i}:{pct if pct is not None else '-'}" for i, pct in pairs)


def parse_status(reply: str) -> list[int | None]:
    """Decode a status reply into a list indexed by display index, with None
    for unknown values. Returns [] for empty or error ('err...') replies."""
    reply = reply.strip()
    if not reply or reply.startswith("err"):
        return []
    parsed: dict[int, int | None] = {}
    for tok in reply.split():
        if ":" not in tok:
            continue
        i_str, v_str = tok.split(":", 1)
        try:
            i = int(i_str)
        except ValueError:
            continue
        try:
            parsed[i] = int(v_str)
        except ValueError:
            parsed[i] = None
    if not parsed:
        return []
    return [parsed.get(i) for i in range(max(parsed) + 1)]
