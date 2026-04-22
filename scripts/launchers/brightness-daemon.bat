@echo off
REM Silent daemon launcher (pythonw.exe = no console). For autostart.
cd /d "%~dp0..\.."
start "" ".venv\Scripts\pythonw.exe" -m brightness.daemon
