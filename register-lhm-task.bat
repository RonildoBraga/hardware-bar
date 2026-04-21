@echo off
REM Right-click this file and choose "Run as administrator"
REM to register LibreHardwareMonitor as an auto-starting admin task.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo This script must be run as administrator.
    echo Right-click register-lhm-task.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

pushd "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "register-lhm-task.ps1"
set EXITCODE=%ERRORLEVEL%
popd

echo.
pause
exit /b %EXITCODE%
