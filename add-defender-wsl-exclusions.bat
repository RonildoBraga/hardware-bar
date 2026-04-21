@echo off
REM Right-click this file and choose "Run as administrator"
REM to add WSL path/process exclusions to Windows Defender.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo This script must be run as administrator.
    echo Right-click add-defender-wsl-exclusions.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

pushd "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "add-defender-wsl-exclusions.ps1"
set EXITCODE=%ERRORLEVEL%
popd

echo.
pause
exit /b %EXITCODE%
