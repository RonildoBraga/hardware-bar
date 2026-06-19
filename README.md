# hardware-bar

Minimalist always-on-top desktop bar showing CPU/GPU/RAM/SSD/network/brightness live,
plus per-monitor brightness control, blue-light-reduction toggle, audio control, and
smart-plug control — all designed to be driven from a Loupedeck CT.

Runs on **Windows** (i7-12700F / RTX 3080 / Crucial T500) and **macOS** (Apple Silicon)
from a single, shared codebase. Each OS-specific capability has a platform backend
selected at runtime via `sys.platform`; anything a backend can't supply on a given
machine renders as `--` (or is hidden) rather than failing.

## Platform support

| Capability                        | Windows                                   | macOS (Apple Silicon)                                  |
|-----------------------------------|-------------------------------------------|--------------------------------------------------------|
| CPU % · clock · RAM · network     | psutil + PDH Processor Utility            | psutil                                                 |
| Volume · mute                     | pycaw (IAudioEndpointVolume)              | `osascript` (always available)                         |
| Audio output switching            | IPolicyConfig COM                         | `SwitchAudioSource` (brew)                             |
| Blue-light toggle                 | Night Light (registry blob)               | Night Shift (`nightlight` CLI, brew)                   |
| External display brightness       | DDC/CI (monitorcontrol) + HDR SDRWhiteLevel | `m1ddc` (brew)                                        |
| Built-in display brightness       | n/a                                       | DisplayServices (private framework, ctypes — no install) |
| CPU/SSD temperature               | LibreHardwareMonitor HTTP                 | IOKit IOHID sensors (ctypes — **no sudo**)             |
| Fan RPM                           | LibreHardwareMonitor HTTP                 | AppleSMC IOConnect (ctypes — **no sudo**)              |
| GPU load · CPU/GPU power          | pynvml + LibreHardwareMonitor             | `powermetrics` privileged daemon (one-time sudo install) |
| Smart plugs (Meross) · LAN scan   | cloud / mDNS+SSDP (cross-platform)        | same                                                   |

The architecture: each capability module exposes a stable public API and dispatches to a
`_win.py` / `_mac.py` backend. The bar's hardware sensors sit behind `bar/sensors.py`
(`WindowsBackend` / `MacBackend`). Pure logic that the tests pin — the LHM JSON parser
(`bar/model.py`), the Night Light blob codec (`nightlight/_blob.py`), and the SDR
white-level math (`brightness/_scale.py`) — is platform-free and runs everywhere.

## First-time setup — Windows

```
cd C:\Users\ronildo\Developer\hardware-bar
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## First-time setup — macOS

Use Python 3.13 (3.14 has no PyQt6 wheels yet):

```
cd ~/Developer/mac-bar
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` uses environment markers, so `pip` installs only the relevant
backends per OS (pycaw/monitorcontrol/nvidia-ml-py on Windows; PyObjC on macOS).

Optional Homebrew tools — install only the features you want; each degrades to `--`
or a hidden field if its tool is absent:

```
brew install m1ddc                  # external-display brightness (DDC)
brew install switchaudio-osx        # audio output-device cycling
brew install smudge/smudge/nightlight   # Night Shift toggle
```

Built-in-display brightness, CPU/SSD temperature, and fan RPM need **no** install —
they're read directly via system frameworks. GPU load and CPU/GPU power need the
optional privileged sampler (see *GPU load & power on macOS* below).

Run it:

```
.venv/bin/python -m bar          # or: scripts/launchers/mac/run.command
```

## GPU load & power on macOS (optional, needs sudo once)

macOS gates GPU activity and package power behind root (`powermetrics`). Rather than run
the GUI as root, a tiny stdlib-only sampler runs as root via a LaunchDaemon and writes
samples to `/tmp/hardware-bar-powermetrics.json`, which the user-level bar reads — the
same privileged-service pattern LibreHardwareMonitor uses on Windows.

```
sudo ./scripts/install/install-powermetrics-daemon.sh        # install + start
sudo ./scripts/install/install-powermetrics-daemon.sh -u     # uninstall
```

Verify it's producing data:

```
cat /tmp/hardware-bar-powermetrics.json                      # gpu_pct / *_power_w within ~2s
sudo /usr/bin/python3 macos/powermetrics_daemon.py --dump    # inspect raw powermetrics keys
```

The plist key names powermetrics uses for GPU residency/power vary across macOS releases;
the parser (`macos/powermetrics_daemon.py`) searches the common locations. If GPU%/power
stay `--` after install, run `--dump` and adjust `parse_sample()` to match your build.

## macOS Loupedeck wiring

Point each Loupedeck command at the venv Python with the module form, working dir =
project root — same model as Windows, different paths:

```
Command: ~/Developer/mac-bar/.venv/bin/python
Args:    -m brightness.client 0 +5      (CW/CCW dial -> per-display brightness)
         -m audio --vol +5              (volume up)
         -m audio --cycle               (cycle output device)
         -m nightlight --toggle         (Night Shift)
         -m bar.charts cpu              (toggle a live chart)
Working dir: ~/Developer/mac-bar
```

Thin wrappers are provided in `scripts/launchers/mac/` (`brightness.sh`, `audio.sh`,
`nightlight.sh`, `charts.sh`, `meross.sh`, and `run.command`) if you'd rather bind to a
script than a python invocation.

> The sections below document the **Windows** implementation details. On macOS
> these capabilities use native frameworks/CLIs instead — see *Platform support*
> and *First-time setup — macOS* above; the `python -m <module>` CLIs are
> identical (swap `.venv\Scripts\python.exe` for `.venv/bin/python`).

## LibreHardwareMonitor — Windows (CPU + SSD temps, CPU power, fan tachos)

LHM provides CPU package temp/power and the disk temps/activity the bar shows.
It needs admin rights (CPU MSR access) and its HTTP remote server on port 8085.

**One-time LHM settings** — launch `vendor\LibreHardwareMonitor\LibreHardwareMonitor.exe`
once, then in the tray icon:

- `Options → Remote Web Server → Port` → `8085`
- `Options → Remote Web Server → Run` → on
- (optional) `Options → Minimize To Tray`, `Minimize On Close`, `Start Minimized`

Close LHM via the tray icon so these settings persist.

**Register the on-demand task** — right-click
**`scripts\install\register-lhm-task.bat`** → Run as administrator. This
registers a Task Scheduler entry with `/RL HIGHEST` and **no triggers**, so
LHM never starts on its own. What it buys you is the permission grant: the
bar can fire `schtasks /Run /TN LibreHardwareMonitor` and LHM launches
elevated silently, no UAC prompt.

## Startup model

Nothing in this project auto-starts on login. Everything is manual or
lazy-launched:

| Component              | How it starts                                                                                   |
|------------------------|-------------------------------------------------------------------------------------------------|
| Hardware bar + charts  | Manual — `scripts\launchers\run.bat` (or a Loupedeck key bound to `pythonw -m bar`)             |
| Brightness daemon      | Auto — `brightness.client` spawns it the first time a dial press finds it unreachable           |
| LibreHardwareMonitor   | Auto — the bar's Poller calls `schtasks /Run` the first time `localhost:8085` is unreachable    |

Only LHM needs a one-time admin install (see *Register the on-demand task*
above). The brightness daemon needs nothing — the client spawns it via
`pythonw -m brightness.daemon` on demand. Both auto-launch paths are
idempotent: if the service is already running, nothing extra happens.

The legacy Startup-folder installers
(`install-bar-autostart.*`, `install-brightness-daemon-autostart.*`) are kept
in `scripts/install/` for anyone who wants the old login-start behaviour back;
pass `-Uninstall` to remove a shortcut they previously installed.

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

## Smart plugs (Meross)

Control Meross Wi-Fi smart plugs (and anything else in your Meross account)
via the `meross_iot` Python library. Authenticates to the Meross cloud
once with your account credentials, caches the resulting token, and talks
to the plugs over Meross's MQTT broker. Works for this ExpressVPN-router
network where multicast/LAN-local discovery is suppressed — control traffic
is cloud-routed, not LAN-broadcast.

### One-time credentials

Create `meross_creds.local.json` at the project root (gitignored):

```json
{
  "email": "you@example.com",
  "password": "your-meross-app-password",
  "api_base_url": null
}
```

Use the same email + password you log in to the Meross app with.
`api_base_url` can stay `null` — the code defaults to the Asia-Pacific
endpoint for Australia; Meross auto-redirects regardless.

On first run the code logs in via HTTPS, saves the resulting token to
`meross_token.local.json` (also gitignored), and re-uses it on every
subsequent call so the slow login only happens once.

### Loupedeck wiring

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: C:\Users\ronildo\Developer\hardware-bar\meross\core.py --toggle Dehumidifier
```

Names are matched case-insensitively against the names shown in the Meross
app (not the router dashboard). Use `--list` to confirm the exact strings.

### Handy commands

```powershell
.\.venv\Scripts\python.exe -m meross --list                 # all plugs, with on/off + power
.\.venv\Scripts\python.exe -m meross --status Dehumidifier
.\.venv\Scripts\python.exe -m meross --on     Dehumidifier
.\.venv\Scripts\python.exe -m meross --off    Dehumidifier
.\.venv\Scripts\python.exe -m meross --toggle Dehumidifier
.\scripts\launchers\meross-debug.bat --list                 # visible console
Get-Content $env:TEMP\hardware-bar-meross.log -Tail 20 -Wait
```

Latency budget: first cold call ~1-2s (HTTP login + MQTT connect).
Subsequent calls ~300-500ms (cached token, only MQTT connect). If this
becomes uncomfortable for rapid buttons, we can add a daemon like
`brightness/daemon.py` that keeps MQTT open; start without one.

### Live energy chart

`python -m meross.chart` opens a stacked-area window showing per-plug
power draw over the last 20 minutes. Each device is a coloured band;
the top edge of the stack is the total draw; a text header shows
live TOTAL and top-3 consumers.

```
File: C:\Users\ronildo\Developer\hardware-bar\.venv\Scripts\pythonw.exe
Args: -m meross.chart
Working dir: C:\Users\ronildo\Developer\hardware-bar
```

Architecture: background asyncio thread keeps the Meross MQTT
connection open and polls all `ElectricityMixin` devices in parallel
every 10 s (`POLL_INTERVAL_S`). Qt timer at 1 Hz repaints from the
shared buffer. Each sample is also appended to `.charts/meross-energy
.csv` for later analysis (ISO timestamp, device name, watts).

Launching it while already open closes the existing window, same
single-instance toggle pattern as the other charts.

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

On macOS the metric set is retuned for the unified SoC: `gpu-temp` and `disk`
(activity) are dropped (no discrete GPU temperature, no per-drive activity), the
`disk-temps`/`temps` charts track the single internal SSD, CPU charts use a
0–100% range (no Turbo), and the RAM range follows installed memory.

## Project layout

Platform backends are split into `_win.py` / `_mac.py` behind each module's
`core.py` dispatcher; pure, OS-free logic lives in leaf modules (`_blob.py`,
`_scale.py`, `model.py`) so the unit tests run on any OS.

```
hardware-bar/  (cloned as mac-bar/ on macOS)
├── README.md
├── requirements.txt                          env-marker deps (Windows vs macOS)
├── _common.py                                shared helpers + IS_WINDOWS/IS_MACOS flags
├── config.local.json                         saved bar position (gitignored)
│
├── bar/                                       always-on-top bar + live charts
│   ├── __main__.py                            `python -m bar`
│   ├── main.py                                Qt UI, Poller, render (cross-platform)
│   ├── model.py                               Sample/DiskReading + LHM JSON parser (pure)
│   ├── sensors.py                             make_backend() -> Windows/Mac sensor backend
│   ├── _win.py                                PDH CPU + pynvml GPU + LHM HTTP + schtasks
│   ├── _mac.py                                psutil + IOHID temps + SMC fans + powermetrics
│   ├── iohid.py                               SoC/SSD temperature sensors (macOS, no sudo)
│   ├── smc.py                                 AppleSMC fan RPM (macOS, no sudo)
│   ├── macwin.py                              macOS accessory mode + all-Spaces floating
│   └── charts.py                              `python -m bar.charts <metric>`
│
├── brightness/                                per-monitor brightness control
│   ├── __main__.py                            `python -m brightness` (offline CLI)
│   ├── core.py                                platform dispatcher + offline CLI
│   ├── _scale.py                              SDR white-level <-> percent math (pure, tested)
│   ├── _win.py                                DisplayConfig DDC/CI + HDR SDRWhiteLevel
│   ├── _mac.py                                DisplayServices (built-in) + m1ddc (external)
│   ├── _displayservices.py                    built-in-panel brightness via ctypes (macOS)
│   ├── protocol.py                            dependency-free wire constants + status codec
│   ├── daemon.py                              `python -m brightness.daemon`
│   └── client.py                              `python -m brightness.client` (Loupedeck)
│
├── nightlight/                                blue-light toggle (Night Light / Night Shift)
│   ├── __main__.py                            `python -m nightlight`
│   ├── core.py                                platform dispatcher + CLI
│   ├── _blob.py                               Night Light registry-blob codec (pure, tested)
│   ├── _win.py                                Windows registry read/write
│   └── _mac.py                                Night Shift via the `nightlight` CLI
│
├── audio/                                     master volume / mute / output cycling
│   ├── __main__.py                            `python -m audio`
│   ├── core.py                                platform dispatcher + CLI
│   ├── _types.py, _filter.py                  OutputDevice + output-filter file (shared)
│   ├── _win.py                                pycaw + IPolicyConfig COM
│   └── _mac.py                                osascript volume/mute + SwitchAudioSource
│
├── discovery/                                 LAN reconnaissance (mDNS + SSDP, cross-platform)
├── meross/                                    Meross smart-plug control (cross-platform)
│
├── macos/                                     macOS-only privileged helper
│   └── powermetrics_daemon.py                root sampler -> GPU%/power JSON (stdlib only)
│
├── scripts/
│   ├── launchers/                             Windows .bat launchers
│   │   └── mac/                               macOS launchers: run.command + *.sh wrappers
│   └── install/                               one-time installers
│       ├── register-lhm-task.bat / .ps1       admin on-demand task for LHM (Windows)
│       ├── install-powermetrics-daemon.sh     LaunchDaemon for GPU%/power (macOS)
│       └── install-*-autostart.bat / .ps1     legacy Windows Startup shortcuts
│
└── vendor/
    └── LibreHardwareMonitor/                  vendored LHM (gitignored, Windows)
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

If you previously ran the old Startup-folder installers, they're already
obsolete under the current lazy-launch model — uninstall any leftovers with
`scripts\install\install-*-autostart.bat -Uninstall`.
