# Registers a Task Scheduler entry that starts LibreHardwareMonitor at user
# logon with admin rights. The admin rights are required for LHM to read CPU
# MSRs (temperatures, power) and motherboard fan tachos.
#
# This script is invoked by register-lhm-task.bat, which handles elevation.

$exe  = "C:\Users\ronildo\Developer\hardware-bar\vendor\LibreHardwareMonitor\LibreHardwareMonitor.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $exe)) {
    Write-Host "ERROR: LibreHardwareMonitor.exe not found at:" -ForegroundColor Red
    Write-Host "  $exe"
    exit 1
}

$action    = New-ScheduledTaskAction    -Execute $exe
$trigger   = New-ScheduledTaskTrigger   -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask `
    -TaskName   "LibreHardwareMonitor" `
    -Action     $action `
    -Trigger    $trigger `
    -Principal  $principal `
    -Settings   $settings `
    -Description "Start LHM at logon with admin rights (for CPU MSRs and fan tachos)" `
    -Force | Out-Null

Write-Host ""
Write-Host "Scheduled task registered." -ForegroundColor Green
Get-ScheduledTask -TaskName "LibreHardwareMonitor" |
    Format-List TaskName, State, @{N='RunLevel';E={$_.Principal.RunLevel}}
