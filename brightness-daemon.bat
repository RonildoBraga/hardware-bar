@echo off
REM Silent daemon launcher (pythonw.exe = no console). For autostart.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" brightness_daemon.py
