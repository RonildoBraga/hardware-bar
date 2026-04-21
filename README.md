# hardware-bar

Minimalist always-on-top desktop bar showing CPU/GPU/RAM/SSD/network/brightness live for
this machine (i7-12700F / RTX 3080 / Crucial T500), plus per-monitor brightness control
designed to be driven from a Loupedeck CT.

## First-time setup

```
cd C:\Users\ronildo\Developer\hardware-bar
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## LibreHardwareMonitor (CPU + SSD temps, CPU power, fan tachos)

LHM provides CPU package temp/power and the disk temps/activity the bar shows.
It needs admin rights (CPU MSR access) and its HTTP remote server on port 8085.

**One-time LHM settings** — launch `vendor\LibreHardwareMonitor\LibreHardwareMonitor.exe`
once, then in the tray icon:

- `Options → Remote Web Server → Port` → `8085`
- `Options → Remote Web Server → Run` → on
- (optional) `Options → Minimize To Tray`, `Minimize On Close`, `Start Minimized`

Close LHM via the tray icon so these settings persist.

**Register auto-start** — right-click **`register-lhm-task.bat`** → Run as
administrator. The script creates an ONLOGON Task Scheduler entry (admin
elevation baked in), starts LHM immediately, and confirms port 8085 is live.

## Auto-start the full stack on login

Run these three installers once each; together they bring up everything on every
reboot without manual steps.

| Component         | Installer                                   | Admin? |
|-------------------|---------------------------------------------|--------|
| LHM               | `register-lhm-task.bat`                     | yes    |
| Brightness daemon | `install-brightness-daemon-autostart.bat`   | no     |
| Hardware bar      | `install-bar-autostart.bat`                 | no     |

Each `install-*` script drops a shortcut in the user Startup folder and starts
the component immediately. Pass `-Uninstall` to remove.

## Run / usage

Manual run:

```
run.bat                         # or: .venv\Scripts\pythonw.exe bar.py
```

- **Left-click + drag** to reposition. Position persists in `config.local.json`.
- **Right-click** for an exit menu.
- LHM missing → CPU/SSD fields show `--`. Everything non-LHM still works.
- Brightness daemon missing → `BRI` field disappears silently.

## Brightness control

Per-monitor brightness for a mixed HDR/SDR setup. HDR displays use Windows'
`SDRWhiteLevel` API (DDC/CI brightness is ignored by HDR firmware); SDR displays
use DDC/CI via `monitorcontrol`.

A tiny daemon holds display state in memory so Loupedeck dial ticks don't pay
Python startup + DDC enumeration every time.

### Loupedeck wiring

Point each dial CW/CCW event at `pythonw.exe` directly — bypassing any `.bat`
avoids the `cmd.exe` console flash on every tick:

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: C:\Users\ronildo\Developer\hardware-bar\brightness_client.py <idx> <delta>
```

Indices match `brightness_client.py --list` (Windows display-config order; on
this machine: `0=KAMN49QDQUCLA, 1=Smart TV, 2=Cintiq 16`). The bar's `BRI` field
is reordered independently via `BRIGHTNESS_DISPLAY_ORDER` in `bar.py`.

### Handy commands

```powershell
.\.venv\Scripts\python.exe brightness_client.py --ping    # is daemon alive?
.\.venv\Scripts\python.exe brightness_client.py --list    # what does it see?
.\.venv\Scripts\python.exe brightness_client.py 0 +5      # manual adjust
.\brightness-daemon-debug.bat                             # run daemon in foreground
Get-Content $env:TEMP\hardware-bar-brightness-daemon.log -Tail 20 -Wait
```

## Live charts

Per-metric live history plots (`charts.py`), toggled from a Loupedeck key.
Each metric gets its own `pyqtgraph` window that stays on top; a second press
on the same Loupedeck key closes it (single-instance via `QLocalServer`).

Loupedeck wiring — point each tile's Run Command at `pythonw.exe`:

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: C:\Users\ronildo\Developer\hardware-bar\charts.py <metric>
```

Supported metric names: keys of `METRICS` in `charts.py` (e.g. `cpu`, `cpu_temp`,
`gpu`, `gpu_temp`, `ram`, `net_down`, `net_up`, `disk_c`, `disk_d`, `disk_e`,
`disk_f`). Window positions persist under `.charts/` (gitignored). Log at
`%TEMP%\hardware-bar-charts.log`.

## Project layout

```
hardware-bar/
├── README.md
├── requirements.txt
├── config.local.json                       saved bar position (gitignored)
│
│  ── Python source ───────────────────────────────────────
├── bar.py                                  the always-on-top bar
├── charts.py                               per-metric live chart windows
├── brightness.py                           HDR/DDC helpers + standalone CLI
├── brightness_daemon.py                    TCP server holding display state
├── brightness_client.py                    thin client called by Loupedeck
│
│  ── Manual / Loupedeck launchers ───────────────────────
├── run.bat                                 start bar.py silently
├── charts.bat, charts-debug.bat            pythonw / python wrappers for charts
├── brightness-debug.bat                    visible-console brightness.py CLI
├── brightness-daemon.bat                   silent daemon launch (manual)
├── brightness-daemon-debug.bat             visible daemon launch (debug)
│
│  ── One-time installers ────────────────────────────────
├── register-lhm-task.bat / .ps1            admin ONLOGON task for LHM
├── install-bar-autostart.bat / .ps1        user Startup shortcut for bar.py
├── install-brightness-daemon-autostart.bat / .ps1  ditto for the daemon
│
└── vendor/
    └── LibreHardwareMonitor/               vendored LHM (gitignored)
```
