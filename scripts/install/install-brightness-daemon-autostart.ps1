param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root     = Convert-Path (Join-Path $Here "..\..")
$Target   = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Args     = "-m brightness.daemon"
$Startup  = [Environment]::GetFolderPath("Startup")
$LinkPath = Join-Path $Startup "hardware-bar-brightness-daemon.lnk"

if ($Uninstall) {
    if (Test-Path $LinkPath) {
        Remove-Item $LinkPath -Force
        Write-Host "Removed: $LinkPath" -ForegroundColor Green
    } else {
        Write-Host "Not installed (nothing to remove)." -ForegroundColor Yellow
    }
    return
}

if (-not (Test-Path $Target)) {
    Write-Host "pythonw.exe not found at $Target" -ForegroundColor Red
    Write-Host "Create the venv first: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($LinkPath)
$sc.TargetPath       = $Target
$sc.Arguments        = $Args
$sc.WorkingDirectory = $Root
$sc.WindowStyle      = 7    # minimized (pythonw has no window anyway)
$sc.Description      = "Hardware Bar brightness daemon"
$sc.Save()

Write-Host "Installed: $LinkPath" -ForegroundColor Green
Write-Host "Target:    $Target"
Write-Host "Args:      $Args"
Write-Host ""
Write-Host "Starting daemon now..." -ForegroundColor Cyan
Start-Process -FilePath $Target -ArgumentList $Args -WorkingDirectory $Root -WindowStyle Hidden
Start-Sleep -Milliseconds 800

Write-Host "Testing with ping..."
& (Join-Path $Root ".venv\Scripts\python.exe") -m brightness.client --ping
