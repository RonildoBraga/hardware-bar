#!/bin/bash
# Loupedeck/terminal wrapper for `python -m nightlight`. Forwards all args.
# e.g. nightlight.sh --status   |   nightlight.sh --vol +5   |   brightness.sh 0 +5
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m nightlight "$@"
