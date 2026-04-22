@echo off
REM Same as nightlight.py via python.exe (not pythonw.exe) so the console
REM stays open and log output is visible for debugging.
if "%~1"=="" (
    echo.
    echo Usage: nightlight-debug.bat --toggle ^| --on ^| --off ^| --status
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m nightlight %*
