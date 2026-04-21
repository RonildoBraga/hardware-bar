@echo off
if "%~1"=="" (
    echo.
    echo Usage: brightness.bat ^<index^> ^<delta^>
    echo        brightness.bat --list
    echo.
    echo Examples:
    echo   brightness.bat 0 +5
    echo   brightness.bat 1 -5
    echo   brightness.bat --list
    echo.
    pause
    exit /b 1
)
cd /d "%~dp0"
".venv\Scripts\pythonw.exe" brightness.py %*
