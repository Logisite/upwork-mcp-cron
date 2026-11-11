param(
  [switch]$SkipAuth,
  [switch]$SkipStart
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) {
  Write-Host "`nERROR: $msg" -ForegroundColor Red
  Write-Host "Install did not complete."
  exit 1
}

$AppName = 'upwork-cron'
$InstallRoot = Join-Path $env:LOCALAPPDATA $AppName
$AppDir = Join-Path $InstallRoot 'app'
$VenvDir = Join-Path $InstallRoot 'venv'
$DataDir = Join-Path $InstallRoot 'data'
$SrcDir = $PSScriptRoot

Write-Step "Checking Windows and source"
if ($env:OS -ne 'Windows_NT') { Fail 'This installer is for Windows.' }
if (-not (Test-Path (Join-Path $SrcDir 'pyproject.toml'))) { Fail "Source not found in $SrcDir (pyproject.toml missing)." }
if (-not (Test-Path (Join-Path $SrcDir 'upwork_cron'))) { Fail "Source not found in $SrcDir (upwork_cron package missing)." }

Write-Step "Locating Python 3.10+"
$py = $null
foreach ($cand in @(@('py','-3'), @('python3'), @('python'))) {
  try {
    $exe = $cand[0]
    $args = @()
    if ($cand.Length -gt 1) { $args += $cand[1..($cand.Length-1)] }
    $ver = & $exe @args -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver) {
      $parts = $ver.Trim().Split('.')
      $maj = [int]$parts[0]; $min = [int]$parts[1]
      if ($maj -eq 3 -and $min -ge 10) { $py = @{ Exe = $exe; Args = $args; Ver = $ver.Trim() }; break }
    }
  } catch { }
}
if (-not $py) {
  Write-Host "Python 3.10 or newer was not found." -ForegroundColor Yellow
  Write-Host "Install it from https://www.python.org/downloads/windows/ and make sure the Python Launcher (py) is installed."
  Write-Host "Then run this installer again."
  exit 1
}
Write-Host "Using Python $($py.Ver) via $($py.Exe) $($py.Args -join ' ')"

function Invoke-Py([string[]]$pArgs) {
  $all = @()
  if ($py.Args) { $all += $py.Args }
  $all += $pArgs
  & $py.Exe @all
  if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($py.Exe) $($all -join ' ')" }
}

Write-Step "Creating install directories"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDir | Out-Null

Write-Step "Copying application source to $AppDir"
if (Test-Path $AppDir) {
  # Keep data out of app dir; replace app contents for upgrades.
  Remove-Item -Recurse -Force $AppDir
}
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$exclude = @('.git', '.venv', 'venv', 'data', '__pycache__', '.pytest_cache', 'dist', 'build')
Get-ChildItem -Force -Path $SrcDir | Where-Object { $exclude -notcontains $_.Name } | ForEach-Object {
  Copy-Item -Recurse -Force -Path $_.FullName -Destination (Join-Path $AppDir $_.Name)
}
# Ensure packaged data dir is not shipped inside app copy.
if (Test-Path (Join-Path $AppDir 'data')) { Remove-Item -Recurse -Force (Join-Path $AppDir 'data') }

Write-Step "Creating virtual environment"
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
  Invoke-Py @('-m','venv',$VenvDir)
}
$Vpy = Join-Path $VenvDir 'Scripts\python.exe'
$Vpip = Join-Path $VenvDir 'Scripts\pip.exe'
& $Vpy -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Fail 'pip upgrade failed.' }
& $Vpy -m pip install -e $AppDir
if ($LASTEXITCODE -ne 0) { Fail 'Dependency installation failed.' }

$AppCmd = Join-Path $InstallRoot 'upwork-cron.cmd'
@"
@echo off
"%LOCALAPPDATA%\$AppName\venv\Scripts\python.exe" -m upwork_cron %*
"@ | Set-Content -Path $AppCmd -Encoding ASCII

$env:UPWORK_CRON_DATA = $DataDir

if (-not $SkipAuth) {
  Write-Step "Upwork sign-in"
  $token = Join-Path $DataDir 'token.json'
  if (Test-Path $token) {
    Write-Host "Existing token found at $token; skipping browser sign-in."
  } else {
    Write-Host "A browser window will open so you can approve access. If it does not, copy the printed URL."
    & $Vpy -m upwork_cron auth
    if ($LASTEXITCODE -ne 0) { Fail 'Authentication did not complete.' }
  }
} else {
  Write-Host "Skipping auth (--SkipAuth). Run: `"$AppCmd`" auth"
}

if (-not $SkipStart) {
  Write-Step "Starting watcher and installing schedule"
  & $Vpy -m upwork_cron start
  if ($LASTEXITCODE -ne 0) { Fail 'Failed to start the watcher.' }
} else {
  Write-Host "Skipping start (--SkipStart). Run: `"$AppCmd`" start"
}

Write-Step "Notification test"
try { & $Vpy -m upwork_cron notify-test } catch { Write-Host "Notification test command failed: $_" -ForegroundColor Yellow }

Write-Step "Install complete"
Write-Host "Install root : $InstallRoot"
Write-Host "App code     : $AppDir"
Write-Host "Data/logs    : $DataDir"
Write-Host "Command      : $AppCmd"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  $AppCmd status"
Write-Host "  $AppCmd doctor"
Write-Host "  $AppCmd logs -n 50"
Write-Host "  $AppCmd stop"
Write-Host "  $AppCmd start"
Write-Host ""
Write-Host "If no toast appeared, check Windows Settings > System > Notifications and Focus Assist."
