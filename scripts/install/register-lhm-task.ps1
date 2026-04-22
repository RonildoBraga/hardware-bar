# Registers a Task Scheduler entry that starts LibreHardwareMonitor at user
# logon with admin rights. The admin rights are required for LHM to read CPU
# MSRs (temperatures, power) and motherboard fan tachos.
#
# Uses schtasks.exe (not the ScheduledTasks cmdlets) for maximum compatibility
# across Windows versions. Invoked by register-lhm-task.bat which handles
# elevation.

$ErrorActionPreference = "Stop"

$exe  = "C:\Users\ronildo\Developer\hardware-bar\vendor\LibreHardwareMonitor\LibreHardwareMonitor.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$name = "LibreHardwareMonitor"

if (-not (Test-Path $exe)) {
    Write-Host "ERROR: LibreHardwareMonitor.exe not found at:" -ForegroundColor Red
    Write-Host "  $exe"
    exit 1
}

Write-Host "User: $user"
Write-Host "Exe:  $exe"
Write-Host ""

# Remove any existing task with this name (ignore error if it doesn't exist).
# Wrap in try/catch because $ErrorActionPreference=Stop promotes schtasks'
# stderr to a terminating NativeCommandError when the task is absent.
try {
    & schtasks.exe /Delete /TN $name /F 2>&1 | Out-Null
} catch {
    # task didn't exist - fine, we were going to delete it anyway
}

# Create the task: run at logon, as this user, with highest privileges.
$quotedExe = '"' + $exe + '"'
& schtasks.exe /Create /TN $name /TR $quotedExe /SC ONLOGON /RU $user /RL HIGHEST /F
$schtasksExit = $LASTEXITCODE

Write-Host ""

if ($schtasksExit -ne 0) {
    Write-Host "ERROR: schtasks /Create returned exit code $schtasksExit" -ForegroundColor Red
    exit $schtasksExit
}

# Verify the task now exists. Use schtasks.exe rather than Get-ScheduledTask -
# the ScheduledTasks CIM cmdlet is known to fail with "file not found" on some
# systems when there is an unrelated malformed task in the same folder.
& schtasks.exe /Query /TN $name /FO LIST 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: task not found after registration (schtasks /Query exit $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

Write-Host "Task registered and verified." -ForegroundColor Green
Write-Host ""
& schtasks.exe /Query /TN $name /XML 2>$null |
    Select-String -Pattern "RunLevel|UserId|LogonTrigger|<Enabled>true" |
    ForEach-Object { "  " + $_.Line.Trim() }

Write-Host ""
Write-Host "Starting LHM now (so you don't have to reboot)..." -ForegroundColor Cyan
& schtasks.exe /Run /TN $name | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Couldn't kick off the task via schtasks /Run (exit $LASTEXITCODE)." -ForegroundColor Yellow
    Write-Host "Launch LibreHardwareMonitor.exe manually this once." -ForegroundColor Yellow
} else {
    # Wait up to 10s for the web server to come up on 8085.
    $ok = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:8085/data.json" -UseBasicParsing -TimeoutSec 1
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch {}
    }
    if ($ok) {
        Write-Host "LHM is live on http://localhost:8085 - bar values will populate within 1s." -ForegroundColor Green
    } else {
        Write-Host "LHM started but port 8085 isn't responding yet." -ForegroundColor Yellow
        Write-Host "If it stays this way, open LHM's tray icon and enable:" -ForegroundColor Yellow
        Write-Host "  Options -> Remote Web Server -> Port=8085 and Run"
    }
}
