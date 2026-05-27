# Registers a Task Scheduler entry for LibreHardwareMonitor as an ON-DEMAND
# admin task (no automatic trigger). After registration, the bar can fire
# `schtasks /Run /TN LibreHardwareMonitor` and LHM launches elevated with no
# UAC prompt, because Task Scheduler holds the stored admin consent.
#
# LHM needs admin rights to read CPU MSRs (temps, power) and motherboard fan
# tachos. Running elevated via this task is what lets the bar fetch those
# values automatically without prompting the user every time.
#
# Uses schtasks.exe + /XML so the task is registered with NO triggers — the
# ONDEMAND-only behaviour. Invoked by register-lhm-task.bat which handles
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

# Build a Task Scheduler XML definition with NO <Triggers> element, so the
# task only runs when explicitly triggered via schtasks /Run.
# RunLevel=HighestAvailable is what allows LHM to read CPU MSRs.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$exe</Command>
    </Exec>
  </Actions>
</Task>
"@

# schtasks requires UTF-16 (the XML declaration says so).
$tmp = [System.IO.Path]::GetTempFileName()
try {
    [System.IO.File]::WriteAllText($tmp, $xml, [System.Text.Encoding]::Unicode)
    & schtasks.exe /Create /TN $name /XML $tmp /F
    $schtasksExit = $LASTEXITCODE
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

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

Write-Host "Task registered (on-demand, no automatic trigger)." -ForegroundColor Green
Write-Host ""
& schtasks.exe /Query /TN $name /XML 2>$null |
    Select-String -Pattern "RunLevel|UserId|<Enabled>true|AllowStartOnDemand" |
    ForEach-Object { "  " + $_.Line.Trim() }

Write-Host ""
Write-Host "Done. The bar will auto-trigger this task when it first needs LHM." -ForegroundColor Cyan
Write-Host "(LHM will NOT start on logon anymore.)"
