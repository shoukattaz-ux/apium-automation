# Downloads a portable Node.js, installs Appium + the UiAutomator2 driver, and
# downloads Android platform-tools (adb), all under packaging\ so the PyInstaller
# spec can ship them next to the .exe. End users then need no Node, Appium or
# Android SDK installs.
#
# Run once on the build machine, from the project root:
#     powershell -ExecutionPolicy Bypass -File packaging\prepare_bundle.ps1

$ErrorActionPreference = "Stop"
$NodeVersion = "22.11.0"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Server = Join-Path $Here "appium-server"
$Temp = Join-Path $env:TEMP "apium-bundle"

New-Item -ItemType Directory -Force -Path $Server, $Temp | Out-Null

# 1. Portable Node.js
$NodeZip = Join-Path $Temp "node.zip"
Write-Host "Downloading Node.js $NodeVersion..."
Invoke-WebRequest "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip" -OutFile $NodeZip
Expand-Archive $NodeZip -DestinationPath $Temp -Force
$NodeDir = Join-Path $Temp "node-v$NodeVersion-win-x64"
Copy-Item (Join-Path $NodeDir "node.exe") $Server -Force
$Npm = Join-Path $NodeDir "node_modules\npm\bin\npm-cli.js"
$Node = Join-Path $Server "node.exe"

# 2. Appium itself, installed locally into appium-server\node_modules
Write-Host "Installing Appium..."
& $Node $Npm install --prefix $Server --no-fund --no-audit appium
if ($LASTEXITCODE -ne 0) { throw "npm install appium failed" }

# 3. UiAutomator2 driver, into a private APPIUM_HOME the app points at
$env:APPIUM_HOME = Join-Path $Server "appium-home"
Write-Host "Installing UiAutomator2 driver..."
& $Node (Join-Path $Server "node_modules\appium\index.js") driver install uiautomator2
if ($LASTEXITCODE -ne 0) { throw "Appium driver install failed" }

# 4. Android platform-tools (adb)
$ToolsZip = Join-Path $Temp "platform-tools.zip"
Write-Host "Downloading Android platform-tools..."
Invoke-WebRequest "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $ToolsZip
Expand-Archive $ToolsZip -DestinationPath $Here -Force

Write-Host ""
Write-Host "Done. Now build with:  pyinstaller --noconfirm --clean packaging\apium_automation.spec"
