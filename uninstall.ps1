param(
  [switch]$PurgeData
)

$ErrorActionPreference = 'Stop'
$AppName = 'upwork-cron'
$InstallRoot = Join-Path $env:LOCALAPPDATA $AppName
$Vpy = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$DataDir = Join-Path $InstallRoot 'data'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Write-Step "Stopping watcher and removing schedule"
if (Test-Path $Vpy) {
  try { & $Vpy -m upwork_cron stop } catch { Write-Host "stop command failed (continuing): $_" -ForegroundColor Yellow }
} else {
  try { schtasks /Delete /TN UpworkCron /F | Out-Null } catch { }
  try { taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq " 2>$null | Out-Null } catch { }
}

Write-Step "Removing installed files"
$preserve = @()
if (-not $PurgeData -and (Test-Path $DataDir)) {
  $preserve = Join-Path $env:TEMP "$AppName-data-backup"
  if (Test-Path $preserve) { Remove-Item -Recurse -Force $preserve }
  Copy-Item -Recurse -Force $DataDir $preserve
  Write-Host "Data preserved at $preserve (token, db, logs). Run with -PurgeData to remove everything."
}
if (Test-Path $InstallRoot) {
  Remove-Item -Recurse -Force $InstallRoot
}

Write-Step "Uninstall complete"
if ($PurgeData) {
  Write-Host "All app files and data removed."
} else {
  Write-Host "App removed. Saved data: $preserve"
}
