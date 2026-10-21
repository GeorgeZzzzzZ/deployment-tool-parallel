@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -Command "if (Get-Process -Name DeployConsoleBatchSafe -ErrorAction SilentlyContinue) { Write-Host 'Exit Deploy Console from the tray before replacing the executable.'; exit 1 }"
if errorlevel 1 exit /b 1

python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean --distpath release DeployConsole.spec
if errorlevel 1 exit /b 1

start "" /wait release\DeployConsoleBatchSafe.exe --self-test-credentials
if errorlevel 1 exit /b 1

start "" /wait release\DeployConsoleBatchSafe.exe --self-test-selenium
if errorlevel 1 exit /b 1

echo.
echo Build complete: release\DeployConsoleBatchSafe.exe
endlocal
