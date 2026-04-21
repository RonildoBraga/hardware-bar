param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target   = Join-Path $Here ".venv\Scripts\pythonw.exe"
$Script   = Join-Path $Here "brightness_daemon.py"
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
$sc.Arguments        = "`"$Script`""
$sc.WorkingDirectory = $Here
$sc.WindowStyle      = 7    # minimized (pythonw has no window anyway)
$sc.Description      = "Hardware Bar brightness daemon"
$sc.Save()

Write-Host "Installed: $LinkPath" -ForegroundColor Green
Write-Host "Target:    $Target"
Write-Host "Args:      `"$Script`""
Write-Host ""
Write-Host "Starting daemon now..." -ForegroundColor Cyan
Start-Process -FilePath $Target -ArgumentList "`"$Script`"" -WorkingDirectory $Here -WindowStyle Hidden
Start-Sleep -Milliseconds 800

Write-Host "Testing with ping..."
& (Join-Path $Here ".venv\Scripts\python.exe") (Join-Path $Here "brightness_client.py") --ping
