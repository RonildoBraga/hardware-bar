#!/bin/bash
# Toggle a live chart window. e.g. charts.sh cpu | charts.sh gpu | charts.sh temps
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
exec "$DIR/.venv/bin/python" -m bar.charts "$@"
