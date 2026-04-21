@echo off
REM Same as brightness.bat but uses python.exe (not pythonw.exe) so the console
REM stays open and log output is visible for debugging.
if "%~1"=="" (
    echo.
    echo Usage: brightness-debug.bat ^<index^> ^<delta^>
    echo        brightness-debug.bat --list
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0"
".venv\Scripts\python.exe" brightness.py %*
