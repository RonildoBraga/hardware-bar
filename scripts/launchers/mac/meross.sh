#!/bin/bash
# Loupedeck/terminal wrapper for `python -m meross`. Forwards all args.
# e.g. meross.sh --status   |   meross.sh --vol +5   |   brightness.sh 0 +5
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m meross "$@"
