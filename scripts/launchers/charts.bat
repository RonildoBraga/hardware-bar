@echo off
if "%~1"=="" (
    echo.
    echo Usage: charts.bat ^<metric^>
    echo.
    echo Available metrics:
    echo   cpu  cpu-temp  gpu  gpu-temp  ram
    echo   disk  disk-temps  net  temps
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0..\.."
start "" ".venv\Scripts\pythonw.exe" -m bar.charts %1
