#!/bin/bash
# Loupedeck dial wrapper -> brightness daemon client (spawns daemon on first use).
# e.g. brightness.sh 0 +5   |   brightness.sh --list   |   brightness.sh --ping
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m brightness.client "$@"
