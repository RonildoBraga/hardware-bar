# Excludes WSL-related paths from Windows Defender real-time scanning.
# This dramatically reduces CPU used by dllhost.exe hosting the 9P protocol
# server (vp9fs.dll) — Defender scanning over \\wsl$\ is the classic cause
# of sustained high CPU usage when WSL2 / Docker Desktop are installed.
#
# You are NOT disabling Defender — just telling it not to scan WSL virtual
# filesystem paths, which aren't designed to be scanned anyway.

$ErrorActionPreference = "Stop"

$paths = @(
    "\\wsl$\",
    "\\wsl.localhost\",
    "$env:LOCALAPPDATA\Docker\wsl",
    "$env:LOCALAPPDATA\Packages\CanonicalGroupLimited.Ubuntu_79rhkp1fndgsc\LocalState",
    "$env:LOCALAPPDATA\Packages\DockerDesktop_*\LocalState"
)

$processes = @(
    "wsl.exe",
    "wslservice.exe",
    "vmmem",
    "vmmemWSL",
    "com.docker.backend.exe",
    "Docker Desktop.exe"
)

Write-Host "Adding Defender path exclusions..."
foreach ($p in $paths) {
    try {
        Add-MpPreference -ExclusionPath $p
        Write-Host "  + $p" -ForegroundColor Green
    } catch {
        Write-Host "  ! $p  -  $_" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Adding Defender process exclusions..."
foreach ($proc in $processes) {
    try {
        Add-MpPreference -ExclusionProcess $proc
        Write-Host "  + $proc" -ForegroundColor Green
    } catch {
        Write-Host "  ! $proc  -  $_" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Current exclusions:" -ForegroundColor Cyan
$pref = Get-MpPreference
Write-Host "Paths:"
$pref.ExclusionPath | ForEach-Object { "  $_" }
Write-Host "Processes:"
$pref.ExclusionProcess | ForEach-Object { "  $_" }
