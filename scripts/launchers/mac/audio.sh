#!/bin/bash
# Loupedeck/terminal wrapper for `python -m audio`. Forwards all args.
# e.g. audio.sh --status   |   audio.sh --vol +5   |   brightness.sh 0 +5
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m audio "$@"
