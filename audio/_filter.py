"""Output-device filter file, shared by both platform backends.

`audio_filter.local.json` (gitignored, at project root) has an
`exclude_patterns` list of regex strings matched case-insensitively against a
device's friendly name; matching devices are skipped by `--cycle` but still
appear in `--list`. The format is OS-agnostic, so the loader lives here rather
than in either backend.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("audio")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FILTER_FILE = PROJECT_ROOT / "audio_filter.local.json"


def load_exclude_patterns() -> list[re.Pattern[str]]:
    try:
        data = json.loads(FILTER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    patterns: list[re.Pattern[str]] = []
    for p in data.get("exclude_patterns") or []:
        try:
            patterns.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("bad regex %r in %s: %s", p, FILTER_FILE.name, e)
    return patterns
