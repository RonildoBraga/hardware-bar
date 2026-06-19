#!/bin/bash
# Install (or remove) the privileged powermetrics sampler as a LaunchDaemon.
#
# This is the macOS analogue of scripts/install/register-lhm-task.bat: it grants
# the bar the GPU%/power data Apple gates behind root, by running a tiny stdlib
# sampler (macos/powermetrics_daemon.py) as root at boot and writing samples to
# /tmp/hardware-bar-powermetrics.json, which the user-level bar reads.
#
# Temperatures and fan RPM do NOT need this — the bar reads those without
# privileges. Install this only if you also want GPU load and CPU/GPU power.
#
# Usage:
#   sudo ./scripts/install/install-powermetrics-daemon.sh
#   sudo ./scripts/install/install-powermetrics-daemon.sh --uninstall
set -euo pipefail

LABEL="com.ronildo.hardware-bar.powermetrics"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DAEMON="${ROOT}/macos/powermetrics_daemon.py"
PYTHON="/usr/bin/python3"   # system python: stdlib-only daemon, no venv needed

if [[ "${EUID}" -ne 0 ]]; then
  echo "This installer must run as root. Re-run with: sudo $0 $*" >&2
  exit 1
fi

uninstall() {
  echo "Removing ${LABEL}..."
  launchctl bootout "system/${LABEL}" 2>/dev/null || launchctl unload -w "${PLIST}" 2>/dev/null || true
  rm -f "${PLIST}"
  rm -f /tmp/hardware-bar-powermetrics.json
  echo "Uninstalled."
}

if [[ "${1:-}" == "--uninstall" || "${1:-}" == "-u" ]]; then
  uninstall
  exit 0
fi

if [[ ! -f "${DAEMON}" ]]; then
  echo "Daemon not found at ${DAEMON}" >&2
  exit 1
fi

echo "Installing ${LABEL}"
echo "  python : ${PYTHON}"
echo "  daemon : ${DAEMON}"

cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DAEMON}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/hardware-bar-powermetrics-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/hardware-bar-powermetrics-daemon.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

chown root:wheel "${PLIST}"
chmod 644 "${PLIST}"

# Reload if already present, then start.
launchctl bootout "system/${LABEL}" 2>/dev/null || true
if launchctl bootstrap system "${PLIST}" 2>/dev/null; then
  launchctl kickstart -k "system/${LABEL}" 2>/dev/null || true
else
  # Fallback for older launchctl.
  launchctl load -w "${PLIST}"
fi

echo
echo "Installed and started. Verify with:"
echo "  cat /tmp/hardware-bar-powermetrics.json   # should show gpu_pct / power within ~2s"
echo "  sudo ${PYTHON} ${DAEMON} --dump           # inspect raw powermetrics keys"
echo
echo "The bar picks up GPU%/power automatically on its next tick."
