; Inno Setup script: wraps the PyInstaller one-folder build into a Windows installer.
;
; Build the app first (packaging\build.bat does both steps), then:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.2.0 packaging\installer.iss
; Output: dist\installer\DeviceAutomation-Setup-<version>.exe
;
; Installs per-user by default (no admin prompt) into %LOCALAPPDATA%\Programs, where
; the app can keep configs, logs and run history next to the .exe. Choosing "install
; for all users" puts it in Program Files; the app then keeps user data in
; %LOCALAPPDATA%\DeviceAutomation instead (see core/paths.py).

#define AppName "Device Automation Dashboard"
#define AppExe "DeviceAutomation.exe"
#define AppDataFolder "DeviceAutomation"
#define DistDir "..\dist\DeviceAutomation"
#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif

[Setup]
; Keep this AppId forever: upgrades and the uninstaller find previous installs by it.
AppId={{8C3F2A51-6B7E-4D0A-9F1C-2E5D7A9B4C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=DeviceAutomation-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The program itself (replaced on every upgrade). User data folders are excluded; the leading
; backslash anchors each pattern to the build root, so _internal\default_configs is still included.
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "\configs\*,\logs\*,\runs\*,\settings.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; Example scripts: installed once, never overwritten (so edits survive upgrades), kept on uninstall
; unless the user chooses to delete their data.
Source: "{#DistDir}\configs\*"; DestDir: "{app}\configs"; Flags: onlyifdoesntexist uninsneveruninstall recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\{#AppName} User Guide"; Filename: "{app}\README.txt"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; The in-app updater installs silently with /RELAUNCH=1: start the app again when done.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchRequested

[UninstallDelete]
; Created at runtime by Python/Qt; always safe to remove.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
{ adb keeps a background server running after the app closes, and the bundled Node (Appium)
  can outlive it too. Either one locks its files, so an upgrade or uninstall fails with
  "DeleteFile failed; code 5. Access is denied." Stop only the copies running from this install. }
procedure StopBundledTools(AppDir: String);
var
  ResultCode: Integer;
  Adb: String;
begin
  Adb := AppDir + '\platform-tools\adb.exe';
  if FileExists(Adb) then
    Exec(Adb, 'kill-server', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process adb,node -ErrorAction SilentlyContinue | ' +
       'Where-Object { $_.Path -like ''' + AppDir + '\*'' } | Stop-Process -Force"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopBundledTools(ExpandConstant('{app}'));
  Result := '';
end;

procedure DeleteUserData;
begin
  DelTree(ExpandConstant('{app}\configs'), True, True, True);
  DelTree(ExpandConstant('{app}\logs'), True, True, True);
  DelTree(ExpandConstant('{app}\runs'), True, True, True);
  DeleteFile(ExpandConstant('{app}\settings.json'));
  DelTree(ExpandConstant('{localappdata}\{#AppDataFolder}'), True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopBundledTools(ExpandConstant('{app}'));
  if CurUninstallStep = usPostUninstall then
  begin
    { Silent uninstalls (e.g. during an upgrade by a script) always keep the user's data. }
    if UninstallSilent then
      Exit;
    if MsgBox('Also delete your scripts, schedules, settings, logs and run history?' + #13#10 + #13#10 +
              'Choose No to keep them for a future reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DeleteUserData;
      RemoveDir(ExpandConstant('{app}'));
    end;
  end;
end;
