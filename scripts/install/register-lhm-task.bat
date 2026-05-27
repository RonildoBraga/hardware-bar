@echo off
REM Right-click this file and choose "Run as administrator" to register
REM LibreHardwareMonitor as an on-demand admin task. After this runs once,
REM the bar can trigger LHM elevated without a UAC prompt via
REM `schtasks /Run /TN LibreHardwareMonitor`. No ONLOGON trigger — LHM only
REM starts when the bar/charts actually need it.

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
