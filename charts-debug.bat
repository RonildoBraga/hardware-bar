@echo off
REM Same as charts.bat but uses python.exe (not pythonw.exe) so the console
REM stays open and log output is visible for debugging.
if "%~1"=="" (
    echo.
    echo Usage: charts-debug.bat ^<metric^>
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0"
".venv\Scripts\python.exe" charts.py %1
