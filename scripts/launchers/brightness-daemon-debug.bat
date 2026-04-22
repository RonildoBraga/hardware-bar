@echo off
REM Visible daemon launcher — runs in foreground with a console so you
REM can see logs and Ctrl+C to stop.
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m brightness.daemon
