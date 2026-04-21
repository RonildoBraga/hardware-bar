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

# Remove any existing task with this name (ignore error if it doesn't exist)
& schtasks.exe /Delete /TN $name /F 2>$null | Out-Null

# Create the task: run at logon, as this user, with highest privileges.
$quotedExe = '"' + $exe + '"'
& schtasks.exe /Create /TN $name /TR $quotedExe /SC ONLOGON /RU $user /RL HIGHEST /F
$schtasksExit = $LASTEXITCODE

Write-Host ""

if ($schtasksExit -ne 0) {
    Write-Host "ERROR: schtasks /Create returned exit code $schtasksExit" -ForegroundColor Red
    exit $schtasksExit
}

# Verify the task now exists.
$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "ERROR: task not found after registration." -ForegroundColor Red
    exit 1
}

Write-Host "Task registered and verified." -ForegroundColor Green
$task | Format-List TaskName, TaskPath, State,
    @{N='RunLevel';E={$_.Principal.RunLevel}},
    @{N='UserId';  E={$_.Principal.UserId}}
