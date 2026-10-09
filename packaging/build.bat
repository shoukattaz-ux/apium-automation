@echo off
rem Builds the app and, if Inno Setup 6 is installed, the Windows installer.
rem Run from anywhere:  packaging\build.bat
rem Optional first (bundle Node + Appium + adb so users install nothing):
rem   powershell -ExecutionPolicy Bypass -File packaging\prepare_bundle.ps1
setlocal
cd /d "%~dp0\.."

python -m pip install --upgrade -r requirements.txt pyinstaller || goto :error
python -m pytest -q tests || goto :error
pyinstaller --noconfirm --clean packaging\apium_automation.spec || goto :error

for /f "usebackq delims=" %%v in (`python -c "from core.version import APP_VERSION; print(APP_VERSION)"`) do set VERSION=%%v

set ISCC=
where iscc >nul 2>nul && set ISCC=iscc
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

echo.
echo App build:  dist\DeviceAutomation\DeviceAutomation.exe  (version %VERSION%)
if defined ISCC (
    %ISCC% /Qp /DAppVersion=%VERSION% packaging\installer.iss || goto :error
    echo Installer:  dist\installer\DeviceAutomation-Setup-%VERSION%.exe
) else (
    echo Inno Setup 6 not found - skipped the installer. Get it from https://jrsoftware.org/isdl.php
    echo then re-run this script, or zip dist\DeviceAutomation to share the portable folder.
)
exit /b 0

:error
echo Build failed.
exit /b 1
