@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "PS=%SCRIPT_DIR%install.ps1"
if exist "%PS%" goto run

set "PS=%TEMP%\upcron-install.ps1"
echo Downloading installer...
where curl >nul 2>nul
if errorlevel 1 goto download_ps
curl -fL "https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/install.ps1" -o "%PS%"
if errorlevel 1 goto download_fail
goto run

:download_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/Logisite/upwork-mcp-cron/main/install.ps1' -OutFile '%PS%'"
if errorlevel 1 goto download_fail

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="130" goto cancelled
if "%RC%"=="-1073741510" goto cancelled
if not "%RC%"=="0" (
  echo.
  echo Install failed with exit code %RC%.
  pause
)
exit /b %RC%

:cancelled
echo.
echo Install cancelled.
exit /b 130

:download_fail
echo.
echo Failed to download the installer.
pause
exit /b 1
