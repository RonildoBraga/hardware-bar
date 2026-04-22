@echo off
REM Visible-console audio CLI for debugging.
if "%~1"=="" (
    echo.
    echo Usage: audio-debug.bat --status ^| --list ^| --vol ^<delta^> ^| --mute ^| --cycle
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m audio %*
