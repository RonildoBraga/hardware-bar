# hardware-bar

Minimalist always-on-top desktop bar showing CPU/GPU/RAM/SSD/network live for this machine
(i7-12700F / RTX 3080 / Crucial T500).

## First-time setup

```
cd C:\Users\ronildo\Developer\hardware-bar
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## LibreHardwareMonitor (provides CPU + SSD temps)

1. Launch `vendor\LibreHardwareMonitor\LibreHardwareMonitor.exe` **as Administrator**
   (required to read CPU MSRs).
2. Menu **Options → Remote Web Server → Port** → set to `8085`.
3. Menu **Options → Remote Web Server → Run** → toggle ON.
4. (Optional) Menu **Options → Minimize To Tray**, **Minimize On Close**,
   **Start Minimized**, **Run On Windows Startup**.

Leave it running in the tray. The bar polls `http://localhost:8085/data.json` once per second.

## Run the bar

```
run.bat
```

Or directly:

```
.venv\Scripts\pythonw.exe bar.py
```

`pythonw.exe` (not `python.exe`) means no console window opens alongside the bar.

## Usage

- **Left-click + drag** to reposition. Position persists in `config.local.json`.
- **Right-click** for an exit menu.
- If LHM isn't running, CPU/SSD temps show `--°C`; everything else still works.
