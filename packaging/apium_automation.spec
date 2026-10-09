# PyInstaller spec for the Device Automation Dashboard (one-folder build).
#
# Build from the project root on Windows:
#     pyinstaller --noconfirm --clean packaging\apium_automation.spec
#
# Output: dist\DeviceAutomation\DeviceAutomation.exe plus, next to it:
#     configs\            editable scripts, read at runtime (not frozen)
#     appium-server\      bundled Node + Appium, if packaging\appium-server exists
#     platform-tools\     bundled adb, if packaging\platform-tools exists
#     README.txt          setup steps for end users
# -*- mode: python ; coding: utf-8 -*-

import os
import shutil

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
APP = "DeviceAutomation"

# Appium and Selenium import many modules lazily (by string), which PyInstaller's
# static analysis misses; pull them in explicitly.
hiddenimports = (
    collect_submodules("appium")
    + collect_submodules("selenium.webdriver.remote")
    + collect_submodules("selenium.webdriver.common")
    + collect_submodules("selenium.webdriver.support")
    + ["urllib3", "urllib3.contrib.socks", "certifi", "websocket", "trio", "trio_websocket"]
)

# Large Qt modules the app never uses.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtPdf", "PySide6.QtBluetooth", "PySide6.QtPositioning", "tkinter",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "assets"), "assets"),
        # Seed copy, used to recreate configs\ if a user deletes it.
        (os.path.join(ROOT, "configs"), "default_configs"),
    ],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # one-folder build: faster startup than one-file
    name=APP,
    icon=os.path.join(ROOT, "assets", "icon.ico"),
    version=os.path.join(ROOT, "packaging", "version_info.txt"),
    console=False,
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name=APP, upx=False)

# ---- Files that live next to the .exe, outside the frozen bundle -------------
dist_dir = os.path.join(DISTPATH, APP)
shutil.copytree(os.path.join(ROOT, "configs"), os.path.join(dist_dir, "configs"), dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("schedules.json", "__pycache__"))
shutil.copyfile(os.path.join(ROOT, "packaging", "README_FOR_USERS.txt"), os.path.join(dist_dir, "README.txt"))
for optional in ("appium-server", "platform-tools"):
    source = os.path.join(ROOT, "packaging", optional)
    if os.path.isdir(source):
        shutil.copytree(source, os.path.join(dist_dir, optional), dirs_exist_ok=True)
        print(f"Bundled {optional}")
    else:
        print(f"Note: packaging\\{optional} not found; users will need it installed separately "
              f"(run packaging\\prepare_bundle.ps1 to create it).")
