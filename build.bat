@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean DeployConsole.spec
if errorlevel 1 exit /b 1

start "" /wait dist\DeployConsoleBatchSafe.exe --self-test-credentials
if errorlevel 1 exit /b 1

start "" /wait dist\DeployConsoleBatchSafe.exe --self-test-selenium
if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\DeployConsoleBatchSafe.exe
endlocal
