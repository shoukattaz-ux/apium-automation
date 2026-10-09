@echo off
rem Builds dist\DeviceAutomation\DeviceAutomation.exe (one-folder build).
rem Run from the project root:  packaging\build.bat
rem Optional first: powershell -ExecutionPolicy Bypass -File packaging\prepare_bundle.ps1
setlocal
cd /d "%~dp0\.."

python -m pip install --upgrade -r requirements.txt pyinstaller || goto :error
python -m pytest -q tests || goto :error
pyinstaller --noconfirm --clean packaging\apium_automation.spec || goto :error

echo.
echo Build finished: dist\DeviceAutomation\DeviceAutomation.exe
echo Zip the whole dist\DeviceAutomation folder to share it.
exit /b 0

:error
echo Build failed.
exit /b 1
