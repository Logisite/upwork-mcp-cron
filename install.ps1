param(
  [switch]$SkipAuth,
  [switch]$SkipStart,
  [string]$Ref = 'main'
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) {
  Write-Host "`nERROR: $msg" -ForegroundColor Red
  Write-Host "Install did not complete."
  exit 1
}

if ($env:OS -ne 'Windows_NT') { Fail 'This installer is for Windows.' }
if (-not $env:LOCALAPPDATA) {
  $env:LOCALAPPDATA = [Environment]::GetFolderPath('LocalApplicationData')
}

$AppName = 'upwork-cron'
$InstallRoot = Join-Path $env:LOCALAPPDATA $AppName
$AppDir = Join-Path $InstallRoot 'app'
$VenvDir = Join-Path $InstallRoot 'venv'
$DataDir = Join-Path $InstallRoot 'data'
$BinDir = Join-Path $InstallRoot 'bin'
$RepoZip = "https://codeload.github.com/Logisite/upwork-mcp-cron/zip/refs/heads/$Ref"

Write-Step "Checking Windows and source"
$SrcDir = $PSScriptRoot
$DownloadedSrc = $null

# Never install from the installed app folder itself: deleting $AppDir below
# would remove the source we are copying from.
if ($SrcDir -and $SrcDir.TrimEnd('\').StartsWith($InstallRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
  $SrcDir = $null
}

if (-not $SrcDir -or
    -not (Test-Path (Join-Path $SrcDir 'pyproject.toml')) -or
    -not (Test-Path (Join-Path $SrcDir 'upwork_cron'))) {
  Write-Step "Downloading upwork-cron source ($Ref)"
  if (-not $env:TEMP) { Fail 'TEMP is not set.' }
  $tmp = Join-Path $env:TEMP ("upcron-src-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $zip = Join-Path $tmp 'source.zip'
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri $RepoZip -OutFile $zip
  } catch {
    Fail "Could not download source from $RepoZip : $_"
  }
  $extract = Join-Path $tmp 'src'
  try {
    Expand-Archive -Path $zip -DestinationPath $extract -Force
  } catch {
    Fail "Could not extract downloaded source: $_"
  }
  $top = Get-ChildItem -Path $extract -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName 'pyproject.toml') } |
    Select-Object -First 1
  if (-not $top) { Fail 'Downloaded source did not contain pyproject.toml.' }
  $SrcDir = $top.FullName
  $DownloadedSrc = $tmp
}

Write-Step "Locating Python 3.10+"
$py = $null
# Prefer the plain interpreter, then the Windows launcher. Probe output is
# captured before any filtering: piping a native command into Select-Object
# -First can leave $LASTEXITCODE empty even when Python ran fine.
$candidates = @(
  @{ Exe = 'python';  Args = @() },
  @{ Exe = 'py';      Args = @('-3') },
  @{ Exe = 'python3'; Args = @() }
)
foreach ($cand in $candidates) {
  try {
    $exe = $cand.Exe
    $pyArgs = @($cand.Args)
    $probe = & $exe @pyArgs -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null
    if (-not $probe) { continue }
    $verText = "$(@($probe)[-1])".Trim()
    if ($verText -notmatch '^\d+\.\d+$') { continue }
    $parts = $verText.Split('.')
    $maj = [int]$parts[0]
    $min = [int]$parts[1]
    if ($maj -eq 3 -and $min -ge 10) {
      $py = @{ Exe = $exe; Args = $pyArgs; Ver = $verText }
      break
    }
  } catch { }
}

if (-not $py) {
  Write-Host "Python 3.10 or newer was not found." -ForegroundColor Yellow
  Write-Host "Tried: python, py -3, python3"
  Write-Host "Check the same terminal with: python --version   (or: py -3 --version)"
  Write-Host "Install Python from https://www.python.org/downloads/windows/ if needed, then run this installer again."
  if ($DownloadedSrc) { Remove-Item -Recurse -Force $DownloadedSrc -ErrorAction SilentlyContinue }
  exit 1
}
Write-Host "Using Python $($py.Ver) via $($py.Exe) $($py.Args -join ' ')"

function Invoke-Py([string[]]$arguments) {
  $all = @()
  if ($py.Args) { $all += $py.Args }
  $all += $arguments
  & $py.Exe @all
  if ($LASTEXITCODE -ne 0) { throw "Python command failed: $($py.Exe) $($all -join ' ')" }
}

Write-Step "Creating install directories"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDir, $BinDir | Out-Null

Write-Step "Stopping any running watcher"
$ExistingVpy = Join-Path $VenvDir 'Scripts\python.exe'
if (Test-Path $ExistingVpy) {
  & $ExistingVpy -m upwork_cron stop
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Existing watcher did not stop cleanly; continuing." -ForegroundColor Yellow
  }
}

Write-Step "Copying application source to $AppDir"
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$exclude = @('.git', '.venv', 'venv', 'data', '__pycache__', '.pytest_cache', 'dist', 'build')
Get-ChildItem -Force -Path $SrcDir | Where-Object { $exclude -notcontains $_.Name } | ForEach-Object {
  Copy-Item -Recurse -Force -Path $_.FullName -Destination (Join-Path $AppDir $_.Name)
}
if (Test-Path (Join-Path $AppDir 'data')) {
  Remove-Item -Recurse -Force (Join-Path $AppDir 'data')
}

Write-Step "Creating virtual environment"
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
  Invoke-Py @('-m', 'venv', $VenvDir)
}
$Vpy = Join-Path $VenvDir 'Scripts\python.exe'
& $Vpy -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Fail 'pip upgrade failed.' }
& $Vpy -m pip install -e $AppDir
if ($LASTEXITCODE -ne 0) { Fail 'Dependency installation failed.' }

Write-Step "Creating the upcron command"
$shim = @(
  '@echo off',
  '"%LOCALAPPDATA%\upwork-cron\venv\Scripts\python.exe" -m upwork_cron %*'
)
foreach ($name in @('upcron.cmd', 'upwork-cron.cmd')) {
  Set-Content -Path (Join-Path $BinDir $name) -Value $shim -Encoding ASCII
}
Set-Content -Path (Join-Path $InstallRoot 'upwork-cron.cmd') -Value $shim -Encoding ASCII

Write-Step "Adding upcron to your user PATH"
function Add-UserPath([string]$dir) {
  $old = [Environment]::GetEnvironmentVariable('Path', 'User')
  $parts = @()
  if ($old) { $parts = $old.Split(';') | Where-Object { $_.Trim() } }
  $already = $parts |
    Where-Object { $_.Trim().TrimEnd('\') -ieq $dir.TrimEnd('\') } |
    Select-Object -First 1
  if (-not $already) {
    $parts += $dir
    [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
  }
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
  return (-not $already)
}
$addedToPath = Add-UserPath $BinDir
if ($addedToPath) {
  Write-Host "Added $BinDir to your user PATH."
} else {
  Write-Host "$BinDir is already on your user PATH."
}
$env:Path = "$BinDir;$env:Path"

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
  Write-Host "Skipping auth (--SkipAuth). Run: upcron auth"
}

if (-not $SkipStart) {
  Write-Step "Starting watcher and installing schedule"
  & $Vpy -m upwork_cron start
  if ($LASTEXITCODE -ne 0) { Fail 'Failed to start the watcher.' }
} else {
  Write-Host "Skipping start (--SkipStart). Run: upcron start"
}

Write-Step "Notification test"
try {
  & $Vpy -m upwork_cron notify-test
} catch {
  Write-Host "Notification test command failed: $_" -ForegroundColor Yellow
}

if ($DownloadedSrc) {
  Remove-Item -Recurse -Force $DownloadedSrc -ErrorAction SilentlyContinue
}

Write-Step "Install complete"
Write-Host "Install root : $InstallRoot"
Write-Host "App code     : $AppDir"
Write-Host "Data/logs    : $DataDir"
Write-Host "Command      : upcron"
Write-Host ""
Write-Host "upcron is on your user PATH (open a NEW terminal for it to take effect):"
Write-Host "  upcron status"
Write-Host "  upcron doctor"
Write-Host "  upcron logs -n 50"
Write-Host "  upcron stop"
Write-Host "  upcron start"
Write-Host ""
Write-Host "If no toast appeared, check Windows Settings > System > Notifications and Focus Assist."
