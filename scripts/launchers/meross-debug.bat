@echo off
REM Visible-console Meross CLI for debugging.
if "%~1"=="" (
    echo.
    echo Usage: meross-debug.bat --list ^| --on ^<name^> ^| --off ^<name^> ^| --toggle ^<name^> ^| --status ^<name^>
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m meross %*
