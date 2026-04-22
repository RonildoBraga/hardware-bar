# hardware-bar

Minimalist always-on-top desktop bar showing CPU/GPU/RAM/SSD/network/brightness live for
this machine (i7-12700F / RTX 3080 / Crucial T500), plus per-monitor brightness control
and Windows Night Light toggle — all designed to be driven from a Loupedeck CT.

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

**Register auto-start** — right-click **`scripts\install\register-lhm-task.bat`** → Run as
administrator. The script creates an ONLOGON Task Scheduler entry (admin
elevation baked in), starts LHM immediately, and confirms port 8085 is live.

## Auto-start the full stack on login

Run these three installers once each; together they bring up everything on every
reboot without manual steps.

| Component         | Installer                                                    | Admin? |
|-------------------|--------------------------------------------------------------|--------|
| LHM               | `scripts\install\register-lhm-task.bat`                      | yes    |
| Brightness daemon | `scripts\install\install-brightness-daemon-autostart.bat`    | no     |
| Hardware bar      | `scripts\install\install-bar-autostart.bat`                  | no     |

Each `install-*` script drops a shortcut in the user Startup folder and starts
the component immediately. Pass `-Uninstall` to remove.

## Run / usage

Manual run:

```
scripts\launchers\run.bat       # or: .venv\Scripts\pythonw.exe -m bar
```

- **Left-click + drag** to reposition. Position persists in `config.local.json`.
- **Right-click** for an exit menu.
- LHM missing → CPU/SSD fields show `--`. Everything non-LHM still works.
- Brightness daemon missing → `BRI` field disappears silently.
- Night Light registry key absent → `NL` field disappears silently.
- Audio subsystem unreachable → `VOL`/`OUT` fields disappear silently.

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
Args: -m brightness.client <idx> <delta>
Working dir: C:\Users\ronildo\Developer\hardware-bar
```

The **Working dir must be the project root** for `-m` imports to resolve.

Indices match `python -m brightness.client --list` (Windows display-config order;
on this machine: `0=KAMN49QDQUCLA, 1=Smart TV, 2=Cintiq 16`). The bar's `BRI`
field is reordered independently via `BRIGHTNESS_DISPLAY_ORDER` in `bar/main.py`.

### Handy commands

```powershell
.\.venv\Scripts\python.exe -m brightness.client --ping    # is daemon alive?
.\.venv\Scripts\python.exe -m brightness.client --list    # what does it see?
.\.venv\Scripts\python.exe -m brightness.client 0 +5      # manual adjust
.\.venv\Scripts\python.exe -m brightness --list           # offline list (no daemon)
.\scripts\launchers\brightness-daemon-debug.bat           # run daemon in foreground
Get-Content $env:TEMP\hardware-bar-brightness-daemon.log -Tail 20 -Wait
```

## Night Light toggle

Windows 11's Night Light has no CLI, so the `nightlight` package flips a
2-byte marker inside the CloudStore registry blob the settings service
watches (and adjusts the preceding length byte + varint timestamp so
the display broker re-reads the state). No daemon — the cost is just
Python cold-start per press, which is fine for a button.

The exact registry path and blob layout are documented in
`nightlight/core.py`'s module docstring, since they vary across Windows
builds and older articles reference stale paths.

### Loupedeck wiring

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: -m nightlight --toggle
Working dir: C:\Users\ronildo\Developer\hardware-bar
```

### Handy commands

```powershell
.\.venv\Scripts\python.exe -m nightlight --status    # on | off | unknown
.\.venv\Scripts\python.exe -m nightlight --toggle
.\.venv\Scripts\python.exe -m nightlight --on
.\.venv\Scripts\python.exe -m nightlight --off
.\scripts\launchers\nightlight-debug.bat --toggle    # visible console
Get-Content $env:TEMP\hardware-bar-nightlight.log -Tail 20 -Wait
```

The bar's `NL` field reads the same registry blob every tick and shows
`NL on` (warm orange) or `NL off`.

## Audio (volume, mute, output device cycling)

Master volume and mute via pycaw (IAudioEndpointVolume); default-output
switching via the undocumented `IPolicyConfig` COM interface. Cycling
rotates through active render endpoints in Windows' enumeration order,
skipping anything matching `audio_filter.local.json` (gitignored, at
project root). No daemon — COM calls are sub-millisecond.

### Loupedeck wiring

```
Volume up:    Args: -m audio --vol +5
Volume down:  Args: -m audio --vol -5
Mute toggle:  Args: -m audio --mute
Cycle output: Args: -m audio --cycle
```
(File = `pythonw.exe`, Working dir = project root, same as brightness.)

### Filter file (optional)

Drop a file called `audio_filter.local.json` in the project root with:

```json
{
  "exclude_patterns": [
    "HDMI",
    "NVIDIA"
  ]
}
```

Patterns are case-insensitive regex matched against the FriendlyName.
Devices matching any pattern are skipped by `--cycle` but still listed
by `--list`.

### Handy commands

```powershell
.\.venv\Scripts\python.exe -m audio --status    # vol=88% device=Smart TV ...
.\.venv\Scripts\python.exe -m audio --list      # all active outputs, * marks default
.\.venv\Scripts\python.exe -m audio --vol +5
.\.venv\Scripts\python.exe -m audio --mute
.\.venv\Scripts\python.exe -m audio --cycle
.\scripts\launchers\audio-debug.bat --list      # visible console
Get-Content $env:TEMP\hardware-bar-audio.log -Tail 20 -Wait
```

The bar's `VOL` field shows `VOL 88%` (or `VOL MUTE` in red when muted),
and `OUT` shows the current device name with the driver suffix stripped.

## Live charts

Per-metric live history plots (`bar.charts`), toggled from a Loupedeck key.
Each metric gets its own `pyqtgraph` window that stays on top; a second press
on the same Loupedeck key closes it (single-instance via `QLocalServer`).

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: -m bar.charts <metric>
Working dir: C:\Users\ronildo\Developer\hardware-bar
```

Supported metric names: keys of `METRICS` in `bar/charts.py` (e.g. `cpu`,
`cpu-temp`, `gpu`, `gpu-temp`, `ram`, `disk`, `disk-temps`, `net`,
`cpu-gpu`, `temps`). Window positions persist under `.charts/`
(gitignored). Log at `%TEMP%\hardware-bar-charts.log`.

## Project layout

```
hardware-bar/
├── README.md
├── requirements.txt
├── config.local.json                          saved bar position (gitignored)
│
├── bar/                                       always-on-top bar + live charts
│   ├── __main__.py                            `python -m bar`
│   ├── main.py                                Qt UI, Poller, Sample
│   └── charts.py                              `python -m bar.charts <metric>`
│
├── brightness/                                DDC/CI + HDR SDR-white-level control
│   ├── __main__.py                            `python -m brightness` (offline CLI)
│   ├── core.py                                enumeration, HDR detection, DDC helpers
│   ├── daemon.py                              `python -m brightness.daemon`
│   └── client.py                              `python -m brightness.client` (Loupedeck)
│
├── nightlight/                                Windows 11 Night Light toggle
│   ├── __main__.py                            `python -m nightlight`
│   └── core.py                                registry-blob manipulation + public API
│
├── audio/                                     master volume / mute / output cycling
│   ├── __main__.py                            `python -m audio`
│   └── core.py                                pycaw wrappers + IPolicyConfig switch
│
├── scripts/
│   ├── launchers/                             manual / Loupedeck launchers
│   │   ├── run.bat                            start the bar silently
│   │   ├── charts.bat, charts-debug.bat       pythonw / python wrappers
│   │   ├── brightness-debug.bat               visible-console brightness CLI
│   │   ├── brightness-daemon.bat              silent daemon launch (manual)
│   │   ├── brightness-daemon-debug.bat        visible daemon launch (debug)
│   │   ├── nightlight-debug.bat               visible-console nightlight CLI
│   │   └── audio-debug.bat                    visible-console audio CLI
│   └── install/                               one-time installers
│       ├── register-lhm-task.bat / .ps1       admin ONLOGON task for LHM
│       ├── install-bar-autostart.bat / .ps1   user Startup shortcut for the bar
│       └── install-brightness-daemon-autostart.bat / .ps1   ditto for the daemon
│
└── vendor/
    └── LibreHardwareMonitor/                  vendored LHM (gitignored)
```

## Migrating existing Loupedeck bindings

If you bound buttons before the `scripts/` + package reorganisation, update
each binding to the module-invocation form. The File field stays the same
(`pythonw.exe`); the Args field changes, and the Working Directory must
be the project root.

| Old Args                                             | New Args                              |
|------------------------------------------------------|---------------------------------------|
| `C:\...\hardware-bar\brightness_client.py 0 +5`      | `-m brightness.client 0 +5`           |
| `C:\...\hardware-bar\charts.py cpu`                  | `-m bar.charts cpu`                   |
| `C:\...\hardware-bar\nightlight.py --toggle`         | `-m nightlight --toggle`              |
| `C:\...\hardware-bar\bar.py`                         | `-m bar`                              |

Autostart shortcuts installed before the reorg also point at the old paths
— re-run the three `scripts\install\install-*.bat` scripts to refresh them.
