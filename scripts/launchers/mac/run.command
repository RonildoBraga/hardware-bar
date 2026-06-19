#!/bin/bash
# Start the hardware bar detached (double-clickable in Finder, or bind to a
# Loupedeck key). macOS analogue of scripts/launchers/run.bat.
DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DIR"
nohup "$DIR/.venv/bin/python" -m bar >/tmp/hardware-bar-launch.log 2>&1 &
