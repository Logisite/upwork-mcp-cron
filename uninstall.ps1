param(
  [switch]$PurgeData
)

$ErrorActionPreference = 'Stop'
$AppName = 'upwork-cron'
if (-not $env:LOCALAPPDATA) {
  $env:LOCALAPPDATA = [Environment]::GetFolderPath('LocalApplicationData')
}
$InstallRoot = Join-Path $env:LOCALAPPDATA $AppName
$Vpy = Join-Path $InstallRoot 'venv\Scripts\python.exe'
$DataDir = Join-Path $InstallRoot 'data'
$BinDir = Join-Path $InstallRoot 'bin'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Write-Step "Stopping watcher and removing schedule"
if (Test-Path $Vpy) {
  & $Vpy -m upwork_cron stop
  if ($LASTEXITCODE -ne 0) {
    Write-Host "stop command failed (continuing with manual cleanup)." -ForegroundColor Yellow
  }
}

# Always make sure the task is gone, even if the stop command failed.
try { schtasks /Delete /TN UpworkCron /F 2>$null | Out-Null } catch { }

# Stop only the PID recorded by the watcher, and only if it is still Python.
$pidFile = Join-Path $DataDir 'watcher.pid'
if (Test-Path $pidFile) {
  $procId = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($procId -match '^\d+$') {
    $proc = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'python*') {
      Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
  }
}

Write-Step "Removing upcron from your user PATH"
$oldPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($oldPath) {
  $parts = $oldPath.Split(';') |
    Where-Object { $_.Trim() -and ($_.Trim().TrimEnd('\') -ine $BinDir.TrimEnd('\')) }
  [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
  try {
    if (-not ('Upcron.Native' -as [type])) {
      Add-Type -Namespace Upcron -Name Native -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@ -ErrorAction Stop
    }
    $res = [UIntPtr]::Zero
    [Upcron.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$res) | Out-Null
  } catch { }
}

$preserve = Join-Path $env:LOCALAPPDATA "$AppName-data-backup"
if (-not $PurgeData -and (Test-Path $DataDir)) {
  Write-Step "Preserving data"
  if (Test-Path $preserve) { Remove-Item -Recurse -Force $preserve -ErrorAction SilentlyContinue }
  Copy-Item -Recurse -Force $DataDir $preserve
  Write-Host "Data preserved at $preserve (token, db, logs)."
  Write-Host "Run with -PurgeData to remove everything."
} elseif ($PurgeData) {
  if (Test-Path $preserve) { Remove-Item -Recurse -Force $preserve -ErrorAction SilentlyContinue }
}

Write-Step "Removing installed files"
if (Test-Path $InstallRoot) {
  try {
    Remove-Item -Recurse -Force $InstallRoot -ErrorAction Stop
  } catch {
    Write-Host "Could not remove all files: $_" -ForegroundColor Yellow
    Write-Host "Close any running upcron/python process and delete $InstallRoot manually."
  }
}

Write-Step "Uninstall complete"
if ($PurgeData) {
  Write-Host "All app files and data removed."
} else {
  Write-Host "App removed. Saved data: $preserve"
}
